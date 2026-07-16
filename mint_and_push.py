#!/usr/bin/env python3
"""
批量 CPA mint + 推送到 CLIProxyAPI auth-dir。

DEPRECATED: https://grok.com/rest/app/mint 已返回 404。
请优先使用 xconsole_client.xai_oauth.complete_build_oauth / 任务内 Build OAuth。
读取 grok2api 的现有 SSO token，逐个 mint CPA JSON，推送到 CLIProxyAPI auth-dir。
"""

import argparse
import json
import os
import sys
import time
import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 默认配置
GROK2API_URL = "http://127.0.0.1:8000/v1/admin/tokens"
CLIPROXY_AUTH_DIR = "/root/openclaw-workdir/auth"
OUTPUT_DIR = "./output/cpa_auths"
CPA_PROXY = None  # 可选代理，如 socks5://warp:1080
DRY_RUN = False


def get_sso_tokens(grok2api_url=GROK2API_URL):
    """从 grok2api 获取现有 SSO token 列表"""
    try:
        resp = requests.get(grok2api_url, timeout=10)
        if resp.status_code != 200:
            print(f"[Error] 获取 token 失败: HTTP {resp.status_code}")
            return []
        data = resp.json()
        tokens = data.get("ssoBasic", [])
        return [str(t).strip() for t in tokens if str(t).strip()]
    except Exception as e:
        print(f"[Error] 获取 token 异常: {e}")
        return []


def mint_cpa_json(sso_token, proxy=None):
    """
    用 SSO token 调 grok.com REST API mint CPA Build OAuth JSON。
    返回 (auth_obj, raw_json_str) 或 (None, None)。
    """
    cookies = {"sso": sso_token}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        # 步骤1: mint - 获取 build token
        resp = requests.post(
            "https://grok.com/rest/app/mint",
            cookies=cookies,
            headers=headers,
            timeout=30,
            proxies=proxies,
            verify=False,
        )
        if resp.status_code != 200:
            print(f"  [Mint] HTTP {resp.status_code}: {resp.text[:200]}")
            return None, None

        mint_data = resp.json()
        build_token = mint_data.get("buildToken") or mint_data.get("token", "")
        if not build_token:
            print(f"  [Mint] 响应中无 buildToken: {json.dumps(mint_data, ensure_ascii=False)[:200]}")
            return None, None

        # 步骤2: build OAuth - 用 build token 生成 OAuth 凭证
        resp2 = requests.post(
            "https://grok.com/rest/build/oauth",
            json={"token": build_token},
            headers=headers,
            timeout=30,
            proxies=proxies,
            verify=False,
        )
        if resp2.status_code != 200:
            print(f"  [OAuth] HTTP {resp2.status_code}: {resp2.text[:200]}")
            return None, None

        oauth_data = resp2.json()
        raw_json = resp2.text

        # 提取 email（如果响应里有）
        email = oauth_data.get("email", "unknown")
        if email == "unknown":
            # 尝试用 build token 查 me
            try:
                me_resp = requests.get(
                    "https://grok.com/rest/app/me",
                    cookies={"sso": sso_token},
                    headers=headers,
                    timeout=15,
                    proxies=proxies,
                    verify=False,
                )
                if me_resp.status_code == 200:
                    email = me_resp.json().get("email", "unknown")
            except Exception:
                pass

        auth_obj = {"email": email, **oauth_data}
        return auth_obj, json.dumps(auth_obj, ensure_ascii=False, indent=2)

    except requests.exceptions.ConnectionError as e:
        print(f"  [网络] 连接失败: {e}")
        return None, None
    except Exception as e:
        print(f"  [异常] {e}")
        return None, None


def save_cpa_json(auth_obj, output_dir=OUTPUT_DIR):
    """保存 CPA 认证 JSON 到本地文件"""
    os.makedirs(output_dir, exist_ok=True)
    email = auth_obj.get("email", "unknown")
    safe_name = email.replace("@", "_").replace(".", "_")
    filepath = os.path.join(output_dir, f"xai-{safe_name}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(auth_obj, f, ensure_ascii=False, indent=2)
    return filepath


def push_to_cliproxy(auth_obj, auth_dir=CLIPROXY_AUTH_DIR, dry_run=False):
    """
    推送认证文件到 CLIProxyAPI auth-dir，自动去重。
    去重逻辑：按 email 字段匹配，已有则覆盖；同时扫描目录删除同 email 的旧文件。
    """
    email = auth_obj.get("email", "unknown")
    safe_name = email.replace("@", "_").replace(".", "_")
    target_file = os.path.join(auth_dir, f"xai-{safe_name}.json")

    if dry_run:
        print(f"  [DRY-RUN] 将写入: {target_file}")
        return target_file

    try:
        os.makedirs(auth_dir, exist_ok=True)
    except Exception:
        pass

    # 扫描目录，删除同 email 的旧文件
    existing = False
    if os.path.isdir(auth_dir):
        for fname in os.listdir(auth_dir):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(auth_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    old = json.load(f)
                if isinstance(old, dict) and old.get("email") == email:
                    os.remove(fpath)
                    print(f"    去重: 删除旧文件 {fname}")
                    existing = True
                    break
            except Exception:
                continue

    # 写入
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(auth_obj, f, ensure_ascii=False, indent=2)

    action = "更新" if existing else "新增"
    print(f"    {action}: {target_file}")
    return target_file


def main():
    parser = argparse.ArgumentParser(description="批量 CPA mint + 推送到 CLIProxyAPI")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不实际写入")
    parser.add_argument("--grok2api-url", default=GROK2API_URL, help="Grok2API token 接口地址")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="本地 JSON 输出目录")
    parser.add_argument("--cliproxy-auth-dir", default=CLIPROXY_AUTH_DIR, help="CLIProxyAPI auth-dir 路径")
    parser.add_argument("--proxy", default=None, help="HTTP/SOCKS5 代理（如 socks5://warp:1080）")
    parser.add_argument("--no-cliproxy", action="store_true", help="不推送到 CLIProxyAPI，仅保存本地")
    parser.add_argument("--delay", type=float, default=1.0, help="请求间隔（秒）")
    args = parser.parse_args()

    print(f"[*] 从 {args.grok2api_url} 获取 SSO token...")
    tokens = get_sso_tokens(args.grok2api_url)
    if not tokens:
        print("[Error] 没有获取到 SSO token，退出")
        sys.exit(1)

    print(f"[*] 共 {len(tokens)} 个 SSO token，开始处理...\n")
    success = 0
    failed = 0

    for i, sso in enumerate(tokens):
        print(f"[{i+1}/{len(tokens)}] 处理 SSO: {sso[:30]}...")
        auth_obj, raw_json = mint_cpa_json(sso, proxy=args.proxy)

        if not auth_obj:
            failed += 1
            print(f"  ❌ 失败\n")
            continue

        email = auth_obj.get("email", "unknown")
        print(f"  ✅ mint 成功 | email={email}")

        # 保存本地
        if args.dry_run:
            print(f"  [DRY-RUN] 将保存到: {args.output_dir}/xai-{email.replace('@','_').replace('.','_')}.json")
        else:
            local_path = save_cpa_json(auth_obj, args.output_dir)
            print(f"  本地: {local_path}")

        # 推送到 CLIProxyAPI
        if not args.no_cliproxy:
            push_to_cliproxy(auth_obj, args.cliproxy_auth_dir, dry_run=args.dry_run)

        success += 1

        # 请求间隔
        if i < len(tokens) - 1 and args.delay > 0:
            time.sleep(args.delay)

    print(f"\n{'='*50}")
    print(f"[*] 完成! 成功: {success}/{len(tokens)}, 失败: {failed}")
    if args.dry_run:
        print("[*] 这是 dry-run，没有实际写入任何文件")
    else:
        print(f"[*] 本地 JSON: {args.output_dir}")
        if not args.no_cliproxy:
            print(f"[*] CLIProxyAPI auth-dir: {args.cliproxy_auth_dir}")
            print("[*] CLIProxyAPI 会自动热加载新文件，无需重启")


if __name__ == "__main__":
    main()
