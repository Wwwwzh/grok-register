#!/usr/bin/env python3
"""
CPA mint 批量脚本 — 在 grok2api Docker 容器内执行。
通过 FlareSolverr 获取 cf_clearance + SSO cookie 调 grok.com REST API。
结果写入容器内 /app/data/cpa_auths/。
"""
import argparse
import asyncio
import json
import os
import sys
import urllib3
import requests

urllib3.disable_warnings()

GROK2API_URL = "http://127.0.0.1:8000/v1/admin/tokens"
FLARESOLVERR_URL = "http://flaresolverr:8191/v1"

BUILD_TOKEN_URL = "https://grok.com/rest/app/build_token"
BUILD_OAUTH_URL = "https://grok.com/rest/build/oauth"


def get_sso_tokens():
    try:
        resp = requests.get(GROK2API_URL, timeout=10)
        data = resp.json()
        return [str(t).strip() for t in data.get("ssoBasic", []) if str(t).strip()]
    except Exception as e:
        print(f"[Error] 获取 token: {e}")
        return []


def get_cf_clearance():
    """通过 FlareSolverr 获取 cf_clearance cookie"""
    try:
        resp = requests.post(
            FLARESOLVERR_URL,
            json={
                "cmd": "request.get",
                "url": "https://grok.com/",
                "maxTimeout": 60000,
            },
            timeout=90,
        )
        data = resp.json()
        if data.get("status") != "ok":
            print(f"  FlareSolverr 状态异常: {data.get('message', '')}")
            return None
        cookies = data.get("solution", {}).get("cookies", [])
        for c in cookies:
            if c.get("name") == "cf_clearance":
                val = c.get("value", "")
                print(f"  ✓ cf_clearance: {val[:30]}...")
                return val
        print(f"  未找到 cf_clearance cookie, 可用: {[c['name'] for c in cookies]}")
        return None
    except Exception as e:
        print(f"  FlareSolverr 异常: {e}")
        return None


def mint_one(sso, cf_clearance):
    """用 SSO + cf_clearance 调 grok.com REST API"""
    import httpx

    cookies = {"sso": sso, "cf_clearance": cf_clearance}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    # 由于容器内有 WARP，直连 grok.com
    with httpx.Client(
        proxy="socks5://warp:1080",
        verify=False,
        timeout=30,
        follow_redirects=False,
    ) as c:
        # 步骤1: build_token
        try:
            r = c.get(BUILD_TOKEN_URL, cookies=cookies, headers=headers)
        except Exception as e:
            print(f"  build_token 错误: {e}")
            return None

        if r.status_code == 302 or r.status_code == 301:
            print(f"  build_token 重定向 (SSO 可能过期)")
            return None
        if r.status_code != 200:
            print(f"  build_token HTTP {r.status_code}: {r.text[:200]}")
            return None

        try:
            data = r.json()
        except Exception:
            print(f"  build_token JSON 解析失败")
            return None

        build_token = None
        if isinstance(data.get("response"), dict) and data["response"].get("token"):
            build_token = data["response"]["token"]
        else:
            build_token = data.get("token") or data.get("buildToken", "")

        if not build_token:
            print(f"  build_token 无 token: {json.dumps(data, ensure_ascii=False)[:200]}")
            return None

        print(f"  build_token: {build_token[:30]}...")

        # 步骤2: build_oauth
        try:
            r2 = c.post(
                BUILD_OAUTH_URL,
                json={"token": build_token},
                cookies=cookies,
                headers={**headers, "Content-Type": "application/json"},
            )
        except Exception as e:
            print(f"  build_oauth 错误: {e}")
            return None

        if r2.status_code != 200:
            print(f"  build_oauth HTTP {r2.status_code}: {r2.text[:200]}")
            return None

        try:
            oauth = r2.json()
        except Exception:
            print(f"  build_oauth JSON 解析失败")
            return None

        # 提取 email
        email = oauth.get("email", "")
        if not email:
            try:
                r3 = c.get("https://grok.com/rest/app/me", cookies=cookies, headers=headers)
                if r3.status_code == 200:
                    email = r3.json().get("email", "unknown")
            except Exception:
                pass

        return {"email": email or "unknown", **oauth}


def main():
    parser = argparse.ArgumentParser(description="CPA mint 批量")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", default="/app/data/cpa_auths")
    parser.add_argument("--max-tokens", type=int, default=0)
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args()

    tokens = get_sso_tokens()
    if not tokens:
        print("[Error] 没有 SSO token")
        sys.exit(1)

    print(f"[*] 共 {len(tokens)} 个 SSO token")
    print(f"[*] 输出目录: {args.output_dir}")
    if args.dry_run:
        print("[*] DRY RUN 模式")

    # 获取 cf_clearance（所有请求共用，减少 FlareSolverr 调用）
    print(f"[*] 获取 cf_clearance...")
    cf = get_cf_clearance()
    if not cf:
        print("[Error] 无法获取 cf_clearance，退出")
        sys.exit(1)

    if not args.dry_run:
        os.makedirs(args.output_dir, exist_ok=True)

    success = 0
    failed = 0

    for i, sso in enumerate(tokens):
        print(f"\n[{i+1}/{len(tokens)}] SSO: {sso[:35]}...")

        if args.max_tokens > 0 and success >= args.max_tokens:
            print(f"[*] 已达到最大数量限制 ({args.max_tokens})")
            break

        try:
            result = mint_one(sso, cf)
        except Exception as e:
            print(f"  ❌ 异常: {e}")
            # 重新获取 cf_clearance（可能过期了）
            cf = get_cf_clearance()
            failed += 1
            continue

        if result:
            email = result.get("email", "unknown")
            print(f"  ✅ email={email}  expires_in={result.get('expires_in','N/A')}")
            if not args.dry_run:
                safe = email.replace("@", "_").replace(".", "_")
                filepath = os.path.join(args.output_dir, f"xai-{safe}.json")
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                print(f"  📁 {filepath}")
            success += 1
        else:
            failed += 1
            # 如果 build_token 返回重定向，cf_clearance 可能过期
            cf = get_cf_clearance()

        import time
        time.sleep(args.delay)

    print(f"\n{'='*50}")
    print(f"[*] 完成! 成功={success}/{len(tokens)} 失败={failed}")

    if not args.dry_run and success > 0:
        print(f"\n[*] 导出: docker cp grok-register-grok2api-1:{args.output_dir}/. /root/grok-register/output/cpa_auths/")
        print(f"[*] 推送到 CLIProxyAPI Docker:")
        for fname in os.listdir(args.output_dir):
            if fname.endswith(".json"):
                print(f"  docker exec -i cli-proxy-api sh -c 'mkdir -p /app/data/auth && cat > /app/data/auth/{fname}' < {args.output_dir}/{fname}")


if __name__ == "__main__":
    main()
