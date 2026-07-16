#!/usr/bin/env python3
"""
批量 mint CPA JSON 并推送到 CLIProxyAPI auth-dir。
在 Docker 容器 grok-register-grok2api-1 内执行（共享网络环境 + 已有依赖）。
用法:
  docker exec grok-register-grok2api-1 python3 /workspace/batch_mint_push.py
  docker exec grok-register-grok2api-1 python3 /workspace/batch_mint_push.py --dry-run
"""

import argparse
import asyncio
import json
import os
import sys
import time
import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 默认配置
CLIPROXY_AUTH_DIR = "/workspace/output/cpa_auths"
OUTPUT_DIR = "/workspace/output/cpa_auths"

GROK2API_URL = "http://127.0.0.1:8000/v1/admin/tokens"


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


async def build_cpa_oauth(sso_token):
    """
    使用 grok2api 的 build_oauth 模块，用 SSO cookie 生成 CPA OAuth JSON。
    返回 dict: {"email": "xxx", "expires_in": 3600, ...}，失败返回 None。
    """
    from grok_client.build_token import build_oauth

    try:
        result = await build_oauth(cookie={"sso": sso_token})
        if isinstance(result, dict) and result:
            return result
        print(f"  build_oauth 返回空结果")
        return None
    except Exception as e:
        print(f"  build_oauth 异常: {e}")
        return None


def save_cpa_json(auth_obj, email, output_dir):
    """保存 CPA JSON 到文件"""
    os.makedirs(output_dir, exist_ok=True)
    safe_name = email.replace("@", "_").replace(".", "_")
    filepath = os.path.join(output_dir, f"xai-{safe_name}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(auth_obj, f, ensure_ascii=False, indent=2)
    return filepath


def push_to_cliproxy(auth_obj, email, auth_dir, dry_run=False):
    """
    推送认证文件到 CLIProxyAPI auth-dir，自动去重。
    去重逻辑：按 email 字段匹配，已有则覆盖。
    """
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

    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(auth_obj, f, ensure_ascii=False, indent=2)

    action = "更新" if existing else "新增"
    print(f"    {action}: {target_file}")
    return target_file


def push_to_cliproxy_docker(auth_obj, email, dry_run=False):
    """
    把 JSON 推送到 CLIProxyAPI Docker 容器内的 auth-dir。
    需要通过 docker cp 写入 /app/data/auth/ 目录。
    """
    import tempfile
    import subprocess

    safe_name = email.replace("@", "_").replace(".", "_")
    tmp_file = os.path.join(tempfile.gettempdir(), f"cpa_{safe_name}.json")

    if dry_run:
        print(f"  [DRY-RUN] Docker auth-dir: /app/data/auth/xai-{safe_name}.json")
        return

    # 写入临时文件
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(auth_obj, f, ensure_ascii=False, indent=2)

    # docker cp 到容器内
    try:
        subprocess.run(
            ["docker", "cp", tmp_file, f"cli-proxy-api:/app/data/auth/xai-{safe_name}.json"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        print(f"    推送到 Docker: /app/data/auth/xai-{safe_name}.json")
    except Exception as e:
        print(f"    推送 Docker 失败: {e}")

    # 清理临时文件
    try:
        os.remove(tmp_file)
    except Exception:
        pass


async def main_async(args):
    print(f"[*] 从 grok2api 获取 SSO token...")
    tokens = get_sso_tokens()
    if not tokens:
        print("[Error] 没有获取到 SSO token，退出")
        sys.exit(1)

    print(f"[*] 共 {len(tokens)} 个 SSO token，开始处理...\n")
    success = 0
    failed = 0

    for i, sso in enumerate(tokens):
        print(f"[{i+1}/{len(tokens)}] 处理 SSO (2FA): {sso[:30]}...")

        try:
            auth_obj = await build_cpa_oauth(sso)
        except Exception as e:
            print(f"  ❌ build_oauth 异常: {e}")
            failed += 1
            continue

        if not auth_obj:
            print(f"  ❌ mint 失败\n")
            failed += 1
            continue

        email = auth_obj.get("email", "unknown")
        print(f"  ✅ 成功 | email={email}")

        # 保存本地
        save_cpa_json(auth_obj, email, args.output_dir)

        # 推送到 CLIProxyAPI Docker 容器
        if not args.no_cliproxy:
            push_to_cliproxy_docker(auth_obj, email, dry_run=args.dry_run)
            # 同时也存一份到本地 cliproxy_auth_dir（如果需要）
            if args.cliproxy_auth_dir:
                push_to_cliproxy(auth_obj, email, args.cliproxy_auth_dir, dry_run=args.dry_run)

        success += 1

        if i < len(tokens) - 1:
            await asyncio.sleep(1)

    # 同时把文件也写入 /root/openclaw-workdir/auth/ 目录（在宿主上）
    # 通过 docker cp 从容器内复制出来
    print(f"\n{'='*50}")
    print(f"[*] 完成! 成功: {success}/{len(tokens)}, 失败: {failed}")
    if args.dry_run:
        print("[*] 这是 dry-run，没有实际写入")
    else:
        print(f"[*] 本地 JSON: {args.output_dir}")
        if not args.no_cliproxy:
            print(f"    → Docker CLIProxyAPI: /app/data/auth/")
        print("[*] CLIProxyAPI 会自动热加载新文件，无需重启")


def main():
    parser = argparse.ArgumentParser(description="批量 CPA mint + 推送到 CLIProxyAPI")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不实际写入")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="本地 JSON 输出目录")
    parser.add_argument("--cliproxy-auth-dir", default="/root/openclaw-workdir/auth",
                        help="宿主机上 CLIProxyAPI auth-dir 路径")
    parser.add_argument("--no-cliproxy", action="store_true", help="不推送到 CLIProxyAPI")
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
