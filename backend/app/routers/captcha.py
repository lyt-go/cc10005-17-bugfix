"""验证码处理接口：维护验证码策略，覆盖提交策略验证、启用策略、降级策略等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.routers.deps import get_actor
from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.authz import Actor
from app.services.captcha import CaptchaService

router = APIRouter(prefix="/api/captcha", tags=["验证码处理"])

service = CaptchaService()

LIST_FIELDS = [
    "策略编号",
    "验证码类型",
    "识别服务商",
    "识别超时秒",
    "重试次数上限",
    "降级策略",
    "手动打码开关",
    "打码等待队列",
    "冻结服务商",
    "冻结超时上限",
    "降级结论",
]
STATUSES = ["草稿", "策略验证中", "已启用", "已降级"]


# 静态路径必须先于 /{entry_id} 注册，否则 export 会被当成记录编号。
@router.get("/export")
def export_entries(actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    """导出验证码处理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000, actor=actor)
    return {"module": "captcha", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、策略验证中、已启用、已降级"),
    page: int = 1,
    size: int = 20,
    actor: Actor = Depends(get_actor),
) -> PageResult[dict]:
    """按策略编号与状态过滤验证码处理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, page=page, size=size, actor=actor
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int, actor: Actor = Depends(get_actor)) -> dict:
    """读取单条验证码策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id, actor)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"验证码策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload, actor: Actor = Depends(get_actor)) -> ActionResult:
    """登记一条验证码策略，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values, actor)
    if missing:
        return ActionResult(ok=False, message="；".join(missing))
    return ActionResult(ok=True, message="验证码策略已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload, actor: Actor = Depends(get_actor)) -> ActionResult:
    """对单条验证码策略执行提交策略验证、启用策略、降级策略、完成人工打码。"""
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
    """为一条渲染请求领取人工处理名额；名额用尽时只有先到的请求能领取成功。"""
    values = dict(payload.values)
    if payload.action and not values.get("action"):
        values["action"] = payload.action
    entry, message = service.claim_manual_slot(entry_id, actor, values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
