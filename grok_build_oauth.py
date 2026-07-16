#!/usr/bin/env python3
"""
grok_build_oauth.py — 纯协议 Grok 注册 + Build OAuth 导出

流水线（完全无浏览器）：
  1. 自建临时邮箱获取地址
  2. xaccounts.x.ai 注册（curl_cffi TLS 指纹 + YesCaptcha Turnstile + Castle 占位符）
  3. 邮箱收验证码
  4. 提取 SSO token
  5. OAuth PKCE → Build OAuth (grok-cli:access scope)
  6. 导出 CLIProxyAPI 兼容 auth JSON
  7. 推送 SSO 到 grok2api

前置条件：
  - YESCAPTCHA_API_KEY 环境变量（或 .env 文件）
  - config.json 中的 temp_mail 配置（与 DrissionPage_example 共用）
  - curl_cffi >= 0.7
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path

_WORKSPACE = Path("/workspace")
sys.path.insert(0, str(_WORKSPACE))

# ---------- 环境 ----------
for _env_file in (_WORKSPACE / ".env", Path(".env")):
    if _env_file.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(_env_file)
        except ImportError:
            pass

YESCAPTCHA_KEY = os.environ.get("YESCAPTCHA_API_KEY", "")
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=cloud-console"
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""

# CLIProxyAPI 导出目录
CLIPROXYAPI_AUTH_DIR = Path(os.environ.get("CLIPROXYAPI_AUTH_DIR", str(_WORKSPACE / "cliproxyapi_auth")))

from xconsole_client import XConsoleAuthClient, YesCaptchaSolver, config as C
from xconsole_client.xai_oauth import (
    CLIPROXYAPI_GROK_BASE_URL,
    complete_build_oauth,
    default_cliproxyapi_auth_dir,
)
from xconsole_client.oauth_protocol import extract_cookies_from_auth_client
from xconsole_client.mail_adapter import make_mail_adapter


def register_and_export(
    index: int = 1,
    *,
    do_oauth: bool = True,
    yescaptcha_key: str = "",
    headless: bool = True,
    oauth_timeout: float = 180.0,
    cliproxyapi_auth_dir: str | Path | None = None,
    push_to_grok2api: bool = True,
) -> dict:
    """执行一次注册 + Build OAuth 导出。"""

    ya_key = yescaptcha_key or YESCAPTCHA_KEY
    if not ya_key:
        raise RuntimeError("YESCAPTCHA_API_KEY 环境变量未设置")

    # Step 1: 创建邮箱
    print(f"[{index}] 创建临时邮箱...")
    mail = make_mail_adapter(timeout=120)
    email = mail.create()
    password = f"Pw{os.urandom(6).hex()}!a#A"
    print(f"[{index}] 邮箱: {email}")

    # Step 2: 协议客户端 + 预热
    c = XConsoleAuthClient(debug=True, signup_url=SIGNUP_URL)
    c.visit_home()
    c.load_signup_page()
    print(f"[{index}] cookie + scrape OK")

    # Step 3: 邮箱验证
    c.create_email_validation_code(email)
    code = mail.wait_for_code(timeout=120)
    print(f"[{index}] 验证码: {code}")
    c.verify_email_validation_code(email, code)
    c.validate_password(email, password)
    print(f"[{index}] 邮箱验证通过")

    # Step 4: Turnstile
    solver = YesCaptchaSolver(ya_key)
    turnstile_token = solver.solve_turnstile(
        website_url=SIGNUP_URL,
        website_key=C.TURNSTILE_SITEKEY,
        premium=True,
    )
    print(f"[{index}] Turnstile 已解 ({len(turnstile_token)} 字符)")

    # Step 5: 创建账号
    res = c.create_account(
        email=email,
        given_name="Test",
        family_name="User",
        password=password,
        email_validation_code=code,
        turnstile_token=turnstile_token,
        castle_request_token="",
        conversion_id=str(uuid.uuid4()),
    )
    if not res.ok:
        print(f"[{index}] create_account 失败 HTTP {res.http_status}: {res.body_text[:200]}")
        return {
            "email": email,
            "password": password,
            "sso": None,
            "oauth_access_token": None,
            "cliproxyapi_auth": None,
            "error": f"HTTP {res.http_status}",
        }

    print(f"[{index}] 账号创建成功")

    # Step 6: 提取 SSO
    sso = c.fetch_sso_token(email=email, password=password, save=True, retries=3)
    if not sso:
        print(f"[{index}] SSO 提取失败")
        return {
            "email": email,
            "password": password,
            "sso": None,
            "oauth_access_token": None,
            "cliproxyapi_auth": None,
            "error": "SSO failed",
        }

    payload = json.loads(base64.urlsafe_b64decode(sso.split(".")[1] + "=="))
    print(f"[{index}] SSO 提取成功 (session_id={payload.get(session_id, ?)[:12]}...)")

    result = {
        "email": email,
        "password": password,
        "sso": sso,
        "oauth_access_token": None,
        "oauth_refresh_token": None,
        "cliproxyapi_auth": None,
        "error": None,
    }

    # Step 7: Build OAuth → CLIProxyAPI auth JSON
    if do_oauth:
        auth_dir = Path(cliproxyapi_auth_dir) if cliproxyapi_auth_dir else default_cliproxyapi_auth_dir()
        session_cookies = extract_cookies_from_auth_client(c)
        if sso:
            session_cookies = dict(session_cookies or {})
            session_cookies.setdefault("sso", sso)

        print(f"[{index}] OAuth → {auth_dir}")
        try:
            oauth_result = complete_build_oauth(
                email, password,
                cliproxyapi_auth_dir=str(auth_dir),
                cliproxyapi_base_url=CLIPROXYAPI_GROK_BASE_URL,
                headless=headless,
                timeout=oauth_timeout,
                proxy=PROXY,
                interactive_fallback=False,
                yescaptcha_key=ya_key,
                session_cookies=session_cookies,
            )
            if oauth_result and oauth_result.access_token:
                result["oauth_access_token"] = oauth_result.access_token
                result["oauth_refresh_token"] = oauth_result.refresh_token
                result["cliproxyapi_auth"] = str(oauth_result.cliproxyapi_path) if oauth_result.cliproxyapi_path else None
                print(f"[{index}] Build OAuth 导出成功 → {result[cliproxyapi_auth]}")
            else:
                result["error"] = "Build OAuth returned empty token"
                print(f"[{index}] Build OAuth 返回空 token")
        except Exception as e:
            result["error"] = f"Build OAuth 异常: {e}"
            print(f"[{index}] Build OAuth 异常: {e}")

    # Step 8: 推送 SSO 到 grok2api
    if push_to_grok2api and sso:
        try:
            from grok2api_push import push_sso_tokens
            push_sso_tokens([sso])
            print(f"[{index}] SSO 已推送到 grok2api")
        except Exception as e:
            print(f"[{index}] SSO 推送 grok2api 失败: {e}")

    return result


def push_sso_tokens(tokens: list[str], config_path: str = "/workspace/config.json"):
    """推送 SSO token 到 grok2api (copied from push_sso_to_api in DrissionPage_example.py)."""
    import tempfile
    import requests as req_lib

    with open(config_path) as f:
        cfg = json.load(f)
    api_cfg = cfg.get("api", {})
    login_url = api_cfg.get("endpoint", "http://172.18.0.1:8000/api/admin/v1/auth/login")
    import_url = api_cfg.get("import_endpoint", login_url.replace("/auth/login", "/accounts/web/import"))
    username = api_cfg.get("admin_username", "admin")
    password = api_cfg.get("admin_password", "admin123")

    # Login
    resp = req_lib.post(login_url, json={"username": username, "password": password}, timeout=15)
    resp.raise_for_status()
    jwt = resp.json()["data"]["tokens"]["accessToken"]

    # Import
    sso_text = "\n".join(tokens)
    fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="grok_sso_")
    try:
        os.write(fd, sso_text.encode())
        os.close(fd)
        with open(tmp_path, "rb") as f:
            resp = req_lib.post(
                import_url,
                files={"files": ("grok-web-sso-tokens.txt", f, "text/plain")},
                headers={"Authorization": f"Bearer {jwt}"},
                timeout=30,
            )
        created = updated = 0
        for line in resp.text.split("\n"):
            if "创建" in line:
                try:
                    created = int(line.split("创建")[-1].split(",")[0].strip().split(" ")[-1])
                except:
                    pass
        print(f"[*] SSO 推送到 grok2api (创建 {created}, 更新 {updated}, 共 {len(tokens)} 个)")
    finally:
        try:
            os.unlink(tmp_path)
        except:
            pass


def main():
    import argparse
    p = argparse.ArgumentParser(description="纯协议 Grok 注册 + Build OAuth 导出")
    p.add_argument("--count", type=int, default=1, help="注册数量")
    p.add_argument("--no-oauth", action="store_true", help="只注册+SSO，不走 Build OAuth")
    p.add_argument("--yescaptcha-key", default=YESCAPTCHA_KEY, help="YesCaptcha API key")
    p.add_argument("--cliproxyapi-auth-dir", default=str(CLIPROXYAPI_AUTH_DIR))
    args = p.parse_args()

    if not args.yescaptcha_key:
        print("错误: 请设置 YESCAPTCHA_API_KEY 环境变量或通过 --yescaptcha-key 传入")
        sys.exit(1)

    for i in range(1, args.count + 1):
        print(f"\n{=*50}")
        print(f"开始第 {i}/{args.count} 个账号注册...")
        result = register_and_export(
            i,
            do_oauth=not args.no_oauth,
            yescaptcha_key=args.yescaptcha_key,
            cliproxyapi_auth_dir=args.cliproxyapi_auth_dir,
        )
        status = "OK" if result.get("sso") else "FAIL"
        oauth_status = "BUILD_OK" if result.get("cliproxyapi_auth") else "BUILD_FAIL"
        print(f"[{i}] {status} {oauth_status} {result.get(email, ?)}")
        if result.get("error"):
            print(f"    错误: {result[error]}")

    print(f"\n{=*50}")
    print("完成！")

if __name__ == "__main__":
    main()
