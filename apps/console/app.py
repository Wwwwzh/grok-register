from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
from queue import Empty, Queue
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
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

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                level TEXT NOT NULL,
                event TEXT NOT NULL,
                task_id INTEGER,
                message TEXT NOT NULL,
                detail_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_task_id ON audit_logs(task_id);
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

    checked_at = now_iso()
    # Keep a lightweight local history for pool trend charts.
    try:
        record_pool_snapshot(
            {
                "ok": bool(pool.get("ok")),
                "total": pool.get("total", 0),
                "providers": pool.get("providers") or {},
            },
            checked_at=checked_at,
        )
    except Exception:
        # History must never break health checks.
        pass
    return {
        "items": items,
        "checked_at": checked_at,
        "pool": {
            "total": pool.get("total", 0),
            "providers": pool.get("providers") or {},
            "import_ok": pool.get("import_ok"),
            "import_summary": pool.get("import_summary"),
        },
        "pool_trend": build_pool_trend(limit=120, range_key="6h"),
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


class TaskTemplate(BaseModel):
    id: str | None = None
    name: str = Field(..., min_length=1, max_length=80)
    count: int = Field(50, ge=1, le=5000)
    proxy: str = ""
    browser_proxy: str = ""
    temp_mail_api_base: str = ""
    temp_mail_admin_password: str = ""
    temp_mail_domain: str = ""
    temp_mail_site_password: str = ""
    api_endpoint: str = ""
    api_import_endpoint: str = ""
    api_admin_username: str = ""
    api_admin_password: str = ""
    api_token: str = ""
    api_append: bool | None = None
    notes: str = ""


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


AUDIT_LOG_MAX = 500


def write_audit_log(
    event: str,
    message: str,
    *,
    level: str = "info",
    task_id: int | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    level = (level or "info").strip().lower() or "info"
    if level not in {"info", "success", "warn", "error"}:
        level = "info"
    created_at = now_iso()
    detail_json = json.dumps(detail or {}, ensure_ascii=False)
    log_id = execute(
        """
        INSERT INTO audit_logs (created_at, level, event, task_id, message, detail_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (created_at, level, event, task_id, message, detail_json),
    )
    # Keep table bounded.
    try:
        execute_no_return(
            """
            DELETE FROM audit_logs
            WHERE id NOT IN (
                SELECT id FROM audit_logs ORDER BY id DESC LIMIT ?
            )
            """,
            (AUDIT_LOG_MAX,),
        )
    except Exception:
        pass
    item = {
        "id": int(log_id),
        "created_at": created_at,
        "level": level,
        "event": event,
        "task_id": task_id,
        "message": message,
        "detail": detail or {},
    }
    try:
        sse_hub.publish("audit", item)
    except Exception:
        pass
    return item


def list_audit_logs(
    limit: int = 50,
    task_id: int | None = None,
    level: str | None = None,
    event: str | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 200))
    clauses: list[str] = []
    params: list[Any] = []

    if task_id is not None:
        clauses.append("task_id = ?")
        params.append(int(task_id))

    level_norm = str(level or "").strip().lower()
    if level_norm:
        if level_norm not in {"info", "success", "warn", "error"}:
            level_norm = ""
        else:
            clauses.append("level = ?")
            params.append(level_norm)

    event_norm = str(event or "").strip()
    if event_norm:
        # exact match when no wildcard; support prefix* and contains via %...%
        if event_norm.endswith("*") and "%" not in event_norm:
            clauses.append("event LIKE ?")
            params.append(event_norm[:-1] + "%")
        elif "%" in event_norm or "_" in event_norm:
            clauses.append("event LIKE ?")
            params.append(event_norm)
        else:
            clauses.append("event = ?")
            params.append(event_norm)

    q_norm = str(q or "").strip()
    if q_norm:
        like = f"%{q_norm}%"
        clauses.append("(message LIKE ? OR event LIKE ? OR IFNULL(CAST(task_id AS TEXT), '') LIKE ?)")
        params.extend([like, like, like])

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = fetch_all(
        f"""
        SELECT * FROM audit_logs
        {where_sql}
        ORDER BY id DESC
        LIMIT ?
        """,
        (*params, limit),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        detail = {}
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except Exception:
            detail = {}
        out.append(
            {
                "id": int(row["id"]),
                "created_at": row["created_at"],
                "level": row["level"],
                "event": row["event"],
                "task_id": row["task_id"],
                "message": row["message"] or "",
                "detail": detail if isinstance(detail, dict) else {},
            }
        )
    return out


def list_audit_event_names(limit: int = 40) -> list[str]:
    """Distinct recent event names for filter dropdowns."""
    limit = max(1, min(int(limit or 40), 100))
    rows = fetch_all(
        """
        SELECT event, MAX(id) AS mid
        FROM audit_logs
        WHERE event IS NOT NULL AND TRIM(event) != ''
        GROUP BY event
        ORDER BY mid DESC
        LIMIT ?
        """,
        (limit,),
    )
    out: list[str] = []
    for row in rows:
        name = str(row["event"] or "").strip()
        if name and name not in out:
            out.append(name)
    return out


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


def read_templates() -> list[dict[str, Any]]:
    row = fetch_one("SELECT value FROM settings WHERE key = ?", ("templates",))
    if not row:
        return []
    try:
        data = json.loads(row["value"])
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            out.append(item)
    return out


def write_templates(templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = json.dumps(templates, ensure_ascii=False)
    execute_no_return(
        """
        INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        ("templates", payload, now_iso()),
    )
    return templates


POOL_HISTORY_KEY = "pool_history"
POOL_HISTORY_MAX_POINTS = 288  # ~24h if sampled every 5 min; also used with health refresh cadence
POOL_HISTORY_MIN_INTERVAL_SEC = 60


def read_pool_history() -> list[dict[str, Any]]:
    row = fetch_one("SELECT value FROM settings WHERE key = ?", (POOL_HISTORY_KEY,))
    if not row:
        return []
    try:
        data = json.loads(row["value"])
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict) and item.get("ts"):
            out.append(item)
    return out


def write_pool_history(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = json.dumps(points[-POOL_HISTORY_MAX_POINTS:], ensure_ascii=False)
    execute_no_return(
        """
        INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (POOL_HISTORY_KEY, payload, now_iso()),
    )
    return points[-POOL_HISTORY_MAX_POINTS:]


def _parse_iso_ts(value: str) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        # Support "YYYY-MM-DD HH:MM:SS" and ISO with T/Z.
        cleaned = raw.replace("Z", "+00:00").replace(" ", "T")
        dt = datetime.fromisoformat(cleaned)
        return dt.timestamp()
    except Exception:
        return None


def record_pool_snapshot(pool: dict[str, Any], checked_at: str | None = None) -> dict[str, Any]:
    """Append a pool snapshot if enough time has passed since the last sample."""
    ts = checked_at or now_iso()
    providers = dict(pool.get("providers") or {})
    point = {
        "ts": ts,
        "ok": bool(pool.get("ok")),
        "total": int(pool.get("total") or 0),
        "grok_web": int(providers.get("grok_web") or 0),
        "grok_build": int(providers.get("grok_build") or 0),
        "grok_console": int(providers.get("grok_console") or 0),
    }
    history = read_pool_history()
    if history:
        last_ts = _parse_iso_ts(str(history[-1].get("ts") or ""))
        cur_ts = _parse_iso_ts(ts)
        if last_ts is not None and cur_ts is not None and (cur_ts - last_ts) < POOL_HISTORY_MIN_INTERVAL_SEC:
            # Update the last point in-place so UI still sees freshest totals without exploding history.
            history[-1] = point
            write_pool_history(history)
            return point
    history.append(point)
    write_pool_history(history)
    return point


POOL_TREND_RANGES = {
    "1h": 1 * 60 * 60,
    "6h": 6 * 60 * 60,
    "24h": 24 * 60 * 60,
}


def normalize_pool_range(range_key: str | None = None) -> str:
    key = str(range_key or "6h").strip().lower()
    if key in POOL_TREND_RANGES:
        return key
    # accept aliases
    aliases = {
        "1": "1h",
        "60m": "1h",
        "hour": "1h",
        "6": "6h",
        "24": "24h",
        "day": "24h",
        "1d": "24h",
    }
    return aliases.get(key, "6h")


def build_pool_trend(limit: int = 72, range_key: str | None = None) -> dict[str, Any]:
    history = read_pool_history()
    range_name = normalize_pool_range(range_key)
    range_sec = POOL_TREND_RANGES[range_name]
    now_ts = datetime.now().timestamp()
    cutoff = now_ts - range_sec

    timed_points: list[dict[str, Any]] = []
    for item in history:
        ts = _parse_iso_ts(str(item.get("ts") or ""))
        if ts is None:
            continue
        if ts >= cutoff:
            timed_points.append(item)

    # Fallback: if timestamps are sparse/missing, keep last N by range heuristic.
    if not timed_points and history:
        # Rough sample count fallback when old points lack parseable timestamps.
        heuristic = {
            "1h": 60,
            "6h": 120,
            "24h": POOL_HISTORY_MAX_POINTS,
        }.get(range_name, 72)
        timed_points = history[-max(1, min(heuristic, POOL_HISTORY_MAX_POINTS)):]

    # Soft cap after time filter so response stays light.
    max_points = max(1, min(int(limit or POOL_HISTORY_MAX_POINTS), POOL_HISTORY_MAX_POINTS))
    points = timed_points[-max_points:]
    if not points:
        return {
            "points": [],
            "latest": None,
            "delta": {"total": 0, "grok_web": 0, "grok_build": 0, "grok_console": 0},
            "window": {"count": 0, "from": None, "to": None, "range": range_name, "range_seconds": range_sec},
            "range": range_name,
            "available_ranges": list(POOL_TREND_RANGES.keys()),
        }
    first = points[0]
    last = points[-1]
    keys = ("total", "grok_web", "grok_build", "grok_console")
    delta = {k: int(last.get(k) or 0) - int(first.get(k) or 0) for k in keys}
    return {
        "points": points,
        "latest": last,
        "delta": delta,
        "window": {
            "count": len(points),
            "from": first.get("ts"),
            "to": last.get("ts"),
            "range": range_name,
            "range_seconds": range_sec,
        },
        "range": range_name,
        "available_ranges": list(POOL_TREND_RANGES.keys()),
    }


def classify_error_text(text: str) -> str:
    value = (text or "").lower()
    if not value:
        return "unknown"
    rules = [
        ("captcha", ("turnstile", "captcha", "cloudflare", "cf-challenge", "challenge")),
        ("mail", ("temp mail", "temp_mail", "mailbox", "email", "imap", "duckmail", "验证码", "mailbox")),
        ("proxy", ("proxy", "warp", "socks", "tunnel", "network is unreachable", "connection refused", "timed out", "timeout", "proxyerror")),
        ("import", ("import", "grok2api", "push", "入池", "sso", "token sink", "admin login")),
        ("xai", ("x.ai", "accounts.x.ai", "register", "signup", "最终注册", "registration")),
    ]
    for key, needles in rules:
        if any(n in value for n in needles):
            return key
    if "[error]" in value or "失败" in value or "error" in value:
        return "other"
    return "unknown"


def classify_task_errors(console_path: Path | None, last_error: str = "") -> dict[str, Any]:
    counts = {
        "mail": 0,
        "proxy": 0,
        "captcha": 0,
        "xai": 0,
        "import": 0,
        "other": 0,
        "unknown": 0,
    }
    samples: dict[str, str] = {}
    lines: list[str] = []
    if console_path is not None and console_path.exists():
        try:
            lines = console_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            lines = []
    error_lines = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if "[Error]" in line or "失败" in line or "turnstile" in line.lower() or "push" in line.lower() and "失败" in line:
            error_lines.append(line)
    if last_error:
        error_lines.append(str(last_error))
    # Prefer explicit error regex matches if present
    for raw in lines:
        m = LINE_RE_ERROR.search(raw)
        if m:
            error_lines.append(m.group(2).strip())
    # de-dup while preserving order
    seen = set()
    uniq = []
    for item in error_lines:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(key)
    for item in uniq:
        kind = classify_error_text(item)
        counts[kind] = counts.get(kind, 0) + 1
        samples.setdefault(kind, item[:160])
    top = None
    top_count = 0
    for key, value in counts.items():
        if value > top_count:
            top = key
            top_count = value
    if not top_count and last_error:
        top = classify_error_text(last_error)
        counts[top] = 1
        samples[top] = str(last_error)[:160]
        top_count = 1
    return {
        "error_counts": counts,
        "error_samples": samples,
        "top_error_type": top if top_count else "",
        "top_error_count": top_count,
    }


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



def collect_task_sso_tokens(task_id: int, row: sqlite3.Row | None = None) -> list[str]:
    """Read unique SSO tokens from a task's sso directory/output files."""
    row = row or task_row(task_id)
    task_dir = Path(row["task_dir"])
    candidates: list[Path] = []
    sso_dir = task_dir / "sso"
    if sso_dir.exists() and sso_dir.is_dir():
        candidates.extend(sorted(sso_dir.glob("*.txt")))
    # also allow direct output path patterns
    candidates.append(task_dir / "sso" / f"task_{task_id}.txt")
    tokens: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for line in lines:
            value = str(line or "").strip()
            if not value or value.startswith("#"):
                continue
            # JWT-ish SSO tokens usually contain two dots
            if value.count(".") < 2 and len(value) < 20:
                continue
            if value in seen:
                continue
            seen.add(value)
            tokens.append(value)
    return tokens


def _normalize_grok2api_urls(login_url: str, import_url: str) -> tuple[str, str]:
    """Rewrite old token-sink / unresolvable grok2api hosts for console container."""
    login_url = str(login_url or "").strip()
    import_url = str(import_url or "").strip()
    bridge_login = "http://172.18.0.1:8000/api/admin/v1/auth/login"
    bridge_import = "http://172.18.0.1:8000/api/admin/v1/accounts/web/import"

    def needs_rewrite(url: str) -> bool:
        value = str(url or "").strip().lower()
        if not value:
            return False
        if "/v1/admin/tokens" in value:
            return True
        if "://grok2api" in value or value.startswith("grok2api"):
            return True
        return False

    if needs_rewrite(login_url):
        login_url = bridge_login
    if needs_rewrite(import_url) or (import_url and "/v1/admin/tokens" in import_url):
        import_url = bridge_import
    if login_url and not import_url:
        if login_url.endswith("/auth/login"):
            import_url = login_url.replace("/auth/login", "/accounts/web/import")
        elif login_url == bridge_login:
            import_url = bridge_import
    return login_url, import_url


def push_sso_tokens_to_api(tokens: list[str], task_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Login to grok2api admin and multipart-import SSO tokens."""
    defaults = merged_defaults()
    api_defaults = dict(defaults.get("api") or {})
    cfg_api = dict((task_config or {}).get("api") or {}) if isinstance(task_config, dict) else {}

    def pick_prefer_defaults(key: str, *alts: str) -> str:
        # Prefer system defaults for connectivity; task config may contain
        # historical hostnames (grok2api) that console cannot resolve.
        for source in (api_defaults, cfg_api, task_config or {}):
            if not isinstance(source, dict):
                continue
            for k in (key, *alts):
                val = str(source.get(k) or "").strip()
                if val:
                    return val
        return ""

    def pick_password() -> str:
        for source in (api_defaults, cfg_api, task_config or {}):
            if not isinstance(source, dict):
                continue
            for k in ("admin_password", "api_admin_password"):
                val = str(source.get(k) or "").strip()
                if val:
                    return val
        return ""

    login_url = pick_prefer_defaults("endpoint", "api_endpoint")
    import_url = pick_prefer_defaults("import_endpoint", "api_import_endpoint")
    username = pick_prefer_defaults("admin_username", "api_admin_username") or "admin"
    password = pick_password()
    login_url, import_url = _normalize_grok2api_urls(login_url, import_url)
    if not import_url and login_url:
        import_url = login_url.replace("/auth/login", "/accounts/web/import")
    tokens_to_push = [str(t).strip() for t in tokens if str(t).strip()]
    if not tokens_to_push:
        return {
            "ok": False,
            "error": "no_tokens",
            "summary": "没有可入池的 SSO token",
            "attempted": 0,
            "created": 0,
            "updated": 0,
            "target": import_url or login_url or "-",
        }
    if not login_url or not password:
        return {
            "ok": False,
            "error": "missing_api_config",
            "summary": "缺少 login endpoint 或 admin password",
            "attempted": len(tokens_to_push),
            "created": 0,
            "updated": 0,
            "target": import_url or login_url or "-",
        }
    if not import_url:
        return {
            "ok": False,
            "error": "missing_import_endpoint",
            "summary": "缺少 import endpoint",
            "attempted": len(tokens_to_push),
            "created": 0,
            "updated": 0,
            "target": login_url or "-",
        }

    try:
        login_resp = requests.post(
            login_url,
            json={"username": username, "password": password},
            headers={"Content-Type": "application/json"},
            timeout=15,
            verify=False,
        )
        if login_resp.status_code != 200:
            return {
                "ok": False,
                "error": "login_failed",
                "summary": f"admin 登录失败 HTTP {login_resp.status_code}",
                "detail": (login_resp.text or "")[:300],
                "attempted": len(tokens_to_push),
                "created": 0,
                "updated": 0,
                "target": import_url,
            }
        jwt = (
            (login_resp.json() or {})
            .get("data", {})
            .get("tokens", {})
            .get("accessToken", "")
        )
        if not jwt:
            return {
                "ok": False,
                "error": "login_no_token",
                "summary": "admin 登录响应无 accessToken",
                "attempted": len(tokens_to_push),
                "created": 0,
                "updated": 0,
                "target": import_url,
            }
    except Exception as exc:
        return {
            "ok": False,
            "error": "login_exception",
            "summary": f"admin 登录异常: {exc}",
            "attempted": len(tokens_to_push),
            "created": 0,
            "updated": 0,
            "target": import_url,
        }

    import tempfile

    sso_text = "\n".join(tokens_to_push)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="console_sso_retry_")
        os.close(fd)
        Path(tmp_path).write_text(sso_text, encoding="utf-8")
        with open(tmp_path, "rb") as f:
            resp = requests.post(
                import_url,
                headers={"Authorization": f"Bearer {jwt}"},
                files={"files": ("grok-web-sso-tokens.txt", f, "text/plain")},
                timeout=120,
                verify=False,
            )
        created = 0
        updated = 0
        body = resp.text or ""
        if resp.status_code == 200:
            for line in body.split("\n"):
                if line.startswith("data: ") and '"created"' in line:
                    try:
                        d = json.loads(line[6:])
                        created = int(d.get("created") or 0)
                        updated = int(d.get("updated") or 0)
                    except Exception:
                        pass
            # also accept plain JSON responses
            if created == 0 and updated == 0:
                try:
                    d = resp.json()
                    if isinstance(d, dict):
                        created = int(d.get("created") or (d.get("data") or {}).get("created") or 0)
                        updated = int(d.get("updated") or (d.get("data") or {}).get("updated") or 0)
                except Exception:
                    pass
            return {
                "ok": True,
                "error": "",
                "summary": f"入池成功：新增 {created}，更新 {updated}，本轮 {len(tokens_to_push)} 个",
                "detail": body[:500],
                "attempted": len(tokens_to_push),
                "created": created,
                "updated": updated,
                "target": import_url,
            }
        return {
            "ok": False,
            "error": "import_failed",
            "summary": f"导入失败 HTTP {resp.status_code}",
            "detail": body[:500],
            "attempted": len(tokens_to_push),
            "created": 0,
            "updated": 0,
            "target": import_url,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": "import_exception",
            "summary": f"导入异常: {exc}",
            "attempted": len(tokens_to_push),
            "created": 0,
            "updated": 0,
            "target": import_url,
        }
    finally:
        if tmp_path and Path(tmp_path).exists():
            try:
                Path(tmp_path).unlink()
            except Exception:
                pass


def retry_task_push(task_id: int) -> dict[str, Any]:
    row = task_row(task_id)
    if row["status"] in ACTIVE_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="任务仍在运行中，结束后再重试入池")
    try:
        task_config = json.loads(row["config_json"] or "{}")
    except Exception:
        task_config = {}
    tokens = collect_task_sso_tokens(task_id, row)
    if not tokens:
        result = {
            "ok": False,
            "task_id": task_id,
            "error": "no_sso_file",
            "summary": "任务目录中没有可重试的 SSO 文件",
            "tokens_found": 0,
            "attempted": 0,
            "created": 0,
            "updated": 0,
            "push_gap_before": 0,
        }
        try:
            write_audit_log(
                "push_retry_failed",
                f"任务 #{task_id} 入池重试失败：无 SSO",
                level="error",
                task_id=task_id,
                detail=result,
            )
        except Exception:
            pass
        return result

    # estimate gap before
    serialized = serialize_task(row)
    gap_before = int(serialized.get("push_gap") or 0)
    push_result = push_sso_tokens_to_api(tokens, task_config=task_config)
    # append a console log marker for operators
    try:
        console_path = Path(row["console_path"])
        console_path.parent.mkdir(parents=True, exist_ok=True)
        with console_path.open("a", encoding="utf-8") as f:
            created = int(push_result.get("created") or 0)
            updated = int(push_result.get("updated") or 0)
            attempted = int(push_result.get("attempted") or len(tokens) or 0)
            f.write(
                f"\n[{now_iso()}] [console] 手动重试入池：tokens={len(tokens)} ok={push_result.get('ok')} "
                f"{push_result.get('summary') or ''}\n"
            )
            if push_result.get("ok"):
                # Match worker log format so serialize_task recomputes pushed_count/push_gap.
                f.write(
                    f"[*] SSO 已推送到 grok2api（新增 {created}，更新 {updated}，本轮 {attempted} 个）\n"
                )
    except Exception:
        pass

    out = {
        "ok": bool(push_result.get("ok")),
        "task_id": task_id,
        "tokens_found": len(tokens),
        "push_gap_before": gap_before,
        **push_result,
    }
    try:
        write_audit_log(
            "push_retry_ok" if out["ok"] else "push_retry_failed",
            f"任务 #{task_id} 入池重试{'成功' if out['ok'] else '失败'}：{out.get('summary') or ''}",
            level="success" if out["ok"] else "error",
            task_id=task_id,
            detail={
                "tokens_found": len(tokens),
                "created": out.get("created"),
                "updated": out.get("updated"),
                "target": out.get("target"),
                "error": out.get("error") or "",
            },
        )
        sse_hub.publish("tasks_changed", {"reason": "push_retry", "task_id": task_id, "ok": out["ok"]})
    except Exception:
        pass
    return out


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
    classified = classify_task_errors(console_path, last_error=last_error)
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
        "error_counts": classified.get("error_counts") or {},
        "error_samples": classified.get("error_samples") or {},
        "top_error_type": classified.get("top_error_type") or "",
        "top_error_count": classified.get("top_error_count") or 0,
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
                try:
                    write_audit_log(
                        "task_stopped",
                        f"任务 #{task_id} 启动前已停止",
                        level="warn",
                        task_id=task_id,
                        detail={"status": STATUS_STOPPED},
                    )
                    sse_hub.publish("tasks_changed", {"reason": "stopped", "task_id": task_id})
                except Exception:
                    pass
                return
            raise HTTPException(status_code=409, detail="Task is not running")
        execute_no_return(
            "UPDATE tasks SET status = ?, last_error = ?, current_phase = ? WHERE id = ?",
            (STATUS_STOPPING, "Stopping task...", STATUS_STOPPING, task_id),
        )
        try:
            sse_hub.publish("task_stopping", {"task_id": task_id, "status": STATUS_STOPPING})
            sse_hub.publish("tasks_changed", {"reason": "stopping", "task_id": task_id})
        except Exception:
            pass
        try:
            write_audit_log(
                "task_stopping",
                f"请求停止任务 #{task_id}",
                level="warn",
                task_id=task_id,
                detail={"status": STATUS_STOPPING},
            )
        except Exception:
            pass
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
        try:
            sse_hub.publish("task_started", {"task_id": task_id, "status": STATUS_RUNNING})
            sse_hub.publish("tasks_changed", {"reason": "started", "task_id": task_id})
        except Exception:
            pass
        try:
            write_audit_log(
                "task_started",
                f"任务 #{task_id} 开始运行",
                level="info",
                task_id=task_id,
                detail={"status": STATUS_RUNNING, "pid": process.pid},
            )
        except Exception:
            pass

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
            try:
                sse_hub.publish(
                    "task_progress",
                    {
                        "task_id": task_id,
                        "status": row["status"],
                        "completed_count": parsed["completed_count"],
                        "failed_count": parsed["failed_count"],
                        "current_round": parsed["current_round"],
                        "current_phase": parsed["current_phase"],
                        "last_error": parsed["last_error"],
                    },
                )
            except Exception:
                pass
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
            try:
                sse_hub.publish(
                    "task_finished",
                    {
                        "task_id": task_id,
                        "status": final_status,
                        "exit_code": exit_code,
                        "completed_count": parsed["completed_count"],
                        "failed_count": parsed["failed_count"],
                    },
                )
                sse_hub.publish("tasks_changed", {"reason": "finished", "task_id": task_id, "status": final_status})
            except Exception:
                pass
            try:
                level = "success"
                if final_status in {STATUS_FAILED, STATUS_STOPPED}:
                    level = "error" if final_status == STATUS_FAILED else "warn"
                elif final_status == STATUS_PARTIAL:
                    level = "warn"
                msg = f"任务 #{task_id} 结束：{final_status}"
                if parsed.get("last_error"):
                    msg = f"{msg} · {str(parsed.get('last_error'))[:80]}"
                write_audit_log(
                    "task_finished",
                    msg,
                    level=level,
                    task_id=task_id,
                    detail={
                        "status": final_status,
                        "exit_code": exit_code,
                        "completed_count": parsed["completed_count"],
                        "failed_count": parsed["failed_count"],
                        "last_error": parsed.get("last_error") or "",
                    },
                )
            except Exception:
                pass
        for task_id in finished:
            managed = self._processes.pop(task_id, None)
            if managed and managed.log_handle:
                managed.log_handle.close()



class SseHub:
    """Tiny in-process fan-out for console live updates."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Queue] = []

    def subscribe(self) -> Queue:
        q: Queue = Queue(maxsize=200)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish(self, event: str, data: dict[str, Any] | None = None) -> None:
        payload = {
            "event": event,
            "ts": now_iso(),
            "data": data or {},
        }
        dead: list[Queue] = []
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except Exception:
                # Drop oldest then retry once; if still full, mark dead.
                try:
                    _ = q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    dead.append(q)
        for q in dead:
            self.unsubscribe(q)


sse_hub = SseHub()


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


@app.get("/api/pool/trend")
def api_pool_trend(
    limit: int = Query(default=288, ge=1, le=288),
    range: str = Query(default="6h"),
) -> dict[str, Any]:
    """Return pool history for a time window.

    range: 1h | 6h | 24h
    limit: soft max points after time filtering
    """
    limit = max(1, min(int(limit or POOL_HISTORY_MAX_POINTS), POOL_HISTORY_MAX_POINTS))
    trend = build_pool_trend(limit=limit, range_key=range)
    return {"ok": True, **trend}


@app.get("/api/events")
async def api_events(request: Request, task_id: int | None = Query(default=None)):
    """Server-Sent Events stream for live console updates.

    Auth: same as other APIs (Authorization / query token / cookie).
    Optional task_id filters task_progress/log-ish events to one task, while
    still forwarding global tasks_changed events.
    """
    queue = sse_hub.subscribe()

    async def event_stream():
        # Hello + bootstrap snapshot so clients can reconcile immediately.
        hello = {
            "event": "hello",
            "ts": now_iso(),
            "data": {
                "task_id": task_id,
                "active_tasks": sum(
                    1
                    for row in fetch_all("SELECT status FROM tasks")
                    if row["status"] in ACTIVE_TASK_STATUSES
                ),
            },
        }
        yield f"event: hello\ndata: {json.dumps(hello, ensure_ascii=False)}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.to_thread(queue.get, True, 15.0)
                except Empty:
                    # keepalive comment + ping event
                    yield f": keepalive {now_iso()}\n\n"
                    ping = {"event": "ping", "ts": now_iso(), "data": {}}
                    yield f"event: ping\ndata: {json.dumps(ping, ensure_ascii=False)}\n\n"
                    continue
                except Exception:
                    break

                event_name = str(payload.get("event") or "message")
                data = payload.get("data") or {}
                if task_id is not None and event_name in {"task_progress", "task_started", "task_finished", "task_stopping"}:
                    if int(data.get("task_id") or -1) != int(task_id):
                        # still useful to know list changed
                        if event_name == "task_finished":
                            pass
                        else:
                            continue
                yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            sse_hub.unsubscribe(queue)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


@app.get("/api/audit")
def api_audit_logs(
    limit: int = Query(50, ge=1, le=200),
    task_id: int | None = Query(default=None),
    level: str | None = Query(default=None),
    event: str | None = Query(default=None),
    q: str | None = Query(default=None),
) -> dict[str, Any]:
    items = list_audit_logs(limit=limit, task_id=task_id, level=level, event=event, q=q)
    return {
        "ok": True,
        "items": items,
        "filters": {
            "task_id": task_id,
            "level": (level or "").strip().lower() or None,
            "event": (event or "").strip() or None,
            "q": (q or "").strip() or None,
            "limit": max(1, min(int(limit or 50), 200)),
        },
        "event_names": list_audit_event_names(limit=40),
    }


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


@app.get("/api/templates")
def list_templates() -> dict[str, Any]:
    return {"templates": read_templates()}


@app.post("/api/templates")
def upsert_template(payload: TaskTemplate) -> dict[str, Any]:
    templates = read_templates()
    item = payload.model_dump()
    name = str(item.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="template name is required")
    item["name"] = name
    item_id = str(item.get("id") or "").strip()
    if not item_id:
        stamp = (
            now_iso()
            .replace("-", "")
            .replace(":", "")
            .replace("T", "")
            .replace("Z", "")
            .replace(" ", "")
            .split(".")[0]
        )
        item_id = f"tpl_{stamp}_{len(templates) + 1}"
    item["id"] = item_id
    replaced = False
    for idx, old in enumerate(templates):
        if str(old.get("id") or "") == item_id or str(old.get("name") or "") == name:
            # keep existing secrets if new password empty
            if not str(item.get("api_admin_password") or "").strip():
                item["api_admin_password"] = old.get("api_admin_password") or ""
            if not str(item.get("temp_mail_admin_password") or "").strip() and old.get("temp_mail_admin_password"):
                item["temp_mail_admin_password"] = old.get("temp_mail_admin_password") or ""
            templates[idx] = item
            replaced = True
            break
    if not replaced:
        templates.insert(0, item)
    # cap templates
    templates = templates[:30]
    write_templates(templates)
    return {"ok": True, "template": item, "templates": templates}


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str) -> dict[str, Any]:
    templates = read_templates()
    kept = [t for t in templates if str(t.get("id") or "") != template_id and str(t.get("name") or "") != template_id]
    if len(kept) == len(templates):
        raise HTTPException(status_code=404, detail="Template not found")
    write_templates(kept)
    return {"ok": True, "templates": kept}


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
    try:
        sse_hub.publish("task_created", {"task_id": task_id, "status": STATUS_QUEUED})
        sse_hub.publish("tasks_changed", {"reason": "created", "task_id": task_id})
    except Exception:
        pass
    try:
        write_audit_log(
            "task_created",
            f"创建任务 #{task_id} · {payload.name.strip()}",
            level="info",
            task_id=task_id,
            detail={"name": payload.name.strip(), "count": payload.count, "status": STATUS_QUEUED},
        )
    except Exception:
        pass
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


@app.post("/api/tasks/{task_id}/push-retry")
def retry_task_import(task_id: int) -> dict[str, Any]:
    """Re-import task SSO tokens into grok2api to close push gaps."""
    result = retry_task_push(task_id)
    # refresh serialize for latest gap estimate
    task = serialize_task(task_row(task_id))
    return {"ok": bool(result.get("ok")), "result": result, "task": task}


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
    try:
        sse_hub.publish("task_deleted", {"task_id": task_id})
        sse_hub.publish("tasks_changed", {"reason": "deleted", "task_id": task_id})
    except Exception:
        pass
    try:
        write_audit_log(
            "task_deleted",
            f"删除任务 #{task_id} · {row['name']}",
            level="warn",
            task_id=task_id,
            detail={"name": row["name"], "status": row["status"]},
        )
    except Exception:
        pass
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
    if deleted_ids:
        try:
            sse_hub.publish(
                "tasks_changed",
                {"reason": "cleanup", "deleted_ids": deleted_ids, "deleted_count": len(deleted_ids)},
            )
        except Exception:
            pass
        try:
            write_audit_log(
                "tasks_cleanup",
                f"清理终态任务 {len(deleted_ids)} 个",
                level="warn",
                detail={"deleted_ids": deleted_ids, "statuses": statuses, "skipped_ids": skipped_ids},
            )
        except Exception:
            pass
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
