"""JS渲染采集接口：维护渲染配置，覆盖提交渲染测试、标记上线、标记降级等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.routers.deps import get_actor
from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.authz import Actor
from app.services.jsrender import JsrenderService

router = APIRouter(prefix="/api/jsrender", tags=["JS渲染采集"])

service = JsrenderService()

LIST_FIELDS = [
    "配置编号",
    "目标URL",
    "浏览器类型",
    "渲染等待秒",
    "滚动次数",
    "点击选择器",
    "页面等待条件",
    "截屏开关",
    "关联策略编号",
    "采集结果",
    "降级说明",
]
STATUSES = ["草稿", "渲染测试中", "已上线", "已降级"]


# 静态路径必须先于 /{entry_id} 注册，否则 export 会被当成记录编号。
@router.get("/export")
def export_entries(actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    """导出JS渲染采集清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000, actor=actor)
    return {"module": "jsrender", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、渲染测试中、已上线、已降级"),
    page: int = 1,
    size: int = 20,
    actor: Actor = Depends(get_actor),
) -> PageResult[dict]:
    """按配置编号与状态过滤JS渲染采集列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, page=page, size=size, actor=actor
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int, actor: Actor = Depends(get_actor)) -> dict:
    """读取单条渲染配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id, actor)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"渲染配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload, actor: Actor = Depends(get_actor)) -> ActionResult:
    """登记一条渲染配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values, actor)
    if missing:
        return ActionResult(ok=False, message="；".join(missing))
    return ActionResult(ok=True, message="渲染配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload, actor: Actor = Depends(get_actor)) -> ActionResult:
    """对单条渲染配置执行提交渲染测试、标记上线、标记降级、领取人工打码名额。"""
    values = dict(payload.values)
    if payload.action and not values.get("action"):
        values["action"] = payload.action
    if payload.reason and not values.get("说明"):
        values["说明"] = payload.reason
    action = str(values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, actor, values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/claim", response_model=ActionResult)
def claim_manual_slot(entry_id: int, payload: EntryPayload, actor: Actor = Depends(get_actor)) -> ActionResult:
    """为当前渲染请求领取人工处理名额；同一名额下只有一个请求能领取成功。"""
    values = dict(payload.values)
    if payload.action and not values.get("action"):
        values["action"] = "领取人工打码名额"
    entry, message = service.run_action(
        entry_id, "领取人工打码名额", actor, values
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
