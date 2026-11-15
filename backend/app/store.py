"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from threading import RLock
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        # 深拷贝示例数据：人工队列、history 这类嵌套结构不能在多个 Store 实例
        # 或原始 SEED_ROWS 之间共享，否则一次降级会污染下一次启动的初始状态。
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: deepcopy(rows) for name, rows in SEED_ROWS.items()
        }
        # 跨模块的状态流转（验证码降级 -> 渲染配置停用）必须落在同一次提交里，
        # 这里用可重入锁把「读-改-写」包成一个事务，避免并发请求读到半截状态。
        self.lock = RLock()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """把一次业务动作变成原子提交：中途出现意外异常时回滚全部改动。

        内存仓库的一次动作可能同时改策略、人工队列、降级结论和关联渲染配置；
        任何一步失败都不允许留下半截状态，所以这里先留快照，异常时整体还原。
        """
        with self.lock:
            snapshot = deepcopy(self._tables)
            try:
                yield
            except Exception:
                self._tables = snapshot
                raise

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
