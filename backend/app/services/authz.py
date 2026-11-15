"""操作者身份、受控权限与数据归属校验。

平台里有两类限制，必须分开处理：

* 受控权限：只读角色（X-Role: 只读/viewer）没有任何变更权限；
* 数据归属：即使具备变更权限，也只能改本人名下的记录；共享记录对非归属人只读。

这两类限制最终都落到 ``check_mutation`` 一句可读的拒绝原因上，路由层只负责把
请求头解析成 :class:`Actor`。
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_OPERATOR = "值班管理员"
DEFAULT_ROLE = "操作员"
READONLY_ROLES = {"只读", "只读成员", "viewer", "readonly", "read_only", "reader"}


@dataclass(frozen=True)
class Actor:
    operator: str = DEFAULT_OPERATOR
    role: str = DEFAULT_ROLE

    @property
    def readonly(self) -> bool:
        return self.role.strip().lower() in READONLY_ROLES


def owner_of(entry: dict) -> str:
    return str(entry.get("归属人") or DEFAULT_OPERATOR)


def is_shared(entry: dict) -> bool:
    return bool(entry.get("共享"))


def check_mutation(entry: dict, actor: Actor) -> str | None:
    """返回拒绝原因；``None`` 表示允许变更。"""
    if actor.readonly:
        return f"越权操作：当前身份「{actor.role}」为只读权限，不能变更业务数据"
    owner = owner_of(entry)
    if is_shared(entry) and actor.operator != owner:
        return (
            f"越权操作：共享数据仅对归属人「{owner}」开放变更，"
            f"「{actor.operator}」当前为只读成员"
        )
    if actor.operator != owner:
        return (
            f"越权操作：「{actor.operator}」不在该记录的归属范围内，"
            f"变更只能作用于本人所属数据（归属人：{owner}）"
        )
    return None
