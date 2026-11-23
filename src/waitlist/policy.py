"""候补政策:资格、优先顺序、保留比例与可解释的拒批原因。

政策字段(见 policies.csv):
- ordering: logged_priority(legacy)/ protected_first(A)/ longest_wait(B)
- expiry_minutes: 新报价有效期
- max_regular_wait_minutes: 普通候补自首次登记起的最长允许等待
- regular_floor: 当期席位中保留给普通候补的比例
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import Policy, Slot, WaitRequest
from .occupancy import SlotOccupancy

# 原因码:用于审计"为什么某规则看似填写率更好仍不能用于某场次"
REASON = {
    "NOT_ALTERNATIVE": "该候补未把此场登记为可接受选项",
    "WAIT_CAP": "普通候补等待时长超过该政策允许的最长等待分钟数",
    "NO_FREE_SEAT": "决策时点无足够可用席位(含未到期报价占席)",
    "SPLIT_FORBIDDEN": "家庭未登记可拆意愿,不允许部分改签",
    "FLOOR_PROTECTED_HEADROOM": "普通候补保留比例(regular_floor)已满,保障票不得再占",
    "UNSUPPORTED_NO_RANDOMIZATION": "该候选不在任何有概率记录的送达机会内,反事实响应无支撑",
    "CHAIN_UNSUPPORTED": "名额依赖另一笔换票腾出的原票,单组效果不能外推到交换链",
    "POLICY_NOT_EFFECTIVE": "政策在该决策时点尚未生效",
    "GRANDFATHERED_OFFER": "既有报价按发出时约定继续有效,新政策不得改动",
    "SLICE_TOO_SMALL": "支撑范围不足,只能给界限,规则留作试行",
    "UNOBSERVED_PREFERENCE": "旧规则下未收到报价(或未送达),接受意愿未被观察,不得假设接受",
}


@dataclass
class DecisionContext:
    """单个决策时点(某场次放号)的上下文。"""

    ds: Dataset
    policy: Policy
    at: datetime
    occupancy: dict[str, SlotOccupancy]

    def slot(self, slot_id: str) -> Slot:
        return self.ds.slots[slot_id]


def wait_minutes(req: WaitRequest, at: datetime) -> float:
    """候补时长以首次登记(joined_at)为准。"""
    return (at - req.joined_at).total_seconds() / 60.0


def regular_reserve(slot: Slot, occ: SlotOccupancy, floor: float) -> int:
    """当期席位中为普通候补保留的席位数(向上取整保证下限)。"""
    return math.ceil(slot.capacity * floor)


def protected_headroom(
    slot: Slot, occ: SlotOccupancy, floor: float
) -> int:
    """保障票最多可用的席位 = 容量 - 普通保留席。"""
    return slot.capacity - regular_reserve(slot, occ, floor)


def check_basic_eligibility(
    req: WaitRequest,
    slot_id: str,
    ctx: DecisionContext,
    seats_needed: int | None = None,
) -> list[str]:
    """返回拒绝原因码列表;空列表表示通过基本资格(不含排序与送达支撑)。"""
    reasons: list[str] = []
    need = req.seats if seats_needed is None else seats_needed

    if slot_id not in req.alternatives:
        reasons.append("NOT_ALTERNATIVE")
    occ = ctx.occupancy.get(slot_id)
    if occ is None:
        reasons.append("NO_FREE_SEAT")
    elif occ.free < need:
        reasons.append("SPLIT_FORBIDDEN" if need < req.seats
                       and not req.may_split else "NO_FREE_SEAT")
    if need < req.seats and not req.may_split:
        if "SPLIT_FORBIDDEN" not in reasons:
            reasons.append("SPLIT_FORBIDDEN")
    if not req.is_protected:
        if wait_minutes(req, ctx.at) > ctx.policy.max_regular_wait_minutes:
            reasons.append("WAIT_CAP")
    # regular_floor:保障票只能使用普通保留席之外的容量。
    # headroom = 容量 - 普通保留席;本次保障需求超过 headroom 即拒批。
    if req.is_protected and occ is not None:
        headroom = protected_headroom(
            ctx.slot(slot_id), occ, ctx.policy.regular_floor
        )
        if need > headroom:
            reasons.append("FLOOR_PROTECTED_HEADROOM")
    return reasons


def ordering_key(
    req: WaitRequest, policy: Policy, at: datetime
) -> tuple:
    """排序键(升序,越靠前越优先)。legacy 用登记顺序;A 保障优先;B 最长等待优先。"""
    if policy.ordering == "protected_first":
        # 保障票先处理(工作人员可先处理保障票),组内按登记先后保证确定性
        return (1 if req.is_protected else 0, req.joined_at, req.request)
    if policy.ordering == "longest_wait":
        # 等待越久越优先:joined_at 越早越靠前
        return (req.joined_at, req.request)
    if policy.ordering == "logged_priority":
        return (req.joined_at, req.request)
    raise ValueError(f"未知排序口径 {policy.ordering}")


def rank_candidates(
    candidates: list[WaitRequest], policy: Policy, at: datetime
) -> list[WaitRequest]:
    return sorted(candidates, key=lambda r: ordering_key(r, policy, at))


def policy_effective(policy: Policy, at: datetime) -> bool:
    return at >= policy.effective_at


def expiry_for(policy: Policy, sent_at: datetime) -> datetime:
    return sent_at + timedelta(minutes=policy.expiry_minutes)
