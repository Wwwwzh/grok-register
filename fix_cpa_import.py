#!/usr/bin/env python3
"""Fix CPA import issues in DrissionPage_example.py"""
import re

with open('/root/grok-register/DrissionPage_example.py', 'r') as f:
    content = f.read()

# 1. Add import after the last existing import
lines = content.split('\n')
last_import_line = 0
for i, line in enumerate(lines):
    if line.startswith('import ') or line.startswith('from '):
        last_import_line = i

# Insert new import after last import
new_import = 'from mint_and_push import mint_cpa_json, save_cpa_json, push_to_cliproxy'
if new_import not in content:
    lines.insert(last_import_line + 1, new_import)
    print(f"Added import at line {last_import_line + 2}: {new_import}")

# 2. Fix function calls
new_content = '\n'.join(lines)
new_content = new_content.replace('mint_cpa_build_oauth', 'mint_cpa_json')
new_content = new_content.replace('push_cpa_to_cliproxy', 'push_to_cliproxy')

# 3. Fix the unpacking issue: mint_cpa_json returns (auth_obj, raw_json) tuple
# The original call: auth_obj = mint_cpa_json(sso, proxy=...)
# Should be: auth_obj, _ = mint_cpa_json(sso, proxy=...)
new_content = re.sub(
    r'auth_obj = mint_cpa_json\(sso, proxy=',
    'auth_obj, _ = mint_cpa_json(sso, proxy=',
    new_content
)

with open('/root/grok-register/DrissionPage_example.py', 'w') as f:
    f.write(new_content)

print("Fixed DrissionPage_example.py")

# Also fix the task_3 copy if it exists
import os
task_copy = '/root/grok-register/apps/console/runtime/tasks/task_3/DrissionPage_example.py'
if os.path.exists(task_copy):
    with open(task_copy, 'r') as f:
        content = f.read()
    lines = content.split('\n')
    last_import_line = 0
    for i, line in enumerate(lines):
        if line.startswith('import ') or line.startswith('from '):
            last_import_line = i
    if new_import not in content:
        lines.insert(last_import_line + 1, new_import)
        print(f"Added import to task_3 copy")
    new_content = '\n'.join(lines)
    new_content = new_content.replace('mint_cpa_build_oauth', 'mint_cpa_json')
    new_content = new_content.replace('push_cpa_to_cliproxy', 'push_to_cliproxy')
    new_content = re.sub(
        r'auth_obj = mint_cpa_json\(sso, proxy=',
        'auth_obj, _ = mint_cpa_json(sso, proxy=',
        new_content
    )
    with open(task_copy, 'w') as f:
        f.write(new_content)
    print("Fixed task_3 copy")
