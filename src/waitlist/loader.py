"""CSV 加载与资料核验。

核验不"修正"数据:结构性问题记 error,业务异常(如验票记录无法与报价/订单对账、
原票释放条件不成立)记 warn,由后续模块在报告中披露。
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from .models import (
    Dataset,
    DrawCandidate,
    Event,
    Finding,
    GateRecord,
    Order,
    Policy,
    Slot,
    WaitRequest,
    parse_ts,
)

TABLE_FIELDS = {
    "slots": ("slot", "day", "starts", "capacity", "priority_class"),
    "orders": (
        "order_id", "family", "slot", "seats", "may_split", "confirmed_at",
    ),
    "waitlist": (
        "request", "family", "old_slot", "alternatives", "seats",
        "joined_at", "may_split", "wait_class",
    ),
    "events": (
        "event", "offer", "request", "slot", "kind", "at", "expires",
        "seen", "seats", "requires_offer",
    ),
    "randomization": (
        "draw", "request", "slot", "at", "probability", "eligible",
        "selected",
    ),
    "gate": ("entry", "family", "slot", "seats", "at", "status"),
    "policies": (
        "policy", "state", "effective_at", "ordering", "expiry_minutes",
        "max_regular_wait_minutes", "regular_floor",
    ),
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path} 缺少表头")
        expected = list(TABLE_FIELDS[path.stem])
        if reader.fieldnames != expected:
            raise ValueError(
                f"{path.name} 表头不符: 期望 {expected}, 实际 {reader.fieldnames}"
            )
        rows = list(reader)
    if not rows:
        raise ValueError(f"{path.name} 为空")
    for row in rows:
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f"{path.name} 存在缺列行")
    return rows


def load_dataset(data_dir: str | Path) -> Dataset:
    data_dir = Path(data_dir)
    findings: list[Finding] = []

    slots: dict[str, Slot] = {}
    for row in _read_csv(data_dir / "slots.csv"):
        key = row["slot"]
        if key in slots:
            findings.append(Finding("error", "dup_key", f"slots 重复键 {key}", (key,)))
        slots[key] = Slot(
            slot=key,
            day=row["day"],
            starts=row["starts"],
            capacity=int(row["capacity"]),
            priority_class=row["priority_class"],
        )

    orders: dict[str, Order] = {}
    family_orders: dict[str, list[str]] = defaultdict(list)
    for row in _read_csv(data_dir / "orders.csv"):
        key = row["order_id"]
        if key in orders:
            findings.append(Finding("error", "dup_key", f"orders 重复键 {key}", (key,)))
        if row["slot"] not in slots:
            findings.append(Finding(
                "error", "bad_ref",
                f"订单 {key} 指向不存在的场次 {row['slot']}", (key,),
            ))
        order = Order(
            order_id=key,
            family=row["family"],
            slot=row["slot"],
            seats=int(row["seats"]),
            may_split=row["may_split"] == "yes",
            confirmed_at=parse_ts(row["confirmed_at"]),
        )
        orders[key] = order
        family_orders[order.family].append(key)

    requests: dict[str, WaitRequest] = {}
    for row in _read_csv(data_dir / "waitlist.csv"):
        key = row["request"]
        if key in requests:
            findings.append(
                Finding("error", "dup_key", f"waitlist 重复键 {key}", (key,))
            )
        alts = tuple(a for a in row["alternatives"].split("|") if a)
        for alt in alts:
            if alt not in slots:
                findings.append(Finding(
                    "error", "bad_ref",
                    f"候补 {key} 的备选场次 {alt} 不存在", (key,),
                ))
        old = row["old_slot"]
        if old and old not in slots:
            findings.append(Finding(
                "error", "bad_ref",
                f"候补 {key} 的原场 {old} 不存在", (key,),
            ))
        req = WaitRequest(
            request=key,
            family=row["family"],
            old_slot=old,
            alternatives=alts,
            seats=int(row["seats"]),
            joined_at=parse_ts(row["joined_at"]),
            may_split=row["may_split"] == "yes",
            wait_class=row["wait_class"],
        )
        requests[key] = req

    events: list[Event] = []
    seen_event_ids: set[str] = set()
    for row in _read_csv(data_dir / "events.csv"):
        if row["event"] in seen_event_ids:
            findings.append(Finding(
                "error", "dup_key", f"events 重复键 {row['event']}", (row["event"],)
            ))
        seen_event_ids.add(row["event"])
        if row["request"] not in requests:
            findings.append(Finding(
                "error", "bad_ref",
                f"事件 {row['event']} 指向不存在的候补 {row['request']}",
                (row["event"],),
            ))
        if row["slot"] not in slots:
            findings.append(Finding(
                "error", "bad_ref",
                f"事件 {row['event']} 指向不存在的场次 {row['slot']}",
                (row["event"],),
            ))
        events.append(
            Event(
                event=row["event"],
                offer=row["offer"],
                request=row["request"],
                slot=row["slot"],
                kind=row["kind"],
                at=parse_ts(row["at"]),
                expires=parse_ts(row["expires"]),
                seen=row["seen"],
                seats=int(row["seats"]),
                requires_offer=row["requires_offer"],
            )
        )
    events.sort(key=lambda e: (e.at, e.event))

    draws: list[DrawCandidate] = []
    for row in _read_csv(data_dir / "randomization.csv"):
        if row["request"] not in requests:
            findings.append(Finding(
                "error", "bad_ref",
                f"抽签 {row['draw']} 指向不存在的候补 {row['request']}",
                (row["draw"],),
            ))
        draws.append(
            DrawCandidate(
                draw=row["draw"],
                request=row["request"],
                slot=row["slot"],
                at=parse_ts(row["at"]),
                probability=float(row["probability"]),
                eligible=row["eligible"] == "yes",
                selected=row["selected"] == "yes",
            )
        )

    gate: list[GateRecord] = []
    for row in _read_csv(data_dir / "gate.csv"):
        if row["slot"] not in slots:
            findings.append(Finding(
                "error", "bad_ref",
                f"验票 {row['entry']} 指向不存在的场次 {row['slot']}",
                (row["entry"],),
            ))
        gate.append(
            GateRecord(
                entry=row["entry"],
                family=row["family"],
                slot=row["slot"],
                seats=int(row["seats"]),
                at=parse_ts(row["at"]),
                status=row["status"],
            )
        )
    gate.sort(key=lambda g: g.at)

    policies: dict[str, Policy] = {}
    for row in _read_csv(data_dir / "policies.csv"):
        key = row["policy"]
        if key in policies:
            findings.append(Finding(
                "error", "dup_key", f"policies 重复键 {key}", (key,)
            ))
        policies[key] = Policy(
            policy=key,
            state=row["state"],
            effective_at=parse_ts(row["effective_at"]),
            ordering=row["ordering"],
            expiry_minutes=int(row["expiry_minutes"]),
            max_regular_wait_minutes=int(row["max_regular_wait_minutes"]),
            regular_floor=float(row["regular_floor"]),
        )

    dataset = Dataset(
        slots=slots,
        orders=orders,
        requests=requests,
        events=events,
        draws=draws,
        gate=gate,
        policies=policies,
        findings=findings,
    )
    _cross_validate(dataset)
    return dataset


def _cross_validate(ds: Dataset) -> None:
    findings = ds.findings

    # 1. 每个报价的事件链:sent 在前,终局唯一且在 sent 之后;占席事件须有 expires。
    by_offer: dict[str, list[Event]] = defaultdict(list)
    for ev in ds.events:
        by_offer[ev.offer].append(ev)
    offer_terminal: dict[str, Event] = {}
    for offer, chain in by_offer.items():
        chain.sort(key=lambda e: (e.at, e.event))
        kinds = [e.kind for e in chain]
        if kinds[0] != "sent":
            findings.append(Finding(
                "error", "chain_order",
                f"报价 {offer} 的首个事件不是 sent", (offer,),
            ))
        terminals = [
            e for e in chain
            if e.kind in {"accepted", "declined", "expired", "not_delivered",
                          "pending", "origin_released"}
        ]
        life_terminals = [
            e for e in terminals
            if e.kind in {"accepted", "declined", "expired",
                          "not_delivered", "pending"}
        ]
        if len(life_terminals) > 1:
            findings.append(Finding(
                "error", "chain_terminal",
                f"报价 {offer} 存在多个终局事件", (offer,),
            ))
        if life_terminals:
            offer_terminal[offer] = life_terminals[0]
        for ev in chain:
            if ev.kind != "origin_released" and ev.expires is None:
                findings.append(Finding(
                    "error", "missing_expiry",
                    f"报价事件 {ev.event} 缺少 expires", (ev.event,),
                ))
        if len({e.request for e in chain}) != 1 or len({e.slot for e in chain
                  if e.kind != "origin_released"}) != 1:
            findings.append(Finding(
                "error", "chain_target",
                f"报价 {offer} 的候补/场次不一致", (offer,),
            ))

    # 2. origin_released 的条件:对应报价已 accepted;原场须为该家庭订单所在场;
    #    requires_offer 指向被释放所依据的报价。
    released_origins: set[str] = set()
    for ev in ds.events:
        if ev.kind != "origin_released":
            continue
        req = ds.requests.get(ev.request)
        if req is None:
            continue
        basis = ev.requires_offer
        basis_chain = by_offer.get(basis, [])
        if not any(e.kind == "accepted" for e in basis_chain):
            findings.append(Finding(
                "error", "release_without_accept",
                f"原票释放 {ev.event} 依据的报价 {basis} 未确认", (ev.event,),
            ))
        order = ds.family_order(req.family)
        if order is None or order.slot != ev.slot:
            findings.append(Finding(
                "warn", "release_condition_unmet",
                f"释放 {ev.event}: 家庭 {req.family} 的订单不在 {ev.slot},"
                "按约定该释放不成立(空 origin/外部订单不产生释放)",
                (ev.event,),
            ))
            continue
        if ev.slot != req.old_slot:
            findings.append(Finding(
                "warn", "release_old_slot_mismatch",
                f"释放 {ev.event} 的场次与候补登记原场 {req.old_slot} 不符",
                (ev.event,),
            ))
        if order.order_id in released_origins:
            findings.append(Finding(
                "error", "double_release",
                f"原订单 {order.order_id} 被重复释放", (ev.event,),
            ))
        released_origins.add(order.order_id)

    # 3. requires_offer 依赖:被依赖报价必须先确认,依赖报价的 sent 必须晚于该确认,
    #    且依赖报价的目标场次在依赖发出时确实由上游腾出(由 occupancy 模块复核)。
    for ev in ds.events:
        if ev.kind != "sent" or not ev.requires_offer:
            continue
        basis = ev.requires_offer
        basis_events = by_offer.get(basis, [])
        accept = next((e for e in basis_events if e.kind == "accepted"), None)
        if accept is None:
            findings.append(Finding(
                "warn", "chain_basis_not_accepted",
                f"报价 {ev.offer} 依赖 {basis},但上游未确认;该名额在反事实中无支撑",
                (ev.offer, basis),
            ))
        elif accept.at >= ev.at:
            findings.append(Finding(
                "error", "chain_time",
                f"报价 {ev.offer} 早于上游确认 {accept.event}", (ev.offer,),
            ))

    # 4. 抽签:每个 draw 的概率和为 1;selected 必须 eligible 且唯一;
    #    候选必须把该场次列为备选;selected=yes 应有同场次同时刻的 sent。
    draw_groups: dict[str, list[DrawCandidate]] = defaultdict(list)
    for cand in ds.draws:
        draw_groups[cand.draw].append(cand)
    sent_index = {
        (e.request, e.slot, e.at): e for e in ds.events if e.kind == "sent"
    }
    for draw, cands in draw_groups.items():
        total = round(sum(c.probability for c in cands), 9)
        if abs(total - 1.0) > 1e-9:
            findings.append(Finding(
                "error", "prob_sum",
                f"抽签 {draw} 概率和为 {total} 而非 1", (draw,),
            ))
        selected = [c for c in cands if c.selected]
        if len(selected) != 1:
            findings.append(Finding(
                "error", "draw_selection",
                f"抽签 {draw} 选中项数量为 {len(selected)}", (draw,),
            ))
        for c in cands:
            req = ds.requests.get(c.request)
            if req is not None and c.slot not in req.alternatives:
                findings.append(Finding(
                    "error", "draw_slot_not_alternative",
                    f"抽签 {draw} 候选 {c.request} 未把 {c.slot} 列为备选",
                    (draw, c.request),
                ))
            if c.selected and not c.eligible:
                findings.append(Finding(
                    "error", "selected_ineligible",
                    f"抽签 {draw} 选中了不合格候选 {c.request}", (draw,),
                ))
        if selected:
            c = selected[0]
            if (c.request, c.slot, c.at) not in sent_index:
                findings.append(Finding(
                    "warn", "selected_not_sent",
                    f"抽签 {draw} 选中 {c.request},但无对应 sent 事件", (draw,),
                ))

    # 5. 验票对账:能与 accepted 报价对上的为"改签兑现";能与原始订单对上的为
    #    "原约入园";两者皆非的记为不可解释验票(数据异常,不静默纳入任何口径)。
    accepted_families: dict[tuple[str, str, int], str] = {}
    for offer, terminal in offer_terminal.items():
        if terminal.kind != "accepted":
            continue
        req = ds.requests[terminal.request]
        accepted_families[(req.family, terminal.slot, terminal.seats)] = offer
    for g in ds.gate:
        key = (g.family, g.slot, g.seats)
        if key in accepted_families:
            continue
        order = ds.family_order(g.family)
        if order is not None and order.slot == g.slot and order.seats == g.seats:
            continue
        findings.append(Finding(
            "warn", "gate_unreconciled",
            f"验票 {g.entry}: 家庭 {g.family} 在 {g.slot} 的 {g.seats} 人"
            "无法与确认报价或原始订单对账",
            (g.entry,),
        ))

    # 6. 报价席位与候补登记席位一致。
    for ev in ds.events:
        if ev.kind == "origin_released":
            continue
        req = ds.requests.get(ev.request)
        if req is not None and ev.seats != req.seats:
            findings.append(Finding(
                "warn", "seats_mismatch",
                f"事件 {ev.event} 席位 {ev.seats} 与候补登记 {req.seats} 不符",
                (ev.event,),
            ))
