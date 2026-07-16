from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import Request
from pydantic import BaseModel, Field


APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parents[1]
RUNTIME_DIR = APP_DIR / "runtime"
TASKS_DIR = RUNTIME_DIR / "tasks"
DB_PATH = RUNTIME_DIR / "console.db"
TEMPLATES = Jinja2Templates(directory=str(APP_DIR / "templates"))

SOURCE_PROJECT = Path(os.getenv("GROK_REGISTER_SOURCE_DIR", str(REPO_ROOT))).resolve()
SOURCE_VENV_PYTHON = Path(
    os.getenv("GROK_REGISTER_PYTHON", str(SOURCE_PROJECT / ".venv" / "bin" / "python"))
).expanduser()
DEFAULT_MAX_CONCURRENT_TASKS = max(
    1, int(os.getenv("GROK_REGISTER_CONSOLE_MAX_CONCURRENT_TASKS", "1"))
)
# Hard ceiling so a bad UI value cannot fork-bomb the host.
MAX_CONCURRENT_TASKS_CAP = max(
    DEFAULT_MAX_CONCURRENT_TASKS,
    int(os.getenv("GROK_REGISTER_CONSOLE_MAX_CONCURRENT_CAP", "8")),
)
SUPERVISOR_INTERVAL = max(1.0, float(os.getenv("GROK_REGISTER_CONSOLE_POLL_INTERVAL", "2")))
# Backward-compatible alias used by templates/meta until settings override is applied.
MAX_CONCURRENT_TASKS = DEFAULT_MAX_CONCURRENT_TASKS


def _normalize_root_path(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw or raw == "/":
        return ""
    if not raw.startswith("/"):
        raw = f"/{raw}"
    return raw.rstrip("/")


ROOT_PATH = _normalize_root_path(os.getenv("GROK_REGISTER_CONSOLE_ROOT_PATH", ""))


CONSOLE_AUTH_TOKEN = (os.getenv("GROK_REGISTER_CONSOLE_AUTH_TOKEN") or "").strip()
CONSOLE_BASIC_USER = (os.getenv("GROK_REGISTER_CONSOLE_BASIC_USER") or "").strip()
CONSOLE_BASIC_PASSWORD = (os.getenv("GROK_REGISTER_CONSOLE_BASIC_PASSWORD") or "").strip()


def console_auth_enabled() -> bool:
    return bool(CONSOLE_AUTH_TOKEN or (CONSOLE_BASIC_USER and CONSOLE_BASIC_PASSWORD))


AUTH_COOKIE_NAME = "grok_register_console_token"


def _extract_console_token(request: Request) -> str:
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    q = (request.query_params.get("token") or "").strip()
    if q:
        return q
    cookie = (request.cookies.get(AUTH_COOKIE_NAME) or "").strip()
    if cookie:
        return cookie
    return ""


def require_console_auth(request: Request) -> str | None:
    """Optional console gate: bearer / query / cookie token and/or basic auth.

    Returns the accepted token when token-auth succeeds so middleware can set cookie.
    """
    if not console_auth_enabled():
        return None

    if CONSOLE_AUTH_TOKEN:
        token = _extract_console_token(request)
        if token and token == CONSOLE_AUTH_TOKEN:
            # Remember how it was provided so browser static assets can reuse cookie.
            if (request.query_params.get("token") or "").strip() == CONSOLE_AUTH_TOKEN:
                request.state.console_set_auth_cookie = True
            return token

    if CONSOLE_BASIC_USER and CONSOLE_BASIC_PASSWORD:
        auth = (request.headers.get("authorization") or "").strip()
        if auth.lower().startswith("basic "):
            import base64

            try:
                raw = base64.b64decode(auth[6:].strip()).decode("utf-8", errors="ignore")
                user, _, password = raw.partition(":")
                if user == CONSOLE_BASIC_USER and password == CONSOLE_BASIC_PASSWORD:
                    return None
            except Exception:
                pass

    # Challenge basic auth in browser when configured.
    headers = {}
    if CONSOLE_BASIC_USER and CONSOLE_BASIC_PASSWORD:
        headers["WWW-Authenticate"] = 'Basic realm="Grok Register Console"'
    raise HTTPException(status_code=401, detail="Unauthorized", headers=headers)


PROJECT_FILES = ("DrissionPage_example.py", "email_register.py", "mint_and_push.py")
PROJECT_DIRS = ("turnstilePatch",)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_STOPPING = "stopping"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"

LINE_RE_ROUND = re.compile(r"开始第\s*(\d+)\s*轮注册")
LINE_RE_SUCCESS = re.compile(r"注册成功\s*\|\s*email=([^|\s]+)")
LINE_RE_ERROR = re.compile(r"\[Error\]\s*第\s*(\d+)\s*轮失败:\s*(.+)")
LINE_RE_TEMP_EMAIL = re.compile(r"临时邮箱创建成功:\s*([^\s]+)")
LINE_RE_FILLED_EMAIL = re.compile(r"已填写邮箱并点击注册:\s*([^\s]+)")
# Compatible with old and current sink push logs.
LINE_RE_PUSH = re.compile(
    r"(?:SSO token 已推送到 API|SSO 已推送到 grok2api|已推送到 grok2api|注册完成，推送\s*\d+\s*个 token 到 API)"
)
LINE_RE_PUSH_STATS = re.compile(
    r"SSO 已推送到 grok2api（新增\s*(\d+)\s*，更新\s*(\d+)\s*，本轮\s*(\d+)\s*个）"
)
LINE_RE_PUSH_COUNT = re.compile(r"注册完成，推送\s*(\d+)\s*个 token 到 API")

db_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with db_lock, get_conn() as conn:
        return conn.execute(query, params).fetchall()


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with db_lock, get_conn() as conn:
        return conn.execute(query, params).fetchone()


def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with db_lock, get_conn() as conn:
        cur = conn.execute(query, params)
        conn.commit()
        return int(cur.lastrowid)


def execute_no_return(query: str, params: tuple[Any, ...] = ()) -> None:
    with db_lock, get_conn() as conn:
        conn.execute(query, params)
        conn.commit()


def init_db() -> None:
    ensure_dirs()
    with db_lock, get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                target_count INTEGER NOT NULL,
                completed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                current_round INTEGER NOT NULL DEFAULT 0,
                current_phase TEXT,
                last_email TEXT,
                last_error TEXT,
                last_log_at TEXT,
                notes TEXT,
                config_json TEXT NOT NULL,
                task_dir TEXT NOT NULL,
                console_path TEXT NOT NULL,
                pid INTEGER,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                exit_code INTEGER
            );
            """
        )


def load_source_defaults() -> dict[str, Any]:
    config_path = SOURCE_PROJECT / "config.json"
    if config_path.exists():
        base = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        example_path = SOURCE_PROJECT / "config.example.json"
        if example_path.exists():
            base = json.loads(example_path.read_text(encoding="utf-8"))
        else:
            base = {
                "run": {"count": 50},
                "proxy": "",
                "browser_proxy": "",
                "temp_mail_api_base": "",
                "temp_mail_admin_password": "",
                "temp_mail_domain": "",
                "temp_mail_site_password": "",
                "api": {"endpoint": "", "token": "", "append": True},
            }

    env_count = os.getenv("GROK_REGISTER_DEFAULT_RUN_COUNT", "").strip()
    if env_count:
        try:
            base.setdefault("run", {})["count"] = max(1, int(env_count))
        except ValueError:
            pass

    env_map = {
        "proxy": "GROK_REGISTER_DEFAULT_PROXY",
        "browser_proxy": "GROK_REGISTER_DEFAULT_BROWSER_PROXY",
        "temp_mail_api_base": "GROK_REGISTER_DEFAULT_TEMP_MAIL_API_BASE",
        "temp_mail_admin_password": "GROK_REGISTER_DEFAULT_TEMP_MAIL_ADMIN_PASSWORD",
        "temp_mail_domain": "GROK_REGISTER_DEFAULT_TEMP_MAIL_DOMAIN",
        "temp_mail_site_password": "GROK_REGISTER_DEFAULT_TEMP_MAIL_SITE_PASSWORD",
    }
    for key, env_name in env_map.items():
        value = os.getenv(env_name)
        if value is not None:
            base[key] = value

    api_base = dict(base.get("api") or {})
    api_env_map = {
        "endpoint": "GROK_REGISTER_DEFAULT_API_ENDPOINT",
        "token": "GROK_REGISTER_DEFAULT_API_TOKEN",
        "import_endpoint": "GROK_REGISTER_DEFAULT_API_IMPORT_ENDPOINT",
        "admin_username": "GROK_REGISTER_DEFAULT_API_ADMIN_USERNAME",
        "admin_password": "GROK_REGISTER_DEFAULT_API_ADMIN_PASSWORD",
    }
    for key, env_name in api_env_map.items():
        value = os.getenv(env_name)
        if value is not None:
            api_base[key] = value
    append_env = os.getenv("GROK_REGISTER_DEFAULT_API_APPEND")
    if append_env is not None:
        api_base["append"] = append_env.strip().lower() in {"1", "true", "yes", "on"}
    # 兼容旧 token-sink 配置：若仍是 /v1/admin/tokens，改用 Go 版 admin login。
    endpoint = str(api_base.get("endpoint", "") or "").strip()
    if endpoint.endswith("/v1/admin/tokens") or "/v1/admin/tokens" in endpoint:
        # console 容器常无法解析 grok2api 主机名；优先用 docker bridge 网关。
        api_base["endpoint"] = "http://172.18.0.1:8000/api/admin/v1/auth/login"
        api_base.setdefault(
            "import_endpoint",
            "http://172.18.0.1:8000/api/admin/v1/accounts/web/import",
        )
        api_base.setdefault("admin_username", "admin")
        if not str(api_base.get("admin_password", "") or "").strip() and api_base.get("token"):
            # 旧 token 字段不再作为 Bearer；若显式配置了 admin_password 则用它。
            pass
    if not str(api_base.get("import_endpoint", "") or "").strip():
        login_ep = str(api_base.get("endpoint", "") or "").strip()
        if login_ep.endswith("/auth/login"):
            api_base["import_endpoint"] = login_ep.replace("/auth/login", "/accounts/web/import")
    base["api"] = api_base

    # CLIProxyAPI auth 目录：优先容器挂载点 /cliproxy-auths（对应宿主机 CLIProxyAPI/auths）
    cliproxy_env = (
        os.getenv("GROK_REGISTER_DEFAULT_CLIPROXY_AUTH_DIR")
        or os.getenv("CLIPROXYAPI_AUTH_DIR")
        or ""
    ).strip()
    if cliproxy_env:
        base["cliproxy_auth_dir"] = cliproxy_env
    else:
        base.setdefault("cliproxy_auth_dir", "/cliproxy-auths")
    base.setdefault("cliproxy_push_enabled", True)
    base.setdefault("cpa_enabled", False)

    # YesCaptcha：仅透传环境变量，不写进配置文件明文
    yk = (os.getenv("YESCAPTCHA_API_KEY") or "").strip()
    if yk:
        base["yescaptcha_api_key_configured"] = True
    else:
        base["yescaptcha_api_key_configured"] = False
    return base


def _mask_proxy(proxy_url: str) -> str:
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.netloc:
        return proxy_url
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def _request_with_optional_proxy(
    url: str,
    proxy_url: str = "",
    method: str = "GET",
    timeout: int = 15,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    return requests.request(
        method,
        url,
        timeout=timeout,
        headers=headers,
        proxies=proxies,
        allow_redirects=True,
    )


def _build_health_item(
    key: str,
    label: str,
    ok: bool,
    summary: str,
    detail: str,
    target: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "ok": ok,
        "summary": summary,
        "detail": detail,
        "target": target,
        "checked_at": now_iso(),
    }



def _fetch_grok2api_pool_stats(defaults: dict[str, Any]) -> dict[str, Any]:
    """Login to grok2api and summarize account pool counts."""
    api_conf = dict(defaults.get("api") or {})
    login_url = str(api_conf.get("endpoint") or "").strip()
    import_url = str(api_conf.get("import_endpoint") or "").strip()
    username = str(api_conf.get("admin_username") or "admin").strip() or "admin"
    password = str(api_conf.get("admin_password") or "").strip()
    if not login_url:
        return {"ok": False, "summary": "未配置 login endpoint", "detail": "缺少 api.endpoint", "target": "-", "total": 0, "providers": {}}

    try:
        login_resp = requests.post(
            login_url,
            json={"username": username, "password": password},
            timeout=15,
            headers={"Content-Type": "application/json"},
        )
        if login_resp.status_code != 200:
            return {
                "ok": False,
                "summary": f"登录失败 HTTP {login_resp.status_code}",
                "detail": "admin login 未成功，无法读取号池。",
                "target": login_url,
                "total": 0,
                "providers": {},
            }
        payload = login_resp.json()
        token = (
            ((payload.get("data") or {}).get("tokens") or {}).get("accessToken")
            or payload.get("token")
            or payload.get("access_token")
        )
        if not token:
            return {
                "ok": False,
                "summary": "登录响应无 token",
                "detail": "admin login 返回成功但没有 accessToken。",
                "target": login_url,
                "total": 0,
                "providers": {},
            }

        headers = {"Authorization": f"Bearer {token}"}
        # total
        total_resp = requests.get(
            login_url.rsplit("/api/admin/v1/", 1)[0] + "/api/admin/v1/accounts?page=1&pageSize=1",
            headers=headers,
            timeout=15,
        )
        total = 0
        if total_resp.status_code == 200:
            total = int((((total_resp.json().get("data") or {}).get("total")) or 0))

        providers: dict[str, int] = {}
        for provider in ("grok_web", "grok_build", "grok_console"):
            base = login_url.rsplit("/api/admin/v1/", 1)[0]
            resp = requests.get(
                f"{base}/api/admin/v1/accounts?page=1&pageSize=1&provider={provider}",
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                providers[provider] = int((((resp.json().get("data") or {}).get("total")) or 0))

        # import endpoint reachability (empty body should be 400 invalidAuthFile)
        import_ok = False
        import_summary = "未配置 import"
        if import_url:
            try:
                ireq = requests.post(import_url, headers={**headers, "Content-Type": "application/json"}, json={}, timeout=15)
                import_ok = ireq.status_code in {200, 400, 401, 403, 422}
                import_summary = f"import HTTP {ireq.status_code}"
            except Exception as exc:
                import_summary = f"import 不可达: {exc}"

        parts = [f"total {total}"]
        for k, v in providers.items():
            parts.append(f"{k} {v}")
        return {
            "ok": True,
            "summary": " | ".join(parts),
            "detail": f"admin login 成功；{import_summary}。",
            "target": import_url or login_url,
            "total": total,
            "providers": providers,
            "import_ok": import_ok,
            "import_summary": import_summary,
        }
    except Exception as exc:
        return {
            "ok": False,
            "summary": "号池统计失败",
            "detail": str(exc),
            "target": login_url,
            "total": 0,
            "providers": {},
        }


def run_health_checks() -> dict[str, Any]:
    defaults = merged_defaults()
    items: list[dict[str, Any]] = []

    browser_proxy = str(defaults.get("browser_proxy", "") or "").strip()
    request_proxy = str(defaults.get("proxy", "") or "").strip()
    api_conf = dict(defaults.get("api") or {})
    api_endpoint = str(api_conf.get("endpoint", "") or "").strip()
    temp_mail_api_base = str(defaults.get("temp_mail_api_base", "") or "").strip()

    warp_target = browser_proxy or request_proxy
    if not warp_target:
        items.append(
            _build_health_item(
                "warp",
                "WARP / Proxy",
                False,
                "未配置代理出口",
                "当前系统默认配置里没有 `browser_proxy` 或 `proxy`，无法检查前置网络出口。",
                "-",
            )
        )
    else:
        try:
            response = _request_with_optional_proxy(
                "https://www.cloudflare.com/cdn-cgi/trace",
                proxy_url=warp_target,
                timeout=20,
            )
            body = response.text
            ip_match = re.search(r"(?m)^ip=(.+)$", body)
            loc_match = re.search(r"(?m)^loc=(.+)$", body)
            warp_match = re.search(r"(?m)^warp=(.+)$", body)
            ip = ip_match.group(1).strip() if ip_match else "unknown"
            loc = loc_match.group(1).strip() if loc_match else "unknown"
            warp_state = warp_match.group(1).strip() if warp_match else "unknown"
            ok = response.status_code == 200
            items.append(
                _build_health_item(
                    "warp",
                    "WARP / Proxy",
                    ok,
                    f"HTTP {response.status_code} | IP {ip} | LOC {loc}",
                    f"通过代理 `{_mask_proxy(warp_target)}` 访问 Cloudflare trace 成功，warp={warp_state}。",
                    _mask_proxy(warp_target),
                )
            )
        except Exception as exc:
            items.append(
                _build_health_item(
                    "warp",
                    "WARP / Proxy",
                    False,
                    "代理出口不可达",
                    f"通过 `{_mask_proxy(warp_target)}` 访问 Cloudflare trace 失败：{exc}",
                    _mask_proxy(warp_target),
                )
            )

    if not api_endpoint:
        items.append(
            _build_health_item(
                "grok2api",
                "grok2api Sink",
                False,
                "未配置 token sink",
                "当前系统默认配置里没有 `api.endpoint`，注册成功后不会自动入池。",
                "-",
            )
        )
    else:
        try:
            # Go 版 admin login 需要 POST；旧 token-sink 可能接受 GET。
            method = "POST" if api_endpoint.rstrip("/").endswith("/auth/login") else "GET"
            headers = {"Content-Type": "application/json"} if method == "POST" else None
            body = None
            if method == "POST":
                api_conf = dict(defaults.get("api") or {})
                body = {
                    "username": str(api_conf.get("admin_username") or "admin"),
                    "password": str(api_conf.get("admin_password") or ""),
                }
            response = requests.request(
                method,
                api_endpoint,
                timeout=15,
                headers=headers,
                json=body,
                allow_redirects=True,
            )
            ok = response.status_code in {200, 401, 403, 405}
            items.append(
                _build_health_item(
                    "grok2api",
                    "grok2api Sink",
                    ok,
                    f"HTTP {response.status_code}",
                    "接口已可达。即使返回 401/403，也说明服务本身在线，只是需要正确的管理口令。",
                    api_endpoint,
                )
            )
        except Exception as exc:
            items.append(
                _build_health_item(
                    "grok2api",
                    "grok2api Sink",
                    False,
                    "接口不可达",
                    f"访问 `{api_endpoint}` 失败：{exc}",
                    api_endpoint,
                )
            )

    if not temp_mail_api_base:
        items.append(
            _build_health_item(
                "temp_mail",
                "Temp Mail API",
                False,
                "未配置临时邮箱 API",
                "当前系统默认配置里没有 `temp_mail_api_base`，注册流程会在创建邮箱阶段直接失败。",
                "-",
            )
        )
    else:
        try:
            response = _request_with_optional_proxy(
                temp_mail_api_base,
                proxy_url=request_proxy,
                timeout=15,
            )
            ok = response.status_code < 500
            items.append(
                _build_health_item(
                    "temp_mail",
                    "Temp Mail API",
                    ok,
                    f"HTTP {response.status_code}",
                    "接口地址可达。这里只做基础连通性检查，不会真的创建邮箱地址。",
                    temp_mail_api_base,
                )
            )
        except Exception as exc:
            items.append(
                _build_health_item(
                    "temp_mail",
                    "Temp Mail API",
                    False,
                    "接口不可达",
                    f"访问 `{temp_mail_api_base}` 失败：{exc}",
                    temp_mail_api_base,
                )
            )

    xai_proxy = browser_proxy or request_proxy
    try:
        response = _request_with_optional_proxy(
            "https://accounts.x.ai/sign-up?redirect=grok-com",
            proxy_url=xai_proxy,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        ok = response.status_code in {200, 301, 302, 303, 307, 308}
        detail = f"使用 `{_mask_proxy(xai_proxy)}` 访问注册页返回 HTTP {response.status_code}。" if xai_proxy else f"直连访问注册页返回 HTTP {response.status_code}。"
        if not ok and response.status_code in {401, 403, 429}:
            detail += " 这通常说明当前出口被目标站点拦截、限流，或还没完成可用的人机验证链路。"
        items.append(
            _build_health_item(
                "xai",
                "x.ai Sign-up",
                ok,
                f"HTTP {response.status_code}",
                detail,
                "https://accounts.x.ai/sign-up?redirect=grok-com",
            )
        )
    except Exception as exc:
        items.append(
            _build_health_item(
                "xai",
                "x.ai Sign-up",
                False,
                "注册页不可达",
                f"访问 `x.ai` 注册页失败：{exc}",
                "https://accounts.x.ai/sign-up?redirect=grok-com",
            )
        )

    pool = _fetch_grok2api_pool_stats(defaults)
    items.append(
        _build_health_item(
            "pool",
            "Account Pool",
            bool(pool.get("ok")),
            str(pool.get("summary") or "-"),
            str(pool.get("detail") or "-"),
            str(pool.get("target") or "-"),
        )
    )

    return {
        "items": items,
        "checked_at": now_iso(),
        "pool": {
            "total": pool.get("total", 0),
            "providers": pool.get("providers") or {},
            "import_ok": pool.get("import_ok"),
            "import_summary": pool.get("import_summary"),
        },
    }


class TaskCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    count: int = Field(50, ge=1, le=5000)
    proxy: str | None = None
    browser_proxy: str | None = None
    temp_mail_api_base: str | None = None
    temp_mail_admin_password: str | None = None
    temp_mail_domain: str | None = None
    temp_mail_site_password: str | None = None
    api_endpoint: str | None = None
    api_token: str | None = None
    api_append: bool | None = None
    api_import_endpoint: str | None = None
    api_admin_username: str | None = None
    api_admin_password: str | None = None
    notes: str = ""


class SystemSettings(BaseModel):
    proxy: str = ""
    browser_proxy: str = ""
    temp_mail_api_base: str = ""
    temp_mail_admin_password: str = ""
    temp_mail_domain: str = ""
    temp_mail_site_password: str = ""
    api_endpoint: str = ""
    api_token: str = ""
    api_append: bool = True
    api_import_endpoint: str = ""
    api_admin_username: str = "admin"
    api_admin_password: str = ""
    max_concurrent_tasks: int = Field(
        default=DEFAULT_MAX_CONCURRENT_TASKS,
        ge=1,
        le=MAX_CONCURRENT_TASKS_CAP,
    )


class TaskCleanupRequest(BaseModel):
    statuses: list[str] = Field(
        default_factory=lambda: [
            STATUS_COMPLETED,
            STATUS_STOPPED,
            STATUS_FAILED,
            STATUS_PARTIAL,
        ]
    )


class PreflightRequest(BaseModel):
    # Optional overrides; empty means use system defaults.
    proxy: str | None = None
    browser_proxy: str | None = None
    temp_mail_api_base: str | None = None
    temp_mail_admin_password: str | None = None
    temp_mail_domain: str | None = None
    temp_mail_site_password: str | None = None
    api_endpoint: str | None = None
    api_import_endpoint: str | None = None
    api_admin_username: str | None = None
    api_admin_password: str | None = None


ACTIVE_TASK_STATUSES = {STATUS_QUEUED, STATUS_RUNNING, STATUS_STOPPING}
TERMINAL_CLEANUP_STATUSES = {
    STATUS_COMPLETED,
    STATUS_STOPPED,
    STATUS_FAILED,
    STATUS_PARTIAL,
}


@dataclass
class ManagedProcess:
    task_id: int
    process: subprocess.Popen[Any]
    log_handle: Any


def read_settings() -> dict[str, Any]:
    row = fetch_one("SELECT value FROM settings WHERE key = ?", ("system",))
    if not row:
        return {}
    try:
        data = json.loads(row["value"])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_settings(settings: SystemSettings) -> dict[str, Any]:
    data = settings.model_dump()
    previous = read_settings()
    # Empty password means "keep existing" so UI can avoid echoing secrets.
    if not str(data.get("api_admin_password") or "").strip():
        data["api_admin_password"] = str(previous.get("api_admin_password") or "")
    data["max_concurrent_tasks"] = get_max_concurrent_tasks(data)
    execute(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        ("system", json.dumps(data, ensure_ascii=False), now_iso()),
    )
    return data


def get_max_concurrent_tasks(saved: dict[str, Any] | None = None) -> int:
    """Runtime concurrency limit: settings override env default, hard-capped."""
    data = saved if isinstance(saved, dict) else read_settings()
    raw = data.get("max_concurrent_tasks", DEFAULT_MAX_CONCURRENT_TASKS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_CONCURRENT_TASKS
    return max(1, min(value, MAX_CONCURRENT_TASKS_CAP))


def merged_defaults() -> dict[str, Any]:
    base = load_source_defaults()
    saved = read_settings()
    if saved.get("proxy") is not None:
        base["proxy"] = str(saved.get("proxy", ""))
    if saved.get("browser_proxy") is not None:
        base["browser_proxy"] = str(saved.get("browser_proxy", ""))
    for key in ("temp_mail_api_base", "temp_mail_admin_password", "temp_mail_domain", "temp_mail_site_password"):
        if key in saved:
            base[key] = str(saved.get(key, ""))
    api_base = dict(base.get("api") or {})
    if "api_endpoint" in saved:
        api_base["endpoint"] = str(saved.get("api_endpoint", ""))
    if "api_token" in saved and str(saved.get("api_token") or "").strip():
        api_base["token"] = str(saved.get("api_token", "")).strip()
    elif "api_token" in saved and not str(saved.get("api_token") or "").strip():
        # explicit empty means clear legacy token
        api_base["token"] = ""
    if "api_append" in saved:
        api_base["append"] = bool(saved.get("api_append", True))
    if "api_import_endpoint" in saved:
        api_base["import_endpoint"] = str(saved.get("api_import_endpoint", ""))
    if "api_admin_username" in saved:
        api_base["admin_username"] = str(saved.get("api_admin_username", "admin"))
    # Only override env/default password when settings actually stores a non-empty value.
    if str(saved.get("api_admin_password") or "").strip():
        api_base["admin_password"] = str(saved.get("api_admin_password", "")).strip()
    # 再次兜底旧 token-sink endpoint。
    endpoint = str(api_base.get("endpoint", "") or "").strip()
    if endpoint.endswith("/v1/admin/tokens") or "/v1/admin/tokens" in endpoint:
        api_base["endpoint"] = "http://172.18.0.1:8000/api/admin/v1/auth/login"
        api_base.setdefault(
            "import_endpoint",
            "http://172.18.0.1:8000/api/admin/v1/accounts/web/import",
        )
    if not str(api_base.get("import_endpoint", "") or "").strip():
        login_ep = str(api_base.get("endpoint", "") or "").strip()
        if login_ep.endswith("/auth/login"):
            api_base["import_endpoint"] = login_ep.replace("/auth/login", "/accounts/web/import")
    base["api"] = api_base

    # CLIProxyAPI auth 目录：优先容器挂载点 /cliproxy-auths（对应宿主机 CLIProxyAPI/auths）
    cliproxy_env = (
        os.getenv("GROK_REGISTER_DEFAULT_CLIPROXY_AUTH_DIR")
        or os.getenv("CLIPROXYAPI_AUTH_DIR")
        or ""
    ).strip()
    if cliproxy_env:
        base["cliproxy_auth_dir"] = cliproxy_env
    else:
        base.setdefault("cliproxy_auth_dir", "/cliproxy-auths")
    base.setdefault("cliproxy_push_enabled", True)
    base.setdefault("cpa_enabled", False)
    base["max_concurrent_tasks"] = get_max_concurrent_tasks(saved)
    base["max_concurrent_tasks_cap"] = MAX_CONCURRENT_TASKS_CAP

    # YesCaptcha：仅透传环境变量，不写进配置文件明文
    yk = (os.getenv("YESCAPTCHA_API_KEY") or "").strip()
    if yk:
        base["yescaptcha_api_key_configured"] = True
    else:
        base["yescaptcha_api_key_configured"] = False
    return base


def build_task_config(payload: TaskCreate) -> dict[str, Any]:
    defaults = merged_defaults()
    api_defaults = dict(defaults.get("api") or {})
    api_conf = {
        "endpoint": api_defaults.get("endpoint", "") if payload.api_endpoint is None else payload.api_endpoint.strip(),
        "token": api_defaults.get("token", "") if payload.api_token is None else payload.api_token.strip(),
        "append": api_defaults.get("append", True) if payload.api_append is None else bool(payload.api_append),
        "import_endpoint": (
            api_defaults.get("import_endpoint", "")
            if payload.api_import_endpoint is None
            else payload.api_import_endpoint.strip()
        ),
        "admin_username": (
            api_defaults.get("admin_username", "admin")
            if payload.api_admin_username is None
            else payload.api_admin_username.strip()
        ),
        "admin_password": (
            api_defaults.get("admin_password", "")
            if payload.api_admin_password is None
            else payload.api_admin_password.strip()
        ),
    }
    if not str(api_conf.get("import_endpoint", "") or "").strip():
        login_ep = str(api_conf.get("endpoint", "") or "").strip()
        if login_ep.endswith("/auth/login"):
            api_conf["import_endpoint"] = login_ep.replace("/auth/login", "/accounts/web/import")
    return {
        "run": {"count": int(payload.count)},
        "proxy": defaults.get("proxy", "") if payload.proxy is None else payload.proxy.strip(),
        "browser_proxy": defaults.get("browser_proxy", "") if payload.browser_proxy is None else payload.browser_proxy.strip(),
        "temp_mail_api_base": defaults.get("temp_mail_api_base", "") if payload.temp_mail_api_base is None else payload.temp_mail_api_base.strip(),
        "temp_mail_admin_password": defaults.get("temp_mail_admin_password", "") if payload.temp_mail_admin_password is None else payload.temp_mail_admin_password.strip(),
        "temp_mail_domain": defaults.get("temp_mail_domain", "") if payload.temp_mail_domain is None else payload.temp_mail_domain.strip(),
        "temp_mail_site_password": defaults.get("temp_mail_site_password", "") if payload.temp_mail_site_password is None else payload.temp_mail_site_password.strip(),
        "api": api_conf,
        # Build OAuth / CLIProxy 输出目录（容器内路径）
        "cliproxy_auth_dir": defaults.get("cliproxy_auth_dir", "/cliproxy-auths"),
        "cliproxy_push_enabled": bool(defaults.get("cliproxy_push_enabled", True)),
        # grok.com /rest/app/mint 已 404；默认关闭旧 CPA mint，改由 Build OAuth 产出 auth。
        "cpa_enabled": bool(defaults.get("cpa_enabled", False)),
        "cpa_local_dir": defaults.get("cpa_local_dir", "./output/cpa_auths"),
        "cpa_proxy": defaults.get("cpa_proxy", ""),
    }



def public_defaults() -> dict[str, Any]:
    """Defaults safe for browser bootstrap: mask secrets, keep configured flags."""
    data = merged_defaults()
    api = dict(data.get("api") or {})
    admin_password = str(api.get("admin_password") or "")
    api["admin_password_configured"] = bool(admin_password.strip())
    api["admin_password"] = ""
    if api.get("token"):
        api["token_configured"] = True
        # keep token visible only if already used as non-secret legacy field; still blank for safety
        api["token"] = ""
    else:
        api["token_configured"] = False
    data["api"] = api
    if data.get("temp_mail_admin_password"):
        data["temp_mail_admin_password_configured"] = True
        data["temp_mail_admin_password"] = ""
    if data.get("temp_mail_site_password"):
        data["temp_mail_site_password_configured"] = True
        data["temp_mail_site_password"] = ""
    return data


def serialize_task(row: sqlite3.Row) -> dict[str, Any]:
    console_path = Path(row["console_path"]) if row["console_path"] else None
    push = {
        "pushed_count": 0,
        "pushed_created": 0,
        "pushed_updated": 0,
        "push_events": 0,
    }
    if console_path is not None and console_path.exists():
        parsed = parse_console_state(console_path)
        push = {
            "pushed_count": int(parsed.get("pushed_count") or 0),
            "pushed_created": int(parsed.get("pushed_created") or 0),
            "pushed_updated": int(parsed.get("pushed_updated") or 0),
            "push_events": int(parsed.get("push_events") or 0),
        }
    completed = int(row["completed_count"])
    failed = int(row["failed_count"])
    pushed = int(push.get("pushed_count") or 0)
    push_gap = max(0, completed - pushed)
    last_error = row["last_error"] or ""
    error_summary = " ".join(str(last_error).split())
    if len(error_summary) > 120:
        error_summary = error_summary[:117] + "..."
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "status": row["status"],
        "target_count": int(row["target_count"]),
        "completed_count": completed,
        "failed_count": failed,
        "current_round": int(row["current_round"]),
        "current_phase": row["current_phase"] or "",
        "last_email": row["last_email"] or "",
        "last_error": last_error,
        "error_summary": error_summary,
        "push_gap": push_gap,
        "has_push_gap": push_gap > 0,
        "last_log_at": row["last_log_at"] or "",
        "notes": row["notes"] or "",
        "config": json.loads(row["config_json"]),
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "exit_code": row["exit_code"],
        "pid": row["pid"],
        **push,
    }


def read_log_lines(path: Path, limit: int = 200) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-limit:]


def parse_console_state(console_path: Path) -> dict[str, Any]:
    state = {
        "completed_count": 0,
        "failed_count": 0,
        "current_round": 0,
        "current_phase": "",
        "last_email": "",
        "last_error": "",
        "last_log_at": now_iso(),
        "pushed_count": 0,
        "pushed_created": 0,
        "pushed_updated": 0,
        "push_events": 0,
    }
    if not console_path.exists():
        return state

    lines = console_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return state

    interesting = (
        "开始第",
        "临时邮箱创建成功",
        "已填写邮箱并点击注册",
        "提取到验证码",
        "已填写验证码",
        "最终注册页",
        "Turnstile",
        "已填写注册资料并点击完成注册",
        "注册成功",
        "[Error]",
        "已推送到 API",
        "已推送到 grok2api",
        "推送",
    )

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if m := LINE_RE_ROUND.search(line):
            state["current_round"] = int(m.group(1))
            state["current_phase"] = "starting_round"
        if m := LINE_RE_SUCCESS.search(line):
            state["completed_count"] += 1
            state["last_email"] = m.group(1)
            state["current_phase"] = "success"
        if m := LINE_RE_ERROR.search(line):
            state["failed_count"] += 1
            state["last_error"] = m.group(2).strip()
            state["current_phase"] = "error"
        if m := LINE_RE_TEMP_EMAIL.search(line):
            state["last_email"] = m.group(1)
            state["current_phase"] = "mailbox_created"
        if m := LINE_RE_FILLED_EMAIL.search(line):
            state["last_email"] = m.group(1)
            state["current_phase"] = "email_submitted"
        if "提取到验证码" in line:
            state["current_phase"] = "otp_received"
        if "最终注册页" in line:
            state["current_phase"] = "profile_page"
        if "Turnstile 响应已同步" in line:
            state["current_phase"] = "turnstile_solved"
        if "已填写注册资料并点击完成注册" in line:
            state["current_phase"] = "submitting_profile"
        if m := LINE_RE_PUSH_STATS.search(line):
            created = int(m.group(1))
            updated = int(m.group(2))
            total = int(m.group(3))
            state["pushed_created"] += created
            state["pushed_updated"] += updated
            state["pushed_count"] += total
            state["push_events"] += 1
            state["current_phase"] = "pushed_to_api"
        elif m := LINE_RE_PUSH_COUNT.search(line):
            state["pushed_count"] += int(m.group(1))
            state["push_events"] += 1
            state["current_phase"] = "pushed_to_api"
        elif LINE_RE_PUSH.search(line):
            # Fallback for older log formats without counts.
            state["push_events"] += 1
            state["pushed_count"] = max(state["pushed_count"], state["completed_count"])
            state["current_phase"] = "pushed_to_api"
        if any(token in line for token in interesting):
            state["last_log_at"] = now_iso()
    return state


def task_row(task_id: int) -> sqlite3.Row:
    row = fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return row


def delete_task_files(row: sqlite3.Row) -> None:
    task_dir = Path(row["task_dir"])
    if task_dir.exists() and task_dir.is_dir():
        shutil.rmtree(task_dir, ignore_errors=True)


def copy_source_to_task_dir(task_dir: Path, task_config: dict[str, Any]) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    for file_name in PROJECT_FILES:
        shutil.copy2(SOURCE_PROJECT / file_name, task_dir / file_name)
    for dir_name in PROJECT_DIRS:
        src = SOURCE_PROJECT / dir_name
        dst = task_dir / dir_name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    (task_dir / "logs").mkdir(exist_ok=True)
    (task_dir / "sso").mkdir(exist_ok=True)
    (task_dir / "config.json").write_text(
        json.dumps(task_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class TaskSupervisor:
    def __init__(self) -> None:
        self._processes: dict[int, ManagedProcess] = {}
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._stop = threading.Event()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def stop_task(self, task_id: int) -> None:
        managed = self._processes.get(task_id)
        if not managed:
            row = task_row(task_id)
            if row["status"] == STATUS_QUEUED:
                execute_no_return(
                    """
                    UPDATE tasks
                    SET status = ?, finished_at = ?, last_error = ?
                    WHERE id = ?
                    """,
                    (STATUS_STOPPED, now_iso(), "Task stopped before launch.", task_id),
                )
                return
            raise HTTPException(status_code=409, detail="Task is not running")
        execute_no_return(
            "UPDATE tasks SET status = ?, last_error = ?, current_phase = ? WHERE id = ?",
            (STATUS_STOPPING, "Stopping task...", STATUS_STOPPING, task_id),
        )
        try:
            os.killpg(managed.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def _running_count(self) -> int:
        return len(self._processes)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_running()
                self._launch_queued()
            except Exception:
                pass
            time.sleep(SUPERVISOR_INTERVAL)

    def _launch_queued(self) -> None:
        slots = get_max_concurrent_tasks() - self._running_count()
        if slots <= 0:
            return
        queued = fetch_all(
            "SELECT * FROM tasks WHERE status = ? ORDER BY id ASC LIMIT ?",
            (STATUS_QUEUED, slots),
        )
        for row in queued:
            self._start_task(row)

    def _start_task(self, row: sqlite3.Row) -> None:
        task_id = int(row["id"])
        task_dir = Path(row["task_dir"])
        console_path = Path(row["console_path"])
        task_config = json.loads(row["config_json"])
        copy_source_to_task_dir(task_dir, task_config)

        output_path = task_dir / "sso" / f"task_{task_id}.txt"
        command = [
            str(SOURCE_VENV_PYTHON),
            str(task_dir / "DrissionPage_example.py"),
            "--count",
            str(int(row["target_count"])),
            "--output",
            str(output_path),
        ]
        log_handle = console_path.open("a", encoding="utf-8")
        # 任务 cwd 是 task_dir，需把仓库根目录加入 PYTHONPATH，
        # 否则无法 import 根目录下的 xconsole_client。
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = (
            f"{SOURCE_PROJECT}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else str(SOURCE_PROJECT)
        )
        # Build OAuth / password CreateSession 兜底依赖
        cliproxy_dir = str((task_config or {}).get("cliproxy_auth_dir") or "").strip()
        if cliproxy_dir:
            env["CLIPROXYAPI_AUTH_DIR"] = cliproxy_dir
        # 保留宿主机/容器注入的 YESCAPTCHA_API_KEY（若有）
        if not (env.get("YESCAPTCHA_API_KEY") or "").strip():
            # 也允许从 config 透传（不推荐，但兼容）
            yk = str((task_config or {}).get("yescaptcha_api_key") or "").strip()
            if yk:
                env["YESCAPTCHA_API_KEY"] = yk
        process = subprocess.Popen(
            command,
            cwd=task_dir,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
        self._processes[task_id] = ManagedProcess(task_id=task_id, process=process, log_handle=log_handle)
        execute_no_return(
            """
            UPDATE tasks
            SET status = ?, pid = ?, started_at = ?, current_phase = ?, last_log_at = ?
            WHERE id = ?
            """,
            (STATUS_RUNNING, process.pid, now_iso(), "process_started", now_iso(), task_id),
        )

    def _refresh_running(self) -> None:
        finished: list[int] = []
        for task_id, managed in list(self._processes.items()):
            row = task_row(task_id)
            console_path = Path(row["console_path"])
            parsed = parse_console_state(console_path)
            execute_no_return(
                """
                UPDATE tasks
                SET completed_count = ?, failed_count = ?, current_round = ?, current_phase = ?,
                    last_email = ?, last_error = ?, last_log_at = ?
                WHERE id = ?
                """,
                (
                    parsed["completed_count"],
                    parsed["failed_count"],
                    parsed["current_round"],
                    parsed["current_phase"],
                    parsed["last_email"],
                    parsed["last_error"],
                    parsed["last_log_at"],
                    task_id,
                ),
            )
            exit_code = managed.process.poll()
            if exit_code is None:
                continue
            final_status = STATUS_FAILED
            if row["status"] == STATUS_STOPPING or exit_code in (-15, -9):
                final_status = STATUS_STOPPED
            elif parsed["completed_count"] >= int(row["target_count"]) and exit_code == 0:
                final_status = STATUS_COMPLETED
            elif parsed["completed_count"] > 0:
                final_status = STATUS_PARTIAL
            execute_no_return(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?, exit_code = ?,
                    completed_count = ?, failed_count = ?, current_round = ?, current_phase = ?,
                    last_email = ?, last_error = ?, last_log_at = ?
                WHERE id = ?
                """,
                (
                    final_status,
                    now_iso(),
                    exit_code,
                    parsed["completed_count"],
                    parsed["failed_count"],
                    parsed["current_round"],
                    parsed["current_phase"] or final_status,
                    parsed["last_email"],
                    parsed["last_error"],
                    parsed["last_log_at"],
                    task_id,
                ),
            )
            finished.append(task_id)
        for task_id in finished:
            managed = self._processes.pop(task_id, None)
            if managed and managed.log_handle:
                managed.log_handle.close()


supervisor = TaskSupervisor()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    supervisor.start()
    try:
        yield
    finally:
        supervisor.stop()


app = FastAPI(title="Grok Register Console", lifespan=lifespan, root_path=ROOT_PATH)

@app.middleware("http")
async def console_auth_middleware(request: Request, call_next):
    try:
        accepted_token = require_console_auth(request)
    except HTTPException as exc:
        from fastapi.responses import JSONResponse, HTMLResponse

        # Browser HTML visits get a tiny unlock page instead of bare JSON 401.
        accept = (request.headers.get("accept") or "").lower()
        wants_html = "text/html" in accept and "application/json" not in accept
        if wants_html and request.method == "GET":
            body = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Register Console Auth</title>
<style>body{font-family:system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;line-height:1.6}
input,button{font:inherit;padding:10px 12px;border-radius:10px;border:1px solid #ccc}
button{background:#222;color:#fff;border:0;cursor:pointer}</style></head>
<body>
  <h1>控制台需要鉴权</h1>
  <p>当前开启了访问保护。请输入访问 token，或用 <code>?token=你的token</code> 打开。</p>
  <form id="f"><input id="t" name="token" placeholder="console auth token" style="width:100%;margin:8px 0" required>
  <button type="submit">进入控制台</button></form>
  <script>
    document.getElementById('f').addEventListener('submit', function (e) {
      e.preventDefault();
      var token = document.getElementById('t').value.trim();
      if (!token) return;
      var url = new URL(window.location.href);
      url.searchParams.set('token', token);
      window.location.href = url.toString();
    });
  </script>
</body></html>"""
            return HTMLResponse(status_code=401, content=body, headers=getattr(exc, "headers", None) or {})
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None) or {},
        )

    response = await call_next(request)
    # Persist token for subsequent static/API requests after ?token= unlock.
    # Always use path="/" so both direct :18600 and nginx /register work.
    if getattr(request.state, "console_set_auth_cookie", False) and accepted_token:
        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=accepted_token,
            httponly=True,
            samesite="lax",
            path="/",
            max_age=60 * 60 * 24 * 30,
        )
    return response

STATIC_DIR = APP_DIR / "static"


@app.get("/static/{asset_path:path}")
def static_asset(asset_path: str) -> FileResponse:
    # Explicit static route: more reliable than StaticFiles mount under reverse-proxy root_path.
    target = (STATIC_DIR / asset_path).resolve()
    if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(target)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    page_token = ""
    if CONSOLE_AUTH_TOKEN:
        candidate = _extract_console_token(request)
        if candidate == CONSOLE_AUTH_TOKEN:
            page_token = candidate
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "defaults": json.dumps(public_defaults(), ensure_ascii=False),
            "max_concurrent_tasks": get_max_concurrent_tasks(),
            "max_concurrent_tasks_cap": MAX_CONCURRENT_TASKS_CAP,
            "source_project": str(SOURCE_PROJECT),
            "base_path": ROOT_PATH,
            "page_token": page_token,
        },
    )


@app.get("/api/meta")
def api_meta() -> dict[str, Any]:
    settings = read_settings()
    safe_settings = dict(settings)
    if safe_settings.get("api_admin_password"):
        safe_settings["api_admin_password"] = ""
        safe_settings["api_admin_password_configured"] = True
    if safe_settings.get("api_token"):
        safe_settings["api_token"] = ""
        safe_settings["api_token_configured"] = True
    return {
        "defaults": public_defaults(),
        "settings": safe_settings,
        "source_project": str(SOURCE_PROJECT),
        "python_path": str(SOURCE_VENV_PYTHON),
        "max_concurrent_tasks": get_max_concurrent_tasks(),
        "max_concurrent_tasks_cap": MAX_CONCURRENT_TASKS_CAP,
        "auth_enabled": console_auth_enabled(),
    }


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return run_health_checks()


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    settings = read_settings()
    safe_settings = dict(settings)
    if safe_settings.get("api_admin_password"):
        safe_settings["api_admin_password"] = ""
        safe_settings["api_admin_password_configured"] = True
    if safe_settings.get("api_token"):
        safe_settings["api_token"] = ""
        safe_settings["api_token_configured"] = True
    return {"settings": safe_settings, "defaults": public_defaults()}


@app.post("/api/settings")
def save_settings(payload: SystemSettings) -> dict[str, Any]:
    saved = write_settings(payload)
    defaults = public_defaults()
    safe_settings = dict(saved)
    if safe_settings.get("api_admin_password"):
        safe_settings["api_admin_password"] = ""
        safe_settings["api_admin_password_configured"] = True
    if safe_settings.get("api_token"):
        safe_settings["api_token"] = ""
        safe_settings["api_token_configured"] = True
    return {
        "settings": safe_settings,
        "defaults": defaults,
        "max_concurrent_tasks": defaults.get("max_concurrent_tasks", get_max_concurrent_tasks()),
        "max_concurrent_tasks_cap": MAX_CONCURRENT_TASKS_CAP,
    }


@app.get("/api/tasks")
def list_tasks() -> dict[str, Any]:
    rows = fetch_all("SELECT * FROM tasks ORDER BY id DESC")
    return {"tasks": [serialize_task(row) for row in rows]}


@app.post("/api/tasks")
def create_task(payload: TaskCreate) -> dict[str, Any]:
    if not SOURCE_PROJECT.exists():
        raise HTTPException(status_code=500, detail=f"Source project not found: {SOURCE_PROJECT}")
    if not SOURCE_VENV_PYTHON.exists():
        raise HTTPException(status_code=500, detail=f"Python not found: {SOURCE_VENV_PYTHON}")
    task_config = build_task_config(payload)
    created_at = now_iso()
    task_id = execute(
        """
        INSERT INTO tasks (
            name, status, target_count, notes, config_json, task_dir, console_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.name.strip(),
            STATUS_QUEUED,
            payload.count,
            payload.notes.strip(),
            json.dumps(task_config, ensure_ascii=False),
            str(TASKS_DIR / "pending"),
            str(TASKS_DIR / "pending.log"),
            created_at,
        ),
    )
    task_dir = TASKS_DIR / f"task_{task_id}"
    console_path = task_dir / "console.log"
    task_dir.mkdir(parents=True, exist_ok=True)
    execute_no_return(
        "UPDATE tasks SET task_dir = ?, console_path = ? WHERE id = ?",
        (str(task_dir), str(console_path), task_id),
    )
    return {"task": serialize_task(task_row(task_id))}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int) -> dict[str, Any]:
    return {"task": serialize_task(task_row(task_id))}


@app.get("/api/tasks/{task_id}/logs")
def get_task_logs(task_id: int, limit: int = Query(200, ge=20, le=1000)) -> dict[str, Any]:
    row = task_row(task_id)
    console_path = Path(row["console_path"])
    return {"lines": read_log_lines(console_path, limit=limit)}


@app.post("/api/tasks/{task_id}/stop")
def stop_task(task_id: int) -> dict[str, Any]:
    supervisor.stop_task(task_id)
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int) -> dict[str, Any]:
    row = task_row(task_id)
    managed = supervisor._processes.get(task_id)
    if managed and managed.process.poll() is None:
        raise HTTPException(status_code=409, detail="Task is still running")
    if row["status"] in ACTIVE_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="Task is still active")
    delete_task_files(row)
    execute_no_return("DELETE FROM tasks WHERE id = ?", (task_id,))
    return {"ok": True}


@app.post("/api/tasks/cleanup")
def cleanup_tasks(payload: TaskCleanupRequest) -> dict[str, Any]:
    statuses = []
    for status in payload.statuses or []:
        value = str(status or "").strip().lower()
        if not value:
            continue
        if value in ACTIVE_TASK_STATUSES:
            raise HTTPException(status_code=400, detail=f"Cannot cleanup active status: {value}")
        if value not in TERMINAL_CLEANUP_STATUSES:
            raise HTTPException(status_code=400, detail=f"Unsupported cleanup status: {value}")
        if value not in statuses:
            statuses.append(value)
    if not statuses:
        raise HTTPException(status_code=400, detail="statuses is required")

    placeholders = ", ".join("?" for _ in statuses)
    rows = fetch_all(
        f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY id DESC",
        tuple(statuses),
    )
    deleted_ids: list[int] = []
    skipped_ids: list[int] = []
    for row in rows:
        task_id = int(row["id"])
        managed = supervisor._processes.get(task_id)
        if managed and managed.process.poll() is None:
            skipped_ids.append(task_id)
            continue
        if row["status"] in ACTIVE_TASK_STATUSES:
            skipped_ids.append(task_id)
            continue
        delete_task_files(row)
        execute_no_return("DELETE FROM tasks WHERE id = ?", (task_id,))
        deleted_ids.append(task_id)
    return {
        "ok": True,
        "deleted_count": len(deleted_ids),
        "deleted_ids": deleted_ids,
        "skipped_ids": skipped_ids,
        "statuses": statuses,
    }


def run_preflight(payload: PreflightRequest | None = None) -> dict[str, Any]:
    """Lightweight create-time checks using current defaults with optional overrides."""
    defaults = merged_defaults()
    payload = payload or PreflightRequest()
    overrides = payload.model_dump(exclude_none=True)

    def pick(key: str, nested_api_key: str | None = None) -> str:
        if key in overrides and overrides[key] is not None and str(overrides[key]).strip() != "":
            return str(overrides[key]).strip()
        if nested_api_key:
            return str((defaults.get("api") or {}).get(nested_api_key) or "").strip()
        return str(defaults.get(key) or "").strip()

    browser_proxy = pick("browser_proxy")
    request_proxy = pick("proxy")
    temp_mail_api_base = pick("temp_mail_api_base")
    temp_mail_admin_password = pick("temp_mail_admin_password")
    temp_mail_domain = pick("temp_mail_domain")
    api_endpoint = pick("api_endpoint", "endpoint")
    api_import_endpoint = pick("api_import_endpoint", "import_endpoint")
    api_admin_username = pick("api_admin_username", "admin_username") or "admin"
    api_admin_password = pick("api_admin_password", "admin_password")

    # Build a temporary defaults-like object for pool helper reuse.
    temp_defaults = {
        **defaults,
        "proxy": request_proxy,
        "browser_proxy": browser_proxy,
        "temp_mail_api_base": temp_mail_api_base,
        "temp_mail_admin_password": temp_mail_admin_password,
        "temp_mail_domain": temp_mail_domain,
        "api": {
            **dict(defaults.get("api") or {}),
            "endpoint": api_endpoint,
            "import_endpoint": api_import_endpoint,
            "admin_username": api_admin_username,
            "admin_password": api_admin_password,
        },
    }

    items: list[dict[str, Any]] = []

    # Browser proxy / WARP-ish reachability via existing health style.
    if browser_proxy:
        try:
            # Treat configured browser proxy as present; deeper probe is expensive.
            items.append(
                _build_health_item(
                    "browser_proxy",
                    "浏览器代理",
                    True,
                    "已配置",
                    _mask_proxy(browser_proxy),
                    browser_proxy,
                )
            )
        except Exception as exc:
            items.append(
                _build_health_item(
                    "browser_proxy",
                    "浏览器代理",
                    False,
                    "配置异常",
                    str(exc),
                    browser_proxy or "-",
                )
            )
    else:
        items.append(
            _build_health_item(
                "browser_proxy",
                "浏览器代理",
                False,
                "未配置",
                "创建任务前建议配置 browser_proxy",
                "-",
            )
        )

    # Temp mail
    if not temp_mail_api_base:
        items.append(
            _build_health_item(
                "temp_mail",
                "临时邮箱",
                False,
                "未配置 API Base",
                "缺少 temp_mail_api_base",
                "-",
            )
        )
    else:
        try:
            probe_url = temp_mail_api_base.rstrip("/") + "/"
            resp = _request_with_optional_proxy(probe_url, proxy_url=request_proxy, method="GET", timeout=12)
            # 2xx/3xx = good; 401/403 often means endpoint reachable but auth/domain issue.
            ok = 200 <= resp.status_code < 400
            summary = f"HTTP {resp.status_code}"
            if resp.status_code in {401, 403}:
                summary = f"HTTP {resp.status_code}（可达但鉴权/权限异常）"
            items.append(
                _build_health_item(
                    "temp_mail",
                    "临时邮箱",
                    ok,
                    summary,
                    f"domain={temp_mail_domain or '-'} admin={'set' if temp_mail_admin_password else 'empty'}",
                    temp_mail_api_base,
                )
            )
        except Exception as exc:
            items.append(
                _build_health_item(
                    "temp_mail",
                    "临时邮箱",
                    False,
                    "不可达",
                    str(exc),
                    temp_mail_api_base,
                )
            )

    # Admin login + import via pool helper
    pool = _fetch_grok2api_pool_stats(temp_defaults)
    items.append(
        _build_health_item(
            "grok2api",
            "Admin Login / 号池",
            bool(pool.get("ok")),
            str(pool.get("summary") or "-"),
            str(pool.get("detail") or "-"),
            str(pool.get("target") or api_endpoint or "-"),
        )
    )
    import_ok = bool(pool.get("import_ok")) if "import_ok" in pool else bool(api_import_endpoint)
    import_summary = str(pool.get("import_summary") or ("未配置 import" if not api_import_endpoint else "未知"))
    items.append(
        _build_health_item(
            "import",
            "Import 入池",
            bool(api_import_endpoint) and (import_ok or bool(pool.get("ok"))),
            import_summary if api_import_endpoint else "未配置",
            "注册成功后 SSO 会推送到此接口",
            api_import_endpoint or "-",
        )
    )

    # x.ai quick check (optional soft signal)
    try:
        xai_resp = _request_with_optional_proxy(
            "https://accounts.x.ai/",
            proxy_url=browser_proxy or request_proxy,
            method="GET",
            timeout=12,
        )
        items.append(
            _build_health_item(
                "xai",
                "x.ai",
                xai_resp.status_code < 500,
                f"HTTP {xai_resp.status_code}",
                "注册页连通性探测",
                "https://accounts.x.ai/",
            )
        )
    except Exception as exc:
        items.append(
            _build_health_item(
                "xai",
                "x.ai",
                False,
                "不可达",
                str(exc),
                "https://accounts.x.ai/",
            )
        )

    ok = all(bool(item.get("ok")) for item in items)
    return {
        "ok": ok,
        "checked_at": now_iso(),
        "items": items,
        "pool": {
            "total": pool.get("total", 0),
            "providers": pool.get("providers") or {},
            "import_ok": pool.get("import_ok"),
            "import_summary": pool.get("import_summary"),
        },
        "blocking": [item for item in items if not item.get("ok")],
    }


@app.post("/api/preflight")
def api_preflight(payload: PreflightRequest | None = None) -> dict[str, Any]:
    return run_preflight(payload)


@app.get("/api/tasks/{task_id}/logs/download")
def download_task_logs(task_id: int, limit: int = Query(5000, ge=100, le=20000)) -> dict[str, Any]:
    row = task_row(task_id)
    console_path = Path(row["console_path"])
    lines = read_log_lines(console_path, limit=limit)
    filename = f"task-{task_id}-{(row['name'] or 'task').replace('/', '_')}.log"
    return {
        "filename": filename,
        "lines": lines,
        "count": len(lines),
        "task_id": task_id,
        "task_name": row["name"],
    }


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("GROK_REGISTER_CONSOLE_HOST", "127.0.0.1")
    port = int(os.getenv("GROK_REGISTER_CONSOLE_PORT", "18600"))
    uvicorn.run("app:app", host=host, port=port, reload=False)
