"""时点正确的席位占用账本。

规则(见 data-contract.md):
- 一个时段令牌按一人计。
- 报价自 sent 起占席,直到 expires;明确撤回/拒绝/确认可提前结束待定占用。
- 确认新场之后原场才释放(accepted 先于 origin_released);
  原订单不属于该馆场次(空 origin 或订单场不符)时不产生释放。
- 任何时点的可用数只能由该时点之前已发生的事件计算,
  禁止用后来实际释放的座位回填,也禁止把确认后才释放的原票当作发报价时的依据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import Dataset, Event


@dataclass
class SlotOccupancy:
    capacity: int
    base_confirmed: int = 0          # 放号前既有订单席位
    released: int = 0               # 截至当前时点已生效的原票释放
    holds: dict[str, int] = field(default_factory=dict)  # offer -> 占席数

    @property
    def hold_seats(self) -> int:
        return sum(self.holds.values())

    @property
    def confirmed_seats(self) -> int:
        return self.base_confirmed - self.released

    @property
    def used(self) -> int:
        return self.confirmed_seats + self.hold_seats

    @property
    def free(self) -> int:
        return self.capacity - self.used


class Ledger:
    """按事件流推进的占用账本。所有查询均针对当前推进到的时点。"""

    def __init__(self, ds: Dataset):
        self.ds = ds
        self.occ: dict[str, SlotOccupancy] = {
            slot_id: SlotOccupancy(capacity=s.capacity)
            for slot_id, s in ds.slots.items()
        }
        # 已成功用于换票的原订单:同一原票不可重复释放
        self.consumed_origins: set[str] = set()
        self.release_log: list[Event] = []
        self._apply_base_orders()

    def _apply_base_orders(self) -> None:
        for order in self.orders_of_dataset():
            self.occ[order.slot].base_confirmed += order.seats

    def orders_of_dataset(self):
        return self.ds.orders.values()

    # ---- 事件推进 --------------------------------------------------------

    def apply(self, ev: Event) -> None:
        """应用单个事件;调用方需保证按 (at, event) 顺序喂入。"""
        kind = ev.kind
        if kind == "sent" or kind == "pending":
            # pending 是 sent 之后仍在占席的状态记录,不重复占席
            if kind == "sent":
                self.occ[ev.slot].holds[ev.offer] = (
                    self.occ[ev.slot].holds.get(ev.offer, 0) + ev.seats
                )
            return
        if kind in {"accepted", "declined", "expired", "not_delivered"}:
            self._close_hold(ev)
            if kind == "accepted":
                # 报价席位转为确认占用:先从 hold 移除,再计入 confirmed。
                # 由于 base_confirmed 不含候补家庭,这里以"新增确认席位"入账。
                self.occ[ev.slot].base_confirmed += ev.seats
            return
        if kind == "origin_released":
            self._apply_release(ev)
            return
        raise ValueError(f"未知事件类型 {kind}")

    def _close_hold(self, ev: Event) -> None:
        occ = self.occ[ev.slot]
        held = occ.holds.pop(ev.offer, 0)
        if held == 0:
            # 同一报价重复终局或终局先于 sent 的数据问题交给 loader 披露
            return

    def _apply_release(self, ev: Event) -> None:
        req = self.ds.requests.get(ev.request)
        if req is None:
            return
        order = self.ds.family_order(req.family)
        # 释放条件:原订单真实存在且位于被释放场次;同一原票只释放一次
        if order is None or order.slot != ev.slot:
            return
        if order.order_id in self.consumed_origins:
            return
        occ = self.occ[ev.slot]
        room = occ.base_confirmed - occ.released
        n = min(ev.seats, room)
        if n <= 0:
            return
        occ.released += n
        self.consumed_origins.add(order.order_id)
        self.release_log.append(ev)

    # ---- 时点查询 --------------------------------------------------------

    def free_at(self, slot: str, at: datetime) -> int:
        """重放到 at(含该时刻事件)后的可用席位。"""
        snap = self.snapshot_at(at)
        return snap[slot].free

    def snapshot_at(self, at: datetime) -> dict[str, SlotOccupancy]:
        """重放到指定时点,返回复制的占用快照(不改变本账本状态)。"""
        occ: dict[str, SlotOccupancy] = {
            slot_id: SlotOccupancy(
                capacity=o.capacity,
                base_confirmed=o.base_confirmed,
                released=o.released,
                holds=dict(o.holds),
            )
            for slot_id, o in self.occ.items()
        }
        consumed: set[str] = set()
        for ev in self.ds.events:
            if ev.at > at:
                break
            kind = ev.kind
            if kind in ("sent",):
                occ[ev.slot].holds[ev.offer] = (
                    occ[ev.slot].holds.get(ev.offer, 0) + ev.seats
                )
            elif kind in ("accepted", "declined", "expired", "not_delivered"):
                occ[ev.slot].holds.pop(ev.offer, None)
                if kind == "accepted":
                    occ[ev.slot].base_confirmed += ev.seats
            elif kind == "origin_released":
                req = self.ds.requests.get(ev.request)
                order = (
                    self.ds.family_order(req.family) if req is not None else None
                )
                if order is None or order.slot != ev.slot:
                    continue
                if order.order_id in consumed:
                    continue
                room = occ[ev.slot].base_confirmed - occ[ev.slot].released
                n = min(ev.seats, room)
                if n > 0:
                    occ[ev.slot].released += n
                    consumed.add(order.order_id)
            # pending 不改变占用
        return occ

    def availability_series(self, slot: str) -> list[tuple[datetime, int]]:
        """逐事件时点的可用席位序列,用于审计"发报价当时席位从哪来"。"""
        series: list[tuple[datetime, int]] = []
        occ = SlotOccupancy(capacity=self.occ[slot].capacity,
                            base_confirmed=self.occ[slot].base_confirmed)
        for ev in self.ds.events:
            if ev.slot != slot and not (
                ev.kind == "origin_released" and ev.slot == slot
            ):
                continue
            if ev.kind == "sent":
                occ.holds[ev.offer] = occ.holds.get(ev.offer, 0) + ev.seats
            elif ev.kind in ("accepted", "declined", "expired", "not_delivered"):
                occ.holds.pop(ev.offer, None)
                if ev.kind == "accepted":
                    occ.base_confirmed += ev.seats
            elif ev.kind == "origin_released":
                occ.released += min(
                    ev.seats, occ.base_confirmed - occ.released
                )
            series.append((ev.at, occ.free))
        return series


def offer_lifecycle(ds: Dataset) -> dict[str, dict]:
    """汇总每个报价的生命周期,供回放与审计复用。"""
    result: dict[str, dict] = {}
    for ev in ds.events:
        info = result.setdefault(ev.offer, {
            "offer": ev.offer,
            "request": ev.request,
            "slot": None,
            "sent_at": None,
            "expires": None,
            "terminal": None,
            "terminal_at": None,
            "seen": None,
            "seats": ev.seats,
            "requires_offer": ev.requires_offer,
            "released_origin": None,
        })
        if ev.kind == "sent":
            info["slot"] = ev.slot
            info["sent_at"] = ev.at
            info["expires"] = ev.expires
        elif ev.kind in ("accepted", "declined", "expired",
                         "not_delivered", "pending"):
            info["terminal"] = ev.kind
            info["terminal_at"] = ev.at
            info["seen"] = ev.seen
        elif ev.kind == "origin_released":
            info["released_origin"] = ev.slot
    return result
