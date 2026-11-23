"""候选规则的反事实评估。

识别策略(严格遵守"只用决策时点已知信息"):
1. 每个历史放号决策点(每天 09:00 的普通场放号)以 randomization 表中有概率记录的
   候选集合作为唯一有支撑的送达集合。集合之外的候补即使政策排序靠前,也标记为
   UNSUPPORTED_NO_RANDOMIZATION,不假设其送达或接受。
2. 接受意愿只在"已送达且有终局作答"的报价上被观察到:
   - accepted 记为接受;declined 记为拒绝;
   - expired(seen=yes)、not_delivered(seen=no)、从未收到报价 均为未观察,
     未送达与收到后拒绝严格分开。
3. 未观察意愿的处理采用 Manski 式界限:下界按"未观察=拒绝",上界按"未观察=接受",
   不假设所有新增候补都接受。
4. 依赖其他换票腾出原票的名额属于交换链:只有上游被同情景接受时才可发出,
   且链上名额单独标注 CHAIN_UNSUPPORTED,单组效果不外推。
5. legacy 的随机送达按 probability 计算期望;A/B 为确定性排序,给点估计与界限。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import Dataset, Policy, WaitRequest
from .occupancy import Ledger
from .policy import (
    DecisionContext,
    check_basic_eligibility,
    policy_effective,
    rank_candidates,
    wait_minutes,
)

# 响应状态
OBSERVED_ACCEPT = "observed_accept"
OBSERVED_DECLINE = "observed_decline"
UNOBSERVED_EXPIRED = "unobserved_expired"          # 送达但未作答
UNOBSERVED_NOT_DELIVERED = "unobserved_not_delivered"  # 未送达 ≠ 拒绝
UNOBSERVED_NEVER_SENT = "unobserved_never_sent"    # 旧规则未收到报价
UNSUPPORTED = "unsupported_no_randomization"
CHAIN = "chain_unsupported"
INELIGIBLE = "ineligible"


@dataclass
class CandidateOutcome:
    request: str
    family: str
    slot: str
    seats: int
    wait_class: str
    wait_minutes_at_decision: float
    policy_reasons: list[str]            # 资格拒批原因码
    support: str                         # supported / unsupported / chain
    response_status: str | None          # 仅对"会被送达"的候选有意义
    accept_low: int                      # 0/1 界限(接受=1)
    accept_high: int
    observed_accept_prob: float | None   # 有观察时的点估计(0/1)
    probability_weight: float            # legacy 随机送达概率,其余为 1
    chosen: bool = False                 # 确定性政策下实际被送达的候选


@dataclass
class DecisionEvaluation:
    day: str
    slot: str
    at: datetime
    policy: str
    effective: bool
    free_seats_at_decision: int
    outcomes: list[CandidateOutcome] = field(default_factory=list)
    # 聚合(当天该决策点)
    expected_accepts_low: float = 0.0
    expected_accepts_high: float = 0.0
    expected_accepts_point: float = 0.0
    delivery_opportunities_low: float = 0.0   # 预期送达名额(家庭数)
    delivery_opportunities_high: float = 0.0
    delivered_observed_accepts: int = 0
    delivered_observed_declines: int = 0
    not_delivered_count: int = 0
    unsupported_count: int = 0
    chain_count: int = 0
    notes: list[str] = field(default_factory=list)


def _draw_support(ds: Dataset) -> dict[tuple[str, str], DrawCandidate]:
    """(request, slot) -> 抽签候选记录,界定反事实支撑集合。"""
    return {(c.request, c.slot): c for c in ds.draws if c.eligible}


def _observed_response(
    ds: Dataset, request: str, slot: str
) -> tuple[str | None, int, int, float | None]:
    """返回 (响应状态, 接受下界, 接受上界, 观察点估计)。

    只认历史上真实送达(有 sent 且终局 seen=yes/有作答)的报价。
    """
    for ev in ds.events:
        if ev.request != request or ev.slot != slot:
            continue
        if ev.kind == "accepted":
            return OBSERVED_ACCEPT, 1, 1, 1.0
        if ev.kind == "declined":
            return OBSERVED_DECLINE, 0, 0, 0.0
        if ev.kind == "expired":
            # 已送达但未在有效期内作答:意愿未观察
            return UNOBSERVED_EXPIRED, 0, 1, None
        if ev.kind == "not_delivered":
            # 未送达不代表拒绝
            return UNOBSERVED_NOT_DELIVERED, 0, 1, None
    return UNOBSERVED_NEVER_SENT, 0, 1, None


def evaluate_decision(
    ds: Dataset,
    policy: Policy,
    day: str,
    slot: str,
    at: datetime,
    ledger: Ledger,
    chain_slots: set[str] | None = None,
) -> DecisionEvaluation:
    """评估单个决策点上某政策的分派与响应界限。"""
    chain_slots = chain_slots or set()
    snap = ledger.snapshot_at(at)
    ctx = DecisionContext(ds=ds, policy=policy, at=at, occupancy=snap)
    support = _draw_support(ds)
    ev = DecisionEvaluation(
        day=day,
        slot=slot,
        at=at,
        policy=policy.policy,
        effective=policy_effective(policy, at),
        free_seats_at_decision=snap[slot].free,
    )
    if not ev.effective:
        ev.notes.append("POLICY_NOT_EFFECTIVE")

    # 当日所有把该场列为备选的候补
    pool = [
        r for r in ds.requests.values()
        if r.joined_at.date().isoformat() == day and slot in r.alternatives
    ]
    ranked = rank_candidates(pool, policy, at)
    draw_candidates = {
        c.request: c
        for c in ds.draws
        if c.slot == slot and c.at == at and c.eligible
    }

    for req in ranked:
        reasons = check_basic_eligibility(req, slot, ctx)
        in_draw = req.request in draw_candidates
        is_chain = slot in chain_slots and not in_draw
        if not in_draw:
            support_kind = "chain" if is_chain else "unsupported"
        else:
            support_kind = "supported"

        status, low, high, point = _observed_response(ds, req.request, slot)
        weight = (
            draw_candidates[req.request].probability
            if policy.ordering == "logged_priority" and in_draw
            else (1.0 if in_draw else 0.0)
        )

        outcome = CandidateOutcome(
            request=req.request,
            family=req.family,
            slot=slot,
            seats=req.seats,
            wait_class=req.wait_class,
            wait_minutes_at_decision=wait_minutes(req, at),
            policy_reasons=reasons,
            support=support_kind,
            response_status=status if in_draw else None,
            accept_low=low if in_draw else 0,
            accept_high=high if in_draw else 0,
            observed_accept_prob=point if in_draw else None,
            probability_weight=weight,
        )
        ev.outcomes.append(outcome)

        if not in_draw:
            if is_chain:
                ev.chain_count += 1
            else:
                ev.unsupported_count += 1
            continue

        if policy.ordering == "logged_priority":
            # 随机机制:只有资格通过的候选才可能被抽中,按概率加权期望与界限
            if not reasons:
                ev.expected_accepts_low += weight * low
                ev.expected_accepts_high += weight * high
                ev.expected_accepts_point += (
                    weight * point if point is not None else weight * 0.5
                )
                # 概率和为 1:该决策点预期恰好送达 1 个家庭
                ev.delivery_opportunities_low += weight
                ev.delivery_opportunities_high += weight
                if status == OBSERVED_ACCEPT:
                    ev.delivered_observed_accepts += 1
                elif status == OBSERVED_DECLINE:
                    ev.delivered_observed_declines += 1
                elif status == UNOBSERVED_NOT_DELIVERED:
                    ev.not_delivered_count += 1

    # 确定性政策(A/B):取排序后第一个"有支撑且资格通过"的候选作为实际送达对象。
    # 其余有支撑候选在该政策下不会收到报价,其历史响应不计入本政策的送达口径。
    if policy.ordering in ("protected_first", "longest_wait"):
        chosen = next(
            (o for o in ev.outcomes
             if o.support == "supported" and not o.policy_reasons),
            None,
        )
        if chosen is not None:
            chosen.chosen = True
            ev.delivery_opportunities_low = 1.0
            ev.delivery_opportunities_high = 1.0
            ev.expected_accepts_low = float(chosen.accept_low)
            ev.expected_accepts_high = float(chosen.accept_high)
            ev.expected_accepts_point = (
                chosen.observed_accept_prob
                if chosen.observed_accept_prob is not None
                else (chosen.accept_low + chosen.accept_high) / 2
            )
            if chosen.response_status == OBSERVED_ACCEPT:
                ev.delivered_observed_accepts += 1
            elif chosen.response_status == OBSERVED_DECLINE:
                ev.delivered_observed_declines += 1
            elif chosen.response_status == UNOBSERVED_NOT_DELIVERED:
                ev.not_delivered_count += 1

    return ev


def historical_decision_points(ds: Dataset) -> list[tuple[str, str, datetime]]:
    """从 randomization 表提取历史决策点(每天每场一次放号抽取)。"""
    points: dict[tuple[str, str, datetime], None] = {}
    for cand in ds.draws:
        points[(cand.at.date().isoformat(), cand.slot, cand.at)] = None
    return sorted(points.keys(), key=lambda p: p[2])


def evaluate_policy(
    ds: Dataset, policy: Policy
) -> list[DecisionEvaluation]:
    ledger = Ledger(ds)
    results: list[DecisionEvaluation] = []
    # 交换链场:存在 requires_offer 的 sent 所指向的场,其链上名额无独立随机支撑
    chain_slots = {
        ev.slot for ev in ds.events
        if ev.kind == "sent" and ev.requires_offer
    }
    for day, slot, at in historical_decision_points(ds):
        results.append(
            evaluate_decision(ds, policy, day, slot, at, ledger, chain_slots)
        )
    return results
