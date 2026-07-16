# -*- coding: utf-8 -*-
"""适配器：将 DrissionPage_example 的 email_register 包装成 xconsole_client 接口。

提供 xconsole_client 风格的邮箱抽象，但底层复用现有的自建临时邮箱 API
（admin/new_address + /messages 端点）。

暴露的接口与 XConsoleAuthClient 预期兼容：
  - create_email() -> email_string
  - wait_for_code(timeout) -> code_string  (6-char alphanumeric)
"""

from __future__ import annotations

import sys
from pathlib import Path

# email_register 在 /workspace/ 根目录
_WORKSPACE = Path("/workspace")
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

from email_register import create_temp_email, wait_for_verification_code


class MailAdapter:
    """薄适配层：我们的自建邮箱 <-> xconsole_client 协议流程"""

    def __init__(self, timeout: int = 120):
        self._email = ""
        self._token = ""
        self._timeout = timeout

    def create(self) -> str:
        """创建临时邮箱，返回邮箱地址"""
        email, _password, mail_token = create_temp_email()
        if not email or not mail_token:
            raise RuntimeError("Unable to create temp email via admin/new_address")
        self._email = email
        self._token = mail_token
        return email

    def wait_for_code(self, timeout: int = 0) -> str:
        """轮询收件箱等待验证码"""
        t = timeout or self._timeout
        code = wait_for_verification_code(self._token, timeout=t)
        if not code:
            raise TimeoutError(f"No verification code received within {t}s for {self._email}")
        return code

    @property
    def email(self) -> str:
        return self._email


def make_mail_adapter(timeout: int = 120) -> MailAdapter:
    return MailAdapter(timeout=timeout)
