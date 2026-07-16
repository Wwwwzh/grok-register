#!/bin/bash
# 去重导入所有 SSO token 到 chenyme-grok2api
set -e

echo "=== 1. 收集去重所有 SSO token ==="
python3 << 'EOF'
import json, os

all_tokens = set()

for task in ['task_1', 'task_2', 'task_3']:
    f = f'/root/grok-register/apps/console/runtime/tasks/{task}/sso/{task}.txt'
    if os.path.exists(f):
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith('eyJ'):
                    all_tokens.add(line)

with open('/root/grok-register/runtime/grok2api/data/token.json') as f:
    data = json.load(f)
for entry in data.get('ssoBasic', []):
    all_tokens.add(entry['token'])

print(f"去重后: {len(all_tokens)} 个 token")

sorted_tokens = sorted(all_tokens)
doc = {
    'provider': 'grok_web',
    'accounts': [{'name': f'Grok Web {i+1}', 'sso_token': t} for i, t in enumerate(sorted_tokens)]
}
with open('/tmp/sso_import.json', 'w') as f:
    json.dump(doc, f)
print(f"导入文件已生成: {len(doc['accounts'])} 个账号")
EOF

echo ""
echo "=== 2. 检查导入前数量 ==="
JWT=$(curl -s -X POST http://127.0.0.1:8000/api/admin/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}' | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['tokens']['accessToken'])")

BEFORE=$(curl -s "http://127.0.0.1:8000/api/admin/v1/accounts?provider=grok_web&page=1&pageSize=1" \
  -H "Authorization: Bearer $JWT" | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['total'])")
echo "导入前 web 账号: $BEFORE"

echo ""
echo "=== 3. 执行导入 ==="
curl -s -N -X POST "http://127.0.0.1:8000/api/admin/v1/accounts/web/import" \
  -H "Authorization: Bearer $JWT" \
  -F "files=@/tmp/sso_import.json" 2>&1 | tail -3

echo ""
echo "=== 4. 检查导入后数量 ==="
AFTER=$(curl -s "http://127.0.0.1:8000/api/admin/v1/accounts?provider=grok_web&page=1&pageSize=1" \
  -H "Authorization: Bearer $JWT" | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['total'])")
echo "导入后 web 账号: $AFTER"
echo "新增: $((AFTER - BEFORE))"

echo ""
echo "=== 5. 账号总览 ==="
curl -s "http://127.0.0.1:8000/api/admin/v1/accounts/summary" \
  -H "Authorization: Bearer $JWT" | python3 -c "
import json,sys
d=json.load(sys.stdin)['data']
print(f'总账号: {d[\"total\"]}')
print(f'可用: {d[\"available\"]}')
print(f'禁用: {d[\"issues\"][\"disabled\"]}')
print(f'需重认证: {d[\"issues\"][\"reauthRequired\"]}')
print(f'冷却: {d[\"recovery\"][\"cooldown\"]}')
"
