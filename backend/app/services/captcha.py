"""验证码处理业务规则：状态流转、字段校验与筛选口径都收在这里。

状态机（单向推进，重新验证受控）：

    草稿 ──提交策略验证──> 策略验证中 ──启用策略──> 已启用 ──降级策略──> 已降级
                                  ▲                                          │
                                  └────── 提交策略验证（人工队列清空后）◄────┘

降级会一次性完成三件事：冻结当时的识别服务商与超时上限、写入降级结论、把人
工复核条目放进人工打码队列；三者要么一起生效，要么都不生效。重复降级不会
再生成新的历史或队列条目。只有「已启用」的策略才能支撑自动渲染，所以降级时
会把关联的渲染配置一起停在可解释的降级态，但不会在策略重新启用后自动复原。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.authz import Actor, check_mutation
from app.store import store

MODULE = "captcha"
JSRE_MODULE = "jsrender"
REQUIRED_FIELDS = ["策略编号", "验证码类型", "识别服务商"]
STATUS_ORDER = ["草稿", "策略验证中", "已启用", "已降级"]
DRAFT, VERIFYING, ENABLED, DEGRADED = STATUS_ORDER
ACTION_RULES = {
    "提交策略验证": VERIFYING,
    "启用策略": ENABLED,
    "降级策略": DEGRADED,
    "完成人工打码": None,
    "领取人工打码名额": None,
}
ACTION_ALIASES = {
    "重新验证": "提交策略验证",
    "提交重新验证": "提交策略验证",
    "发起重新验证": "提交策略验证",
    "重新提交策略验证": "提交策略验证",
    "完成人工处理": "完成人工打码",
    "人工处理完成": "完成人工打码",
    "确认人工打码": "完成人工打码",
    "人工打码完成": "完成人工打码",
    "完成打码": "完成人工打码",
    "处理完成": "完成人工打码",
    "领取人工处理名额": "领取人工打码名额",
    "领取名额": "领取人工打码名额",
    "认领人工打码": "领取人工打码名额",
    "认领名额": "领取人工打码名额",
}
# 只有验证中/已启用的策略允许降级；草稿没有冻结的意义，已降级重复提交不重复记账。
DEGRADABLE_STATUSES = {VERIFYING, ENABLED}
NEGATIVE_ACTIONS = ["降级策略"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_action(action: str) -> str:
    action = (action or "").strip()
    return ACTION_ALIASES.get(action, action)


def manual_enabled(entry: dict[str, Any]) -> bool:
    value = entry.get("手动打码开关")
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"开启", "是", "true", "1", "on", "启用"}


def _queue(entry: dict[str, Any]) -> list[dict[str, Any]]:
    items = entry.get("人工队列")
    if not isinstance(items, list):
        items = []
        entry["人工队列"] = items
    return items


def _pending_queue(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """待处理或处理中的条目都还没给出人工打码结论，都算未清空。"""
    return [item for item in _queue(entry) if item.get("状态") in {"待处理", "处理中"}]


def _unclaimed_queue(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in _pending_queue(entry) if not item.get("领取人")]


def _next_queue_id(entry: dict[str, Any]) -> int:
    return max((int(item.get("id", 0)) for item in _queue(entry)), default=0) + 1


def _refresh_queue_summary(entry: dict[str, Any]) -> None:
    items = _queue(entry)
    pending = len(_pending_queue(entry))
    if not items:
        entry["打码等待队列"] = ""
    else:
        entry["打码等待队列"] = f"{pending} 条待处理 / 共 {len(items)} 条"


def _append_history(entry: dict[str, Any], action: str, actor: Actor, detail: dict[str, Any]) -> dict[str, Any]:
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
    return record


def _degrade_history(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """只记录真实发生的降级：重复降级在状态准入处已被拒绝，不会追加到这里。"""
    history = entry.get("降级历史")
    if not isinstance(history, list):
        history = []
        entry["降级历史"] = history
    return history


def _linked_renderings(entry: dict[str, Any]) -> list[dict[str, Any]]:
    policy_id = int(entry.get("id", 0))
    policy_no = str(entry.get("策略编号") or "")
    linked: list[dict[str, Any]] = []
    for row in store.rows(JSRE_MODULE):
        row_id = row.get("关联策略ID")
        row_no = str(row.get("关联策略编号") or "")
        if (row_id is not None and int(row_id) == policy_id) or (row_no and row_no == policy_no):
            linked.append(row)
    return linked


def _degrade_linked_renderings(entry: dict[str, Any], conclusion: str) -> int:
    """把依赖该策略的渲染配置一起停在可解释的降级态。

    这是策略降级带来的系统级安全动作：只要策略不再处于「已启用」，就不能继续
    支撑自动渲染。已上线的结果原样保留（不删除、不自动复原），只停止自动执行。
    """
    changed = 0
    for row in _linked_renderings(entry):
        if row.get("status") not in {"渲染测试中", "已上线"}:
            continue
        previous = str(row.get("status"))
        online = previous == "已上线"
        if online:
            # 已上线结果冻结留存：只停止自动执行，不改写、不删除。
            row["冻结采集结果"] = row.get("采集结果")
            row["采集结果"] = (
                "已上线结果已冻结留存（见冻结采集结果），自动渲染停止，"
                "不会随策略重新启用而自动复原"
            )
        else:
            row["采集结果"] = (
                f"未上线：渲染测试因关联验证码策略 {entry.get('策略编号')} 降级而中止，"
                "等待人工打码结论"
            )
        row["status"] = "已降级"
        row["pending"] = True
        row["abnormal"] = True
        row["降级来源"] = "验证码策略降级"
        row["降级说明"] = (
            f"关联验证码策略 {entry.get('策略编号')} 已降级；冻结服务商"
            f"「{entry.get('冻结服务商')}」、超时上限 {entry.get('冻结超时上限')} 秒。"
            + ("已上线结果已冻结留存，不会自动复原。" if online else "渲染测试已中止，等待人工打码结论。")
        )
        row["状态说明"] = (
            "自动渲染已停止：关联验证码策略不处于已启用状态"
            + ("；历史上线结果保留，重新启用策略后也不会自动复原。" if online else "。")
        )
        _append_render_history(
            row,
            "策略联动降级",
            Actor(operator="系统", role="系统"),
            {
                "原状态": previous,
                "目标状态": "已降级",
                "说明": conclusion,
            },
        )
        changed += 1
    return changed


def _append_render_history(entry: dict[str, Any], action: str, actor: Actor, detail: dict[str, Any]) -> None:
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


class CaptchaService:
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
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self.present_entry(row, actor) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int, actor: Actor | None = None) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self.present_entry(row, actor or Actor()) if row is not None else None

    def present_entry(self, row: dict[str, Any], actor: Actor) -> dict[str, Any]:
        """列表和详情共用同一份取数结果，状态、结论与可执行动作不会各算各的。"""
        view = dict(row)
        view["状态"] = row.get("status")
        view["allowed_actions"] = self.allowed_actions(row, actor)
        view["人工队列待处理"] = len(_pending_queue(row))
        view["人工队列摘要"] = row.get("打码等待队列") or ""
        view["冻结服务商"] = row.get("冻结服务商")
        view["冻结超时上限"] = row.get("冻结超时上限")
        view["降级历史"] = list(row.get("降级历史") or [])
        view["降级动作已提交"] = bool(row.get("降级历史"))
        linked = _linked_renderings(row)
        view["关联渲染配置"] = [
            {
                "id": item.get("id"),
                "配置编号": item.get("配置编号"),
                "status": item.get("status"),
                "采集结果": item.get("采集结果"),
            }
            for item in linked
        ]
        return view

    def allowed_actions(self, row: dict[str, Any], actor: Actor) -> list[str]:
        if check_mutation(row, actor) is not None:
            return []
        status = str(row.get("status"))
        actions: list[str] = []
        if status == DRAFT:
            actions.append("提交策略验证")
        if status == VERIFYING:
            actions.extend(["启用策略", "降级策略"])
        if status == ENABLED:
            actions.append("降级策略")
        if status == DEGRADED:
            if _pending_queue(row):
                actions.append("完成人工打码")
            else:
                actions.append("提交策略验证")
            if _unclaimed_queue(row):
                actions.append("领取人工打码名额")
        return actions

    def create_entry(
        self, values: dict[str, Any], actor: Actor | None = None
    ) -> tuple[dict[str, Any] | None, list[str]]:
        actor = actor or Actor()
        if actor.readonly:
            return None, ["当前身份为只读权限，不能登记验证码策略"]
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, [f"缺少必填字段：{'、'.join(missing)}"]
        rows = store.rows(MODULE)
        with store.transaction():
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            for field in ["识别超时秒", "重试次数上限", "降级策略", "手动打码开关"]:
                if values.get(field) is not None:
                    entry[field] = values.get(field)
            entry.setdefault("识别超时秒", "30")
            entry.setdefault("重试次数上限", "3")
            entry.setdefault("降级策略", "转人工打码")
            entry.setdefault("手动打码开关", "开启")
            entry["status"] = DRAFT
            entry["pending"] = True
            entry["abnormal"] = False
            entry["打码等待队列"] = ""
            entry["人工处理名额"] = int(values.get("人工处理名额") or 1)
            entry["人工队列"] = []
            entry["冻结服务商"] = None
            entry["冻结超时上限"] = None
            entry["降级结论"] = ""
            entry["history"] = []
            entry["重新验证次数"] = 0
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
                return None, f"验证码策略 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于验证码处理可执行范围"
            denied = check_mutation(entry, actor)
            if denied:
                return None, denied
            if action == "提交策略验证":
                return self._submit_verification(entry, actor)
            if action == "启用策略":
                return self._enable(entry, actor)
            if action == "降级策略":
                return self._degrade(entry, actor)
            if action == "领取人工打码名额":
                return self.claim_manual_slot(entry_id, actor, values)
            return self._complete_manual(entry, actor, values)

    # ---- 状态流转 -----------------------------------------------------
    def _submit_verification(self, entry: dict[str, Any], actor: Actor) -> tuple[dict[str, Any] | None, str]:
        current = str(entry.get("status"))
        if current == VERIFYING:
            return None, "该策略已在策略验证中，不能重复提交"
        if current == ENABLED:
            return None, "该策略已启用，如需重新验证请先执行降级"
        if current == DEGRADED:
            pending = _pending_queue(entry)
            if pending:
                return None, (
                    f"降级后不能直接回到策略验证中：人工打码队列还有 {len(pending)} 条待处理，"
                    "请先完成人工打码（动作「完成人工打码」），再发起重新验证"
                )
            if _manual_needed_but_never_run(entry):
                return None, (
                    "人工打码队列尚未完成，不能直接回到策略验证中；"
                    "请先完成人工打码（动作「完成人工打码」）"
                )
        previous = current
        if current == DEGRADED:
            entry["重新验证次数"] = int(entry.get("重新验证次数") or 0) + 1
            entry["状态说明"] = (
                f"第 {entry['重新验证次数']} 次重新验证：人工打码队列已清空，进入策略验证中"
            )
        else:
            entry["状态说明"] = "已提交策略验证"
        entry["status"] = VERIFYING
        entry["pending"] = True
        entry["abnormal"] = False
        _append_history(
            entry,
            "提交策略验证",
            actor,
            {"原状态": previous, "目标状态": VERIFYING, "说明": entry["状态说明"]},
        )
        return self.present_entry(entry, actor), (
            "验证码策略已提交策略验证" if previous != DEGRADED else "验证码策略已重新提交策略验证"
        )

    def _enable(self, entry: dict[str, Any], actor: Actor) -> tuple[dict[str, Any] | None, str]:
        current = str(entry.get("status"))
        if current != VERIFYING:
            return None, f"只有策略验证中的策略才能启用，当前状态为「{current}」"
        entry["status"] = ENABLED
        entry["pending"] = False
        entry["abnormal"] = False
        entry["状态说明"] = "策略验证通过，已启用；可支撑关联的自动渲染"
        # 同一次提交里显式告知受影响方：已降级的渲染配置不会因为策略重新启用而自动复原。
        for row in _linked_renderings(entry):
            if row.get("status") == "已降级" and row.get("降级来源") == "验证码策略降级":
                row["状态说明"] = (
                    "关联验证码策略已重新启用，但已降级的渲染结果不会自动复原；"
                    "如需恢复请由渲染侧显式提交渲染测试"
                )
        _append_history(
            entry,
            "启用策略",
            actor,
            {"原状态": current, "目标状态": ENABLED, "说明": entry["状态说明"]},
        )
        # 注意：重新启用不会自动复原此前降级的渲染配置，只能由渲染侧显式重新提测。
        return self.present_entry(entry, actor), "验证码策略已启用"

    def _degrade(self, entry: dict[str, Any], actor: Actor) -> tuple[dict[str, Any] | None, str]:
        current = str(entry.get("status"))
        if current == DEGRADED:
            return None, "该策略已处于已降级状态，重复降级不会重复生成历史或人工队列"
        if current not in DEGRADABLE_STATUSES:
            return None, f"只有策略验证中或已启用的策略才能降级，当前状态为「{current}」"

        # 冻结降级当刻的服务商与超时上限，后续修改策略字段也不会改写这份快照。
        frozen_provider = str(entry.get("识别服务商") or "")
        frozen_timeout = entry.get("识别超时秒")
        entry["冻结服务商"] = frozen_provider
        entry["冻结超时上限"] = frozen_timeout
        entry["status"] = DEGRADED
        entry["pending"] = True
        entry["abnormal"] = True

        queue = _queue(entry)
        if manual_enabled(entry):
            # 每个名额对应一条待处理条目：条目本身就是可被渲染请求领取的名额。
            capacity = max(int(entry.get("人工处理名额") or 1), 1)
            for _ in range(capacity):
                queue.append(
                    {
                        "id": _next_queue_id(entry),
                        "来源": "策略降级复核",
                        "渲染配置ID": None,
                        "渲染配置编号": None,
                        "状态": "待处理",
                        "领取人": None,
                        "领取时间": None,
                        "创建时间": _now(),
                        "完成时间": None,
                        "结论": "",
                        "操作人": actor.operator,
                    }
                )
        _refresh_queue_summary(entry)

        conclusion = (
            f"策略 {entry.get('策略编号')} 已降级：冻结服务商「{frozen_provider}」、"
            f"识别超时上限 {frozen_timeout} 秒；人工打码队列 {entry.get('打码等待队列') or '无待处理'}；"
            "关联渲染停止自动执行，已上线结果不会被自动复原。"
        )
        entry["降级结论"] = conclusion
        entry["状态说明"] = (
            "已降级：服务商与超时上限已冻结，需完成人工打码队列后才能发起重新验证"
            if _pending_queue(entry)
            else "已降级：服务商与超时上限已冻结，人工打码队列已清空后可发起重新验证"
        )
        new_degrade = {
            "序号": len(_degrade_history(entry)) + 1,
            "时间": _now(),
            "冻结服务商": frozen_provider,
            "冻结超时上限": frozen_timeout,
            "人工处理名额": int(entry.get("人工处理名额") or 1),
            "结论": conclusion,
            "操作人": actor.operator,
        }
        _degrade_history(entry).append(new_degrade)
        changed = _degrade_linked_renderings(entry, conclusion)
        # 冻结、结论、人工队列、历史与渲染联动都在同一把锁内一次提交；
        # 上面的重复降级分支已提前返回，所以每次降级只会留下一条历史。
        _append_history(
            entry,
            "降级策略",
            actor,
            {
                "原状态": current,
                "目标状态": DEGRADED,
                "冻结服务商": frozen_provider,
                "冻结超时上限": frozen_timeout,
                "关联渲染降级数": changed,
                "结论": conclusion,
            },
        )
        return self.present_entry(entry, actor), conclusion

    def claim_manual_slot(
        self,
        entry_id: int,
        actor: Actor,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """为一条渲染请求领取人工处理名额；整个判定与占用在同一把锁内完成。

        降级时建立的待处理队列条目就是名额本身：谁先把条目改成「处理中」，谁领取
        成功。两个请求同时争用时，后到的一个看到的已是占满状态，不会重复领取。
        """
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"验证码策略 {entry_id} 不存在或已归档"
            if str(entry.get("status")) != DEGRADED:
                return None, (
                    f"只有已降级的验证码策略才开放人工处理名额，"
                    f"当前状态为「{entry.get('status')}」"
                )
            # 领取会改动策略上的人工队列，因此同样受权限与归属约束。
            denied = check_mutation(entry, actor)
            if denied:
                return None, denied
            rendering_id = values.get("渲染配置ID")
            rendering_no = str(values.get("渲染配置编号") or "")
            pending = _pending_queue(entry)
            for item in pending:
                if item.get("领取人"):
                    if (
                        (rendering_id is not None and item.get("渲染配置ID") == rendering_id)
                        or (rendering_no and str(item.get("渲染配置编号") or "") == rendering_no)
                    ):
                        return None, "该渲染请求已经领取过人工处理名额，不能重复领取"
            available = _unclaimed_queue(entry)
            capacity = int(entry.get("人工处理名额") or 1)
            occupied = len([item for item in pending if item.get("领取人")])
            if capacity <= occupied or not available:
                return None, (
                    f"人工处理名额已被占用（上限 {capacity} 个），"
                    "同一名额只允许一个渲染请求领取成功"
                )
            item = available[0]
            item["领取人"] = actor.operator
            item["领取时间"] = _now()
            item["渲染配置ID"] = rendering_id
            item["渲染配置编号"] = rendering_no or None
            item["状态"] = "处理中"
            _refresh_queue_summary(entry)
            entry["状态说明"] = (
                f"渲染配置 {rendering_no or rendering_id} 已领取人工处理名额，"
                "人工打码完成前不能重新验证"
            )
            _append_history(
                entry,
                "领取人工处理名额",
                actor,
                {
                    "原状态": DEGRADED,
                    "目标状态": DEGRADED,
                    "渲染配置编号": rendering_no or rendering_id,
                    "队列条目": item.get("id"),
                    "说明": "人工处理名额已领取，同一名额不会再被其他渲染请求占用",
                },
            )
            return self.present_entry(entry, actor), (
                f"已为渲染配置 {rendering_no or rendering_id} 领取人工处理名额"
            )

    def _complete_manual(
        self,
        entry: dict[str, Any],
        actor: Actor,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        current = str(entry.get("status"))
        if current != DEGRADED:
            return None, f"只有已降级的策略才有待完成的人工打码队列，当前状态为「{current}」"
        pending = _pending_queue(entry)
        if not pending:
            return None, "该策略的人工打码队列没有待处理条目"
        target_id = values.get("队列条目") or values.get("item_id") or values.get("条目ID")
        targets = pending
        if target_id is not None:
            targets = [item for item in pending if int(item.get("id", 0)) == int(target_id)]
            if not targets:
                return None, f"人工打码队列中没有编号为 {target_id} 的待处理条目"
        # 一条人工打码条目只能对应一个渲染请求：被别的请求占用的条目不允许代签结论。
        other_claimed = [
            item
            for item in targets
            if item.get("领取人") and item.get("领取人") != actor.operator
        ]
        if other_claimed:
            return None, (
                f"队列条目 {other_claimed[0].get('id')} 已被渲染配置 "
                f"{other_claimed[0].get('渲染配置编号') or other_claimed[0].get('渲染配置ID')} 领取，"
                "不能代为完成"
            )
        now = _now()
        for item in targets:
            if not item.get("领取人"):
                item["领取人"] = actor.operator
                item["领取时间"] = now
            item["状态"] = "已完成"
            item["完成时间"] = now
            item["结论"] = f"人工打码处理完成（操作人：{actor.operator}）"
        _refresh_queue_summary(entry)
        remaining = _pending_queue(entry)
        entry["降级结论"] = (
            f"{entry.get('降级结论') or ''} 人工打码队列已处理 {len(targets)} 条，"
            f"剩余 {len(remaining)} 条待处理；"
            + ("队列已清空，可发起重新验证。" if not remaining else "队列未清空，暂不能重新验证。")
        ).strip()
        entry["状态说明"] = (
            "人工打码队列已全部完成，可发起重新验证"
            if not remaining
            else f"人工打码队列仍有 {len(remaining)} 条待处理，完成后才能重新验证"
        )
        # 队列完成不会自动恢复策略，也不会自动复原任何渲染结果。
        for row in _linked_renderings(entry):
            if row.get("status") != DEGRADED or not row.get("人工处理领取"):
                continue
            row["状态说明"] = "人工打码已完成，等待验证码策略重新验证；渲染结果不会自动复原"
            row["采集结果"] = (
                "已降级：人工打码已完成，等待策略重新验证，已上线结果不会自动复原"
                if row.get("冻结采集结果")
                else "未上线：人工打码已完成，等待策略重新验证，渲染结果不会自动复原"
            )
        return self.present_entry(entry, actor), (
            "人工打码条目已全部完成，可发起重新验证"
            if not remaining
            else f"人工打码条目已完成，仍有 {len(remaining)} 条待处理"
        )


def _manual_needed_but_never_run(entry: dict[str, Any]) -> bool:
    """降级时开启手动打码却没有任何队列记录，说明队列未按流程建立，禁止直接回退。"""
    return manual_enabled(entry) and not _queue(entry)