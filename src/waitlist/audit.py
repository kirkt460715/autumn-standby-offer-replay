"""审计查询:指定日期的腾座-再分配链路,以及规则适用性解释。"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Dataset
from .policy import REASON
from .replay import ReplayResult, ReplayedOffer


@dataclass
class SeatTrail:
    day: str
    release_event: str
    freed_slot: str
    seats: int
    freed_by_family: str
    basis_offer: str
    released_at: str
    valid: bool
    reason: str
    acquired_by_offer: str | None
    acquired_by_family: str | None
    acquired_request: str | None
    gate_entry: str | None


def audit_day(
    ds: Dataset, replay: ReplayResult, day: str
) -> list[SeatTrail]:
    """返回某日所有"改签腾座 -> 后续获得者 -> 验票"的完整链路。"""
    trails: list[SeatTrail] = []
    day_releases = [
        c for c in replay.release_chain
        if c["at"][:10] == day
    ]
    for link in day_releases:
        downstream_offer = (
            link["downstream_offers"][0]
            if link["downstream_offers"] else None
        )
        acquired_family = (
            link["downstream_families"][0]
            if link["downstream_families"] else None
        )
        acquired_request = None
        gate_entry = None
        if downstream_offer:
            ro: ReplayedOffer = replay.offers[downstream_offer]
            acquired_request = ro.request
            gate_entry = next(
                (
                    g.entry for g in ds.gate
                    if g.family == ro.family
                    and g.slot == ro.slot and g.seats == ro.seats
                ),
                None,
            )
        trails.append(
            SeatTrail(
                day=day,
                release_event=link["release_event"],
                freed_slot=link["slot"],
                seats=link["seats"],
                freed_by_family=link["family"],
                basis_offer=link["basis_offer"],
                released_at=link["at"],
                valid=link["valid"],
                reason=link["reason"],
                acquired_by_offer=downstream_offer,
                acquired_by_family=acquired_family,
                acquired_request=acquired_request,
                gate_entry=gate_entry,
            )
        )
    return trails


def explain_rule_for_slot(
    ds: Dataset,
    policy_name: str,
    slot: str,
    at,
    evaluation_by_policy: dict[str, object],
) -> dict:
    """解释某政策为何(不)能用于指定场次:汇总该决策点上每个候补的拒批原因。"""
    policy = ds.policies[policy_name]
    evals = evaluation_by_policy.get(policy_name)
    target = None
    if evals is not None:
        for e in evals:
            if e.slot == slot and e.at == at:
                target = e
                break
    explanation = {
        "policy": policy_name,
        "slot": slot,
        "at": at.isoformat() if hasattr(at, "isoformat") else str(at),
        "effective": policy_effective_safe(policy, at),
        "ordering": policy.ordering,
        "expiry_minutes": policy.expiry_minutes,
        "max_regular_wait_minutes": policy.max_regular_wait_minutes,
        "regular_floor": policy.regular_floor,
        "candidate_reasons": [],
        "headline": "",
    }
    if target is None:
        explanation["headline"] = "该场次在该时点没有有支撑的放号决策点"
        return explanation
    explanation["free_seats_at_decision"] = target.free_seats_at_decision
    blockers: dict[str, int] = {}
    for o in target.outcomes:
        codes = list(o.policy_reasons)
        if o.support == "unsupported":
            codes.append("UNSUPPORTED_NO_RANDOMIZATION")
        elif o.support == "chain":
            codes.append("CHAIN_UNSUPPORTED")
        for c in codes:
            blockers[c] = blockers.get(c, 0) + 1
        explanation["candidate_reasons"].append({
            "request": o.request,
            "family": o.family,
            "wait_class": o.wait_class,
            "support": o.support,
            "reasons": codes,
            "reason_text": [REASON[c] for c in codes],
            "response_status": o.response_status,
        })
    if not blockers:
        explanation["headline"] = (
            "政策可用于该场次;但反事实接受意愿仍受观察范围限制,见界限"
        )
    else:
        top = sorted(blockers.items(), key=lambda kv: (-kv[1], kv[0]))
        explanation["headline"] = "阻塞原因: " + "; ".join(
            f"{REASON[c]}({n} 个候补)" for c, n in top
        )
    return explanation


def policy_effective_safe(policy, at) -> bool:
    return at >= policy.effective_at
