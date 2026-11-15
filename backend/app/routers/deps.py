"""路由层公共依赖：从请求头解析当前操作者与受控角色。"""
from __future__ import annotations

from urllib.parse import unquote

from fastapi import Request

from app.services.authz import DEFAULT_OPERATOR, DEFAULT_ROLE, Actor


def _header(request: Request, *names: str) -> str | None:
    for name in names:
        value = request.headers.get(name)
        if value:
            # 兼容前端把中文操作者/角色做百分号编码后放进请求头的情况。
            return unquote(value).strip()
    return None


def get_actor(request: Request) -> Actor:
    operator = _header(request, "x-operator", "x-user", "x-username")
    role = _header(request, "x-role", "x-permission")
    return Actor(
        operator=operator or DEFAULT_OPERATOR,
        role=role or DEFAULT_ROLE,
    )
