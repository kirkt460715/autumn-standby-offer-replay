"""历史回放:复刻已执行的 legacy 规则,并与真实事件记录对账。

回放严格按真实时刻推进:
- 报价 sent->expires 占席;终局(accepted/declined/expired/not_delivered/pending)
  的区分完全保留,未送达(not_delivered, seen=no)不与拒绝合并。
- 接受新票后才释放原场;释放条件不成立时只记录不生效。
- 依赖其他报价(requires_offer)的名额,其席位来源必须是上游已腾出的原票。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import Dataset, Event
from .occupancy import Ledger, offer_lifecycle


@dataclass
class ReplayedOffer:
    offer: str
    request: str
    family: str
    slot: str
    seats: int
    sent_at: datetime
    expires: datetime | None
    terminal: str
    terminal_at: datetime | None
    seen: str
    requires_offer: str
    basis_release_event: str | None = None  # 席位来自哪次原票释放
    released_origin: str | None = None


@dataclass
class DayReplay:
    day: str
    offers: list[ReplayedOffer] = field(default_factory=list)
    # 每个 sent 在发出时点的可用席位(不含未来信息)
    send_availability: dict[str, int] = field(default_factory=dict)
    chain_violations: list[str] = field(default_factory=list)


@dataclass
class ReplayResult:
    days: dict[str, DayReplay] = field(default_factory=dict)
    offers: dict[str, ReplayedOffer] = field(default_factory=dict)
    gate_reconciled: list[str] = field(default_factory=list)   # entry 编号
    gate_unreconciled: list[str] = field(default_factory=list)
    release_chain: list[dict] = field(default_factory=list)

    def offers_by_terminal(self, terminal: str) -> list[ReplayedOffer]:
        return [o for o in self.offers.values() if o.terminal == terminal]


def replay_history(ds: Dataset) -> ReplayResult:
    result = ReplayResult()
    life = offer_lifecycle(ds)
    ledger = Ledger(ds)

    # 事件索引:报价 sent 时点可用数;释放事件 -> 释放了哪个订单
    release_events = {
        ev.event: ev for ev in ds.events if ev.kind == "origin_released"
    }

    for ev in ds.events:
        if ev.kind == "sent":
            # 发报价"当时"的可用席位:只统计严格早于该时刻的事件
            snap = ledger.snapshot_at(ev.at)
            free = snap[ev.slot].free
            day = ev.at.date().isoformat()
            day_replay = result.days.setdefault(day, DayReplay(day=day))
            day_replay.send_availability[ev.offer] = free

            # 依赖链核验:requires_offer 对应的原票释放必须已发生且在同一目标场
            if ev.requires_offer:
                basis_offer = ev.requires_offer
                basis_info = life.get(basis_offer)
                basis_release = None
                if basis_info:
                    rel = next(
                        (
                            e for e in ds.events
                            if e.kind == "origin_released"
                            and e.request == basis_info["request"]
                            and e.at <= ev.at
                        ),
                        None,
                    )
                    basis_release = rel.event if rel else None
                if basis_release is None:
                    day_replay.chain_violations.append(
                        f"{ev.offer}: 依赖 {basis_offer} 但发出时原票尚未释放"
                    )
        ledger.apply(ev)

    # 组装报价生命周期
    for offer_id, info in sorted(
        life.items(),
        key=lambda kv: kv[1]["sent_at"] or datetime.max,
    ):
        req = ds.requests.get(info["request"])
        ro = ReplayedOffer(
            offer=offer_id,
            request=info["request"],
            family=req.family if req else "",
            slot=info["slot"],
            seats=info["seats"],
            sent_at=info["sent_at"],
            expires=info["expires"],
            terminal=info["terminal"] or "open",
            terminal_at=info["terminal_at"],
            seen=info["seen"] or "unknown",
            requires_offer=info["requires_offer"],
            released_origin=info["released_origin"],
        )
        if info["requires_offer"]:
            basis = next(
                (
                    e.event for e in ds.events
                    if e.kind == "origin_released"
                    and e.requires_offer == info["requires_offer"]
                ),
                None,
            )
            ro.basis_release_event = basis
        result.offers[offer_id] = ro
        if ro.sent_at is not None:
            day = ro.sent_at.date().isoformat()
            result.days.setdefault(day, DayReplay(day=day)).offers.append(ro)

    # 交换链台账:释放事件 -> 腾出的座位 -> 被哪个依赖报价获得 -> 哪个家庭入园
    accepted_by_offer = {
        o.offer: o for o in result.offers.values() if o.terminal == "accepted"
    }
    for ev in ds.events:
        if ev.kind != "origin_released":
            continue
        req = ds.requests.get(ev.request)
        order = ds.family_order(req.family) if req else None
        valid = order is not None and order.slot == ev.slot
        downstream = [
            o for o in result.offers.values()
            if o.requires_offer == ev.requires_offer
        ]
        result.release_chain.append({
            "release_event": ev.event,
            "at": ev.at.isoformat(),
            "slot": ev.slot,
            "seats": ev.seats,
            "family": req.family if req else "",
            "valid": valid,
            "reason": "" if valid else "原订单不在该场,释放不生效",
            "basis_offer": ev.requires_offer,
            "downstream_offers": [o.offer for o in downstream],
            "downstream_families": [o.family for o in downstream],
        })

    # 验票对账
    accepted_keys = {
        (o.family, o.slot, o.seats): o.offer
        for o in result.offers.values() if o.terminal == "accepted"
    }
    for g in ds.gate:
        key = (g.family, g.slot, g.seats)
        if key in accepted_keys:
            result.gate_reconciled.append(g.entry)
            continue
        order = ds.family_order(g.family)
        if order and order.slot == g.slot and order.seats == g.seats:
            result.gate_reconciled.append(g.entry)  # 原约入园
            continue
        result.gate_unreconciled.append(g.entry)

    return result
