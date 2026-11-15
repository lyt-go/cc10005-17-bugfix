"""JS渲染采集业务规则：状态流转、字段校验与筛选口径都收在这里。

渲染配置要依赖一条「已启用」的验证码策略才能自动执行：

    草稿 / 已降级 ──提交渲染测试──> 渲染测试中 ──标记上线──> 已上线
                                        │                      │
                                        └──── 标记降级 ────────┘────> 已降级

* 提交测试与标记上线都会实时复核关联策略，策略不在「已启用」就拒绝并说明原因；
* 策略降级会把关联渲染配置一起停在「已降级」，采集结果保留并附降级说明；
* 策略重新启用不会自动复原任何渲染结果，只能由渲染侧显式重新提测。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.authz import Actor, check_mutation
from app.store import store

MODULE = "jsrender"
CAPTCHA_MODULE = "captcha"
REQUIRED_FIELDS = ["配置编号", "目标URL", "浏览器类型"]
STATUS_ORDER = ["草稿", "渲染测试中", "已上线", "已降级"]
DRAFT, TESTING, ONLINE, DEGRADED = STATUS_ORDER
ACTION_RULES = {
    "提交渲染测试": TESTING,
    "标记上线": ONLINE,
    "标记降级": DEGRADED,
    "领取人工打码名额": None,
}
ACTION_ALIASES = {
    "提交测试": "提交渲染测试",
    "重新测试": "提交渲染测试",
    "重新提交渲染测试": "提交渲染测试",
    "上线": "标记上线",
    "降级": "标记降级",
    "申请人工打码": "领取人工打码名额",
    "领取人工名额": "领取人工打码名额",
    "申请人工处理名额": "领取人工打码名额",
    "领取人工处理名额": "领取人工打码名额",
    "认领人工打码": "领取人工打码名额",
    "领取名额": "领取人工打码名额",
}
# 只有已经跑过测试或已上线的配置才谈得上降级。
DEGRADABLE_STATUSES = {TESTING, ONLINE}
NEGATIVE_ACTIONS = ["标记降级"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_action(action: str) -> str:
    action = (action or "").strip()
    return ACTION_ALIASES.get(action, action)


def linked_policy(row: dict[str, Any]) -> dict[str, Any] | None:
    policy_id = row.get("关联策略ID")
    policy_no = str(row.get("关联策略编号") or "")
    for entry in store.rows(CAPTCHA_MODULE):
        if policy_id is not None and int(entry.get("id", 0)) == int(policy_id):
            return entry
        if policy_no and str(entry.get("策略编号") or "") == policy_no:
            return entry
    return None


def _policy_gate(row: dict[str, Any], action: str) -> str | None:
    """返回不能执行的原因；``None`` 表示关联策略满足自动渲染条件。"""
    policy = linked_policy(row)
    if policy is None:
        if row.get("关联策略ID") is not None or row.get("关联策略编号"):
            return "关联验证码策略不存在或已归档，无法支撑自动渲染"
        return None
    status = str(policy.get("status"))
    if status != "已启用":
        return (
            f"关联验证码策略 {policy.get('策略编号')} 当前为「{status}」，"
            f"只有已启用的验证码策略才能支撑自动渲染，无法{action}"
        )
    return None


def _append_history(entry: dict[str, Any], action: str, actor: Actor, detail: dict[str, Any]) -> None:
    history = entry.get("history")
    if not isinstance(history, list):
        history = []
        entry["history"] = history
    record = {
        "序号": len(history) + 1,
        "动作": action,
        "原状态": detail.pop("原状态", ""),
        "目标状态": detail.pop("目标状态", ""),
        "操作人": actor.operator,
        "时间": _now(),
    }
    record.update(detail)
    history.append(record)


def _captcha_queue_items(row: dict[str, Any]) -> list[dict[str, Any]]:
    policy = linked_policy(row)
    if policy is None:
        return []
    items = policy.get("人工队列")
    if not isinstance(items, list):
        return []
    row_id = row.get("id")
    row_no = str(row.get("配置编号") or "")
    return [
        item
        for item in items
        if item.get("状态") == "待处理"
        and (
            (item.get("渲染配置ID") is not None and int(item.get("渲染配置ID")) == int(row_id or 0))
            or (item.get("渲染配置编号") and str(item.get("渲染配置编号")) == row_no)
        )
    ]


def policy_pending_count(policy: dict[str, Any] | None) -> int:
    if policy is None:
        return 0
    items = policy.get("人工队列")
    if not isinstance(items, list):
        return 0
    return sum(1 for item in items if item.get("状态") in {"待处理", "处理中"})


class JsrenderService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        actor: Actor | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        actor = actor or Actor()
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self.present_entry(row, actor) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int, actor: Actor | None = None) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self.present_entry(row, actor or Actor()) if row is not None else None

    def present_entry(self, row: dict[str, Any], actor: Actor) -> dict[str, Any]:
        """列表和详情共用同一份取数结果，状态说明与可执行动作不会两处对不上。"""
        view = dict(row)
        view["状态"] = row.get("status")
        view["allowed_actions"] = self.allowed_actions(row, actor)
        policy = linked_policy(row)
        view["关联策略状态"] = str(policy.get("status")) if policy is not None else "未关联"
        view["关联策略冻结服务商"] = policy.get("冻结服务商") if policy is not None else None
        view["关联策略冻结超时上限"] = policy.get("冻结超时上限") if policy is not None else None
        view["人工打码待处理"] = policy_pending_count(policy)
        view["人工处理名额"] = bool(row.get("人工处理领取"))
        return view

    def allowed_actions(self, row: dict[str, Any], actor: Actor) -> list[str]:
        if check_mutation(row, actor) is not None:
            return []
        status = str(row.get("status"))
        policy_ready = _policy_gate(row, "执行该动作") is None
        actions: list[str] = []
        # 关联策略不处于「已启用」时，自动渲染相关动作不可执行，页面上也不再展示，
        # 避免出现「看起来能点、点了必失败」的按钮。
        if status in {DRAFT, DEGRADED} and policy_ready:
            actions.append("提交渲染测试")
        if status == TESTING:
            if policy_ready and not _captcha_queue_items(row):
                actions.append("标记上线")
            actions.append("标记降级")
        if status == ONLINE:
            actions.append("标记降级")
        # 降级后且关联策略仍在降级态时，允许为渲染请求领取一个人工处理名额。
        if status == DEGRADED:
            policy = linked_policy(row)
            if policy is not None and str(policy.get("status")) == "已降级":
                actions.append("领取人工打码名额")
        return actions

    def create_entry(
        self, values: dict[str, Any], actor: Actor | None = None
    ) -> tuple[dict[str, Any] | None, list[str]]:
        actor = actor or Actor()
        if actor.readonly:
            return None, ["当前身份为只读权限，不能登记渲染配置"]
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, [f"缺少必填字段：{'、'.join(missing)}"]
        rows = store.rows(MODULE)
        with store.transaction():
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            for field in ["渲染等待秒", "滚动次数", "点击选择器", "页面等待条件", "截屏开关"]:
                if values.get(field) is not None:
                    entry[field] = values.get(field)
            policy = None
            if values.get("关联策略ID") is not None:
                policy = store.find(CAPTCHA_MODULE, int(values.get("关联策略ID")))
            elif values.get("关联策略编号"):
                policy = linked_policy({"关联策略编号": values.get("关联策略编号")})
            if policy is not None:
                entry["关联策略ID"] = policy.get("id")
                entry["关联策略编号"] = policy.get("策略编号")
            entry["status"] = DRAFT
            entry["pending"] = True
            entry["abnormal"] = False
            entry["采集结果"] = "渲染配置尚未提交测试"
            entry["降级说明"] = ""
            entry["状态说明"] = ""
            entry["history"] = []
            entry["归属人"] = actor.operator
            entry["共享"] = bool(values.get("共享"))
            rows.append(entry)
        return self.present_entry(entry, actor), []

    def run_action(
        self,
        entry_id: int,
        action: str,
        actor: Actor | None = None,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        actor = actor or Actor()
        action = _normalize_action(action)
        values = values or {}
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"渲染配置 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于JS渲染采集可执行范围"
            denied = check_mutation(entry, actor)
            if denied:
                return None, denied
            if action == "领取人工打码名额":
                return self._claim_manual(entry, actor, values)
            if action == "提交渲染测试":
                return self._submit_test(entry, actor)
            if action == "标记上线":
                return self._mark_online(entry, actor)
            return self._degrade(entry, actor, values)

    # ---- 状态流转 -----------------------------------------------------
    def _submit_test(self, entry: dict[str, Any], actor: Actor) -> tuple[dict[str, Any] | None, str]:
        current = str(entry.get("status"))
        if current == TESTING:
            return None, "该渲染配置已在渲染测试中，不能重复提交"
        if current == ONLINE:
            return None, "该配置已上线，已上线结果不会自动复原；如需重新测试请先标记降级"
        blocked = _policy_gate(entry, "提交渲染测试")
        if blocked:
            return None, blocked
        if current == DEGRADED:
            # 显式重新提测：历史结果保留，但不自动回到已上线。
            entry["采集结果"] = "渲染测试进行中，等待重新验证结果"
            entry["状态说明"] = "已重新提交渲染测试，此前降级结果保留且不会自动复原"
            entry["降级说明"] = f"{entry.get('降级说明') or '曾因降级中止'}；已重新提测"
        else:
            entry["采集结果"] = "渲染测试进行中"
            entry["状态说明"] = "已提交渲染测试"
        entry["status"] = TESTING
        entry["pending"] = True
        entry["abnormal"] = False
        _append_history(
            entry,
            "提交渲染测试",
            actor,
            {"原状态": current, "目标状态": TESTING, "说明": entry["状态说明"]},
        )
        return self.present_entry(entry, actor), "渲染配置已提交渲染测试"

    def _mark_online(self, entry: dict[str, Any], actor: Actor) -> tuple[dict[str, Any] | None, str]:
        current = str(entry.get("status"))
        if current != TESTING:
            return None, f"只有渲染测试中的配置才能标记上线，当前状态为「{current}」"
        blocked = _policy_gate(entry, "标记上线")
        if blocked:
            return None, blocked
        pending_manual = _captcha_queue_items(entry)
        if pending_manual:
            return None, (
                f"人工打码队列仍有 {len(pending_manual)} 条待处理，"
                "需完成人工打码后才能标记上线"
            )
        entry["status"] = ONLINE
        entry["pending"] = False
        entry["abnormal"] = False
        entry["采集结果"] = "自动渲染采集已产出结果"
        entry["状态说明"] = "渲染测试通过，已上线"
        entry["降级说明"] = ""
        # 显式重新上线才清除冻结标记；策略侧重新启用永远不会触发这一步。
        entry.pop("冻结采集结果", None)
        entry.pop("降级来源", None)
        entry.pop("人工处理领取", None)
        _append_history(
            entry,
            "标记上线",
            actor,
            {"原状态": current, "目标状态": ONLINE, "说明": entry["状态说明"]},
        )
        return self.present_entry(entry, actor), "渲染配置已标记上线"

    def _degrade(
        self, entry: dict[str, Any], actor: Actor, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        current = str(entry.get("status"))
        if current == DEGRADED:
            return None, "该渲染配置已处于已降级状态，重复降级不会重复生成历史"
        if current not in DEGRADABLE_STATUSES:
            return None, f"只有渲染测试中或已上线的配置才能降级，当前状态为「{current}」"
        online = current == ONLINE
        if online:
            # 已上线结果冻结留存：只停止自动执行，不改写、不删除。
            entry["冻结采集结果"] = entry.get("采集结果")
            entry["采集结果"] = "已上线结果已冻结留存（见冻结采集结果），自动渲染停止，不会自动复原"
        else:
            entry["采集结果"] = "渲染测试中止，结果为未上线"
        reason = str(values.get("说明") or values.get("reason") or "").strip()
        policy = linked_policy(entry)
        provider = policy.get("冻结服务商") if policy is not None else None
        timeout = policy.get("冻结超时上限") if policy is not None else None
        entry["status"] = DEGRADED
        entry["pending"] = True
        entry["abnormal"] = True
        entry["降级说明"] = (
            f"渲染配置已降级：{reason or '人工标记降级'}；"
            f"关联验证码策略 {policy.get('策略编号') if policy is not None else '—'}，"
            f"冻结服务商「{provider if provider is not None else '—'}」、"
            f"超时上限 {timeout if timeout is not None else '—'} 秒；"
            + ("已上线结果冻结留存，不会自动复原。" if online else "等待人工打码结论。")
        )
        entry["状态说明"] = (
            "已降级：自动渲染已停止，已上线结果冻结留存，不会自动复原"
            if online
            else "已降级：自动渲染已停止，等待人工打码结论"
        )
        _append_history(
            entry,
            "标记降级",
            actor,
            {
                "原状态": current,
                "目标状态": DEGRADED,
                "说明": entry["降级说明"],
            },
        )
        return self.present_entry(entry, actor), entry["降级说明"]

    def _claim_manual(
        self, entry: dict[str, Any], actor: Actor, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        # 领取名额真正的并发互斥在验证码策略上完成，这里只做渲染侧的归属校验。
        from app.services import captcha as captcha_service

        policy = linked_policy(entry)
        if policy is None:
            return None, "该渲染配置未关联验证码策略，无法领取人工处理名额"
        if str(entry.get("status")) != DEGRADED:
            return None, (
                f"只有已降级的渲染配置才需要人工打码名额，当前状态为「{entry.get('status')}」"
            )
        result, message = captcha_service.CaptchaService().claim_manual_slot(
            int(policy.get("id", 0)),
            actor,
            {"渲染配置ID": entry.get("id"), "渲染配置编号": entry.get("配置编号")},
        )
        if result is None:
            return None, message
        entry["人工处理领取"] = True
        entry["人工处理名额"] = True
        entry["人工处理名额状态"] = "已领取"
        entry["状态说明"] = "已领取人工处理名额，等待人工打码完成；渲染结果不会自动复原"
        entry["采集结果"] = (
            f"{entry.get('采集结果') or ''}；已领取人工处理名额，等待人工打码完成"
        ).lstrip("；")
        return self.present_entry(entry, actor), message