"""指标汇总:实际入园、原预约履约、候补等待机会与填写率对比。"""
from __future__ import annotations

from dataclasses import dataclass

from .counterfactual import (
    DecisionEvaluation,
    OBSERVED_ACCEPT,
    OBSERVED_DECLINE,
    UNOBSERVED_NOT_DELIVERED,
)
from .models import Dataset
from .replay import ReplayResult


@dataclass
class HistoricalMetrics:
    days: int
    offers_sent: int
    accepted: int
    declined: int
    expired_seen: int
    not_delivered: int
    pending_open: int
    observed_accept_rate: float | None   # 仅在已送达且有作答者上计算
    fill_rate_confirmed: float           # 确认改签席位 / 决策点可用席位
    seats_held_wasted: int               # 未确认报价在有效期内占用的席位
    gate_total: int
    gate_reconciled: int
    gate_unreconciled: int
    origin_releases_valid: int
    origin_releases_invalid: int
    original_orders: int
    original_order_entries: int          # 可对账的原约入园回单数


def historical_metrics(ds: Dataset, replay: ReplayResult) -> HistoricalMetrics:
    offers = list(replay.offers.values())
    terminal_counts = {
        k: len(replay.offers_by_terminal(k))
        for k in ("accepted", "declined", "expired", "not_delivered", "pending")
    }
    answered = terminal_counts["accepted"] + terminal_counts["declined"]
    accept_rate = (
        terminal_counts["accepted"] / answered if answered else None
    )

    # 填写率:确认改签占用的席位 / 各决策点当时可用席位之和
    confirmed_seats = sum(o.seats for o in offers if o.terminal == "accepted")
    decision_free = sum(
        avail
        for day in replay.days.values()
        for avail in day.send_availability.values()
    )
    fill = confirmed_seats / decision_free if decision_free else 0.0

    # 未确认报价在有效期内的占席浪费(人·分钟同时计席位数)
    wasted = sum(
        o.seats
        for o in offers
        if o.terminal in ("expired", "not_delivered", "declined", "pending")
    )

    valid_releases = sum(1 for c in replay.release_chain if c["valid"])
    invalid_releases = len(replay.release_chain) - valid_releases

    original_entries = 0
    for g in ds.gate:
        order = ds.family_order(g.family)
        if order and order.slot == g.slot and order.seats == g.seats:
            original_entries += 1

    return HistoricalMetrics(
        days=len(replay.days),
        offers_sent=len(offers),
        accepted=terminal_counts["accepted"],
        declined=terminal_counts["declined"],
        expired_seen=terminal_counts["expired"],
        not_delivered=terminal_counts["not_delivered"],
        pending_open=terminal_counts["pending"],
        observed_accept_rate=accept_rate,
        fill_rate_confirmed=fill,
        seats_held_wasted=wasted,
        gate_total=len(ds.gate),
        gate_reconciled=len(replay.gate_reconciled),
        gate_unreconciled=len(replay.gate_unreconciled),
        origin_releases_valid=valid_releases,
        origin_releases_invalid=invalid_releases,
        original_orders=len(ds.orders),
        original_order_entries=original_entries,
    )


@dataclass
class PolicyCounterfactualMetrics:
    policy: str
    decisions: int
    supported_candidates: int
    unsupported_candidates: int
    chain_candidates: int
    observed_accepts: int
    observed_declines: int
    not_delivered_kept_separate: int
    # 候补等待机会:预期送达的家庭名额(随机政策为概率期望)
    delivery_opportunities: float
    # 反事实确认数的界限(人)与点估计
    accept_seats_low: float
    accept_seats_high: float
    accept_seats_point: float
    # 有作答观察的比例(证据强度)
    observed_share: float
    bound_width: float
    pilot: bool
    pilot_reasons: list[str]


def policy_metrics(
    evals: list[DecisionEvaluation],
) -> PolicyCounterfactualMetrics:
    policy_name = evals[0].policy if evals else ""
    supported = sum(
        1 for e in evals for o in e.outcomes if o.support == "supported"
    )
    unsupported = sum(
        1 for e in evals for o in e.outcomes if o.support == "unsupported"
    )
    chain = sum(
        1 for e in evals for o in e.outcomes if o.support == "chain"
    )
    accepts = sum(e.delivered_observed_accepts for e in evals)
    declines = sum(e.delivered_observed_declines for e in evals)
    not_delivered = sum(e.not_delivered_count for e in evals)

    low = sum(e.expected_accepts_low for e in evals)
    high = sum(e.expected_accepts_high for e in evals)
    point = sum(e.expected_accepts_point for e in evals)

    # 席位口径:每个决策点为单场单名额。
    # legacy 按各候选随机概率加权;A/B 只对确定性选中(chosen)的候选计 1 个名额。
    seat_low = seat_high = seat_point = 0.0
    for e in evals:
        for o in e.outcomes:
            if o.support != "supported" or o.policy_reasons:
                continue
            if e.policy == "legacy":
                w = o.probability_weight
            else:
                w = 1.0 if o.chosen else 0.0
            if w == 0:
                continue
            seat_low += w * o.accept_low * o.seats
            seat_high += w * o.accept_high * o.seats
            p = o.observed_accept_prob
            seat_point += w * (p if p is not None else 0.5) * o.seats

    observed_total = accepts + declines
    observed_share = observed_total / supported if supported else 0.0
    delivery_opportunities = sum(
        (e.delivery_opportunities_low + e.delivery_opportunities_high) / 2
        for e in evals
    )
    pilot = (high - low) > 0 or unsupported > 0 or chain > 0
    reasons: list[str] = []
    if high - low > 0:
        reasons.append("SLICE_TOO_SMALL")
        reasons.append("UNOBSERVED_PREFERENCE")
    if unsupported:
        reasons.append("UNSUPPORTED_NO_RANDOMIZATION")
    if chain:
        reasons.append("CHAIN_UNSUPPORTED")

    return PolicyCounterfactualMetrics(
        policy=policy_name,
        decisions=len(evals),
        supported_candidates=supported,
        unsupported_candidates=unsupported,
        chain_candidates=chain,
        observed_accepts=accepts,
        observed_declines=declines,
        not_delivered_kept_separate=not_delivered,
        delivery_opportunities=delivery_opportunities,
        accept_seats_low=seat_low,
        accept_seats_high=seat_high,
        accept_seats_point=seat_point,
        observed_share=observed_share,
        bound_width=seat_high - seat_low,
        pilot=pilot,
        pilot_reasons=sorted(set(reasons)),
    )
