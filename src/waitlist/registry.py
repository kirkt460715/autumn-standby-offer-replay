"""下一轮放号核准登记(2026-12-05 NEXT 场)。

承诺规则(见 data-contract.md 与用户要求):
- 出票主管按规则承诺优先顺序、公平等待条件、当前未到期报价;
- 既有报价按发出时的约定有效,运营后来提高优先级不改变其条款;
- 已用于另一笔换票的原票不能重复释放;
- 无随机机会/分派概率记录的轮次,候选规则只能登记为试行,不得承诺反事实效果。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import Dataset, Policy
from .occupancy import Ledger
from .policy import (
    DecisionContext,
    REASON,
    check_basic_eligibility,
    expiry_for,
    policy_effective,
    rank_candidates,
    regular_reserve,
    wait_minutes,
)


@dataclass
class GrandfatheredOffer:
    offer: str
    request: str
    family: str
    slot: str
    seats: int
    sent_at: datetime
    expires: datetime
    terms: str
    standing_at_effective: str  # 在新生效时点仍占席 / 已终局


@dataclass
class NextRoundSlotDecision:
    slot: str
    capacity: int
    priority_class: str
    base_orders: int
    grandfathered_holds: int
    free_at_effective: int
    regular_reserve_seats: int
    protected_headroom: int


@dataclass
class PolicyApproval:
    policy: str
    approvable: bool
    pilot: bool
    reasons: list[str]
    ordering: str
    expiry_minutes: int
    max_regular_wait_minutes: int
    regular_floor: float
    effective_at: datetime
    applicable_slots: list[str]
    # 各候补在该政策下的资格原因(可解释)
    request_decisions: list[dict] = field(default_factory=list)


@dataclass
class Registry:
    round_day: str
    grandfathered: list[GrandfatheredOffer]
    slot_decisions: list[NextRoundSlotDecision]
    approvals: list[PolicyApproval]
    consumed_origins: list[str]
    evidence_note: str


def build_registry(ds: Dataset) -> Registry:
    next_policies = [p for p in ds.policies.values() if p.policy != "legacy"]
    effective_at = min(p.effective_at for p in next_policies)
    round_day = effective_at.date().isoformat()
    next_slots = [
        s for s in ds.slots.values() if s.day == round_day
    ]

    ledger = Ledger(ds)
    snap = ledger.snapshot_at(effective_at)

    # 1. 未到期报价:在生效时点仍处于占席状态者,按 legacy 条款继续有效
    grandfathered: list[GrandfatheredOffer] = []
    for ev in ds.events:
        if ev.kind != "sent":
            continue
        slot = ds.slots.get(ev.slot)
        if slot is None or slot.day != round_day:
            continue
        # 生效时点是否仍未到期且无终局
        terminal = next(
            (
                e for e in ds.events
                if e.offer == ev.offer
                and e.kind in ("accepted", "declined", "expired",
                               "not_delivered", "pending")
            ),
            None,
        )
        still_open = (
            terminal is not None
            and terminal.kind == "pending"
            and terminal.at <= effective_at < ev.expires
        ) or (terminal is None and ev.expires > effective_at)
        if not still_open:
            continue
        req = ds.requests[ev.request]
        grandfathered.append(
            GrandfatheredOffer(
                offer=ev.offer,
                request=ev.request,
                family=req.family,
                slot=ev.slot,
                seats=ev.seats,
                sent_at=ev.at,
                expires=ev.expires,
                terms=f"legacy 有效期至 {ev.expires.isoformat()}",
                standing_at_effective="占席中(按旧约定保留至 expires)",
            )
        )

    # 2. 各 NEXT 场的容量与保留口径
    slot_decisions: list[NextRoundSlotDecision] = []
    for slot in sorted(next_slots, key=lambda s: s.slot):
        occ = snap[slot.slot]
        holds = sum(o.seats for o in grandfathered if o.slot == slot.slot)
        for p in next_policies:
            reserve = regular_reserve(slot, occ, p.regular_floor)
            break  # 每个政策的 floor 不同,在审批明细里分别展开
        slot_decisions.append(
            NextRoundSlotDecision(
                slot=slot.slot,
                capacity=slot.capacity,
                priority_class=slot.priority_class,
                base_orders=occ.confirmed_seats,
                grandfathered_holds=holds,
                free_at_effective=occ.free,
                regular_reserve_seats=reserve,
                protected_headroom=slot.capacity - reserve,
            )
        )

    # 3. 政策审批:无 randomization 记录 -> 全部留作试行
    has_round_randomization = any(
        c.at.date().isoformat() == round_day for c in ds.draws
    )
    round_requests = [
        r for r in ds.requests.values()
        if r.joined_at.date().isoformat() == round_day
    ]
    approvals: list[PolicyApproval] = []
    for p in sorted(next_policies, key=lambda x: x.policy):
        reasons: list[str] = []
        if not policy_effective(p, effective_at):
            reasons.append("POLICY_NOT_EFFECTIVE")
        if not has_round_randomization:
            reasons.append("UNSUPPORTED_NO_RANDOMIZATION")
            reasons.append("SLICE_TOO_SMALL")
        request_decisions = []
        ctx = DecisionContext(ds=ds, policy=p, at=effective_at, occupancy=snap)
        for req in rank_candidates(round_requests, p, effective_at):
            for alt in req.alternatives:
                req_reasons = check_basic_eligibility(req, alt, ctx)
                request_decisions.append({
                    "request": req.request,
                    "family": req.family,
                    "slot": alt,
                    "seats": req.seats,
                    "wait_class": req.wait_class,
                    "wait_minutes": round(
                        wait_minutes(req, effective_at), 1
                    ),
                    "eligible": not req_reasons,
                    "reasons": req_reasons,
                    "reason_text": [REASON[r] for r in req_reasons],
                })
        approvals.append(
            PolicyApproval(
                policy=p.policy,
                approvable=not reasons,
                pilot=bool(reasons),
                reasons=reasons,
                ordering=p.ordering,
                expiry_minutes=p.expiry_minutes,
                max_regular_wait_minutes=p.max_regular_wait_minutes,
                regular_floor=p.regular_floor,
                effective_at=p.effective_at,
                applicable_slots=[s.slot for s in next_slots],
                request_decisions=request_decisions,
            )
        )

    return Registry(
        round_day=round_day,
        grandfathered=grandfathered,
        slot_decisions=slot_decisions,
        approvals=approvals,
        consumed_origins=sorted(ledger.consumed_origins),
        evidence_note=(
            "本轮无 randomization 记录,反事实响应无支撑;"
            "A/B 只能登记为试行,可上线的只有确定性条款与既有报价保护。"
            if not has_round_randomization
            else "本轮存在随机送达记录,可按支撑集合评估。"
        ),
    )
