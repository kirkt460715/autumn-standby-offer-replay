"""核心数据模型。所有时间均为带时区的 datetime(原始数据为 +08:00)。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


def parse_ts(value: str) -> datetime:
    """解析 ISO8601 时间;空串返回 None。"""
    if not value:
        return None
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Slot:
    slot: str
    day: str
    starts: str
    capacity: int
    priority_class: str

    @property
    def is_school(self) -> bool:
        return self.priority_class == "school"


@dataclass(frozen=True)
class Order:
    order_id: str
    family: str
    slot: str
    seats: int
    may_split: bool
    confirmed_at: datetime


@dataclass(frozen=True)
class WaitRequest:
    request: str
    family: str
    old_slot: str
    alternatives: tuple[str, ...]
    seats: int
    joined_at: datetime
    may_split: bool
    wait_class: str

    @property
    def is_protected(self) -> bool:
        return self.wait_class == "protected"

    @property
    def has_origin(self) -> bool:
        return bool(self.old_slot)


@dataclass(frozen=True)
class Event:
    event: str
    offer: str
    request: str
    slot: str
    kind: str
    at: datetime
    expires: datetime | None
    seen: str  # yes / no / unknown
    seats: int
    requires_offer: str  # 空串表示不依赖其他报价


@dataclass(frozen=True)
class DrawCandidate:
    draw: str
    request: str
    slot: str
    at: datetime
    probability: float
    eligible: bool
    selected: bool


@dataclass(frozen=True)
class GateRecord:
    entry: str
    family: str
    slot: str
    seats: int
    at: datetime
    status: str


@dataclass(frozen=True)
class Policy:
    policy: str
    state: str
    effective_at: datetime
    ordering: str
    expiry_minutes: int
    max_regular_wait_minutes: int
    regular_floor: float


# 报价结局分类:未送达必须与"收到后拒绝"分开
TERMINAL_KINDS = frozenset(
    {"accepted", "declined", "expired", "not_delivered", "pending"}
)
DELIVERED_TERMINAL = frozenset({"accepted", "declined", "expired"})
OFFER_LIFE_KINDS = frozenset(
    {"sent", "accepted", "declined", "expired", "not_delivered", "pending"}
)


@dataclass
class Finding:
    """资料核验发现:error 为结构性错误,warn 为业务上可解释但需披露的异常。"""

    severity: str  # error / warn
    code: str
    message: str
    refs: tuple[str, ...] = ()


@dataclass
class Dataset:
    slots: dict[str, Slot]
    orders: dict[str, Order]
    requests: dict[str, WaitRequest]
    events: list[Event]
    draws: list[DrawCandidate]
    gate: list[GateRecord]
    policies: dict[str, Policy]
    findings: list[Finding] = field(default_factory=list)

    def events_of_offer(self, offer: str) -> list[Event]:
        return sorted(
            (e for e in self.events if e.offer == offer),
            key=lambda e: (e.at, e.event),
        )

    def requests_for_day(self, day: str) -> list[WaitRequest]:
        prefix = day[8:10]  # W00 -> 02 日,按 joined_at 的日期更稳妥
        return [
            r
            for r in self.requests.values()
            if r.joined_at.date().isoformat() == day
        ]

    def family_order(self, family: str) -> Order | None:
        for order in self.orders.values():
            if order.family == family:
                return order
        return None
