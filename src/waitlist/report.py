"""把核验、回放、反事实与核准结果写成 CSV 明细和 Markdown 结论。"""
from __future__ import annotations

import csv
from pathlib import Path

from .audit import SeatTrail
from .counterfactual import DecisionEvaluation
from .metrics import HistoricalMetrics, PolicyCounterfactualMetrics
from .models import Dataset
from .registry import Registry
from .replay import ReplayResult


def _write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_findings(ds: Dataset, out: Path) -> None:
    _write_csv(
        out / "findings.csv",
        ["severity", "code", "message", "refs"],
        [
            {
                "severity": f.severity,
                "code": f.code,
                "message": f.message,
                "refs": "|".join(f.refs),
            }
            for f in ds.findings
        ],
    )


def write_replay(replay: ReplayResult, out: Path) -> None:
    _write_csv(
        out / "historical_offers.csv",
        [
            "offer", "request", "family", "slot", "seats", "sent_at",
            "expires", "terminal", "terminal_at", "seen",
            "requires_offer", "basis_release_event", "released_origin",
        ],
        [
            {
                "offer": o.offer,
                "request": o.request,
                "family": o.family,
                "slot": o.slot,
                "seats": o.seats,
                "sent_at": o.sent_at.isoformat() if o.sent_at else "",
                "expires": o.expires.isoformat() if o.expires else "",
                "terminal": o.terminal,
                "terminal_at": (
                    o.terminal_at.isoformat() if o.terminal_at else ""
                ),
                "seen": o.seen,
                "requires_offer": o.requires_offer,
                "basis_release_event": o.basis_release_event or "",
                "released_origin": o.released_origin or "",
            }
            for o in sorted(
                replay.offers.values(),
                key=lambda x: x.sent_at or x.offer,
            )
        ],
    )
    _write_csv(
        out / "send_availability.csv",
        ["day", "offer", "free_seats_at_send"],
        [
            {
                "day": day.day,
                "offer": offer,
                "free_seats_at_send": free,
            }
            for day in replay.days.values()
            for offer, free in sorted(day.send_availability.items())
        ],
    )
    _write_csv(
        out / "release_chain.csv",
        [
            "release_event", "at", "slot", "seats", "family", "valid",
            "reason", "basis_offer", "downstream_offers",
            "downstream_families",
        ],
        [
            {
                **c,
                "downstream_offers": "|".join(c["downstream_offers"]),
                "downstream_families": "|".join(c["downstream_families"]),
            }
            for c in replay.release_chain
        ],
    )
    _write_csv(
        out / "gate_reconciliation.csv",
        ["entry", "status"],
        [
            {"entry": e, "status": "reconciled"}
            for e in replay.gate_reconciled
        ]
        + [
            {"entry": e, "status": "unreconciled"}
            for e in replay.gate_unreconciled
        ],
    )


def write_counterfactual(
    evals_by_policy: dict[str, list[DecisionEvaluation]], out: Path
) -> None:
    rows = []
    for policy, evals in evals_by_policy.items():
        for e in evals:
            for o in e.outcomes:
                rows.append({
                    "policy": policy,
                    "day": e.day,
                    "slot": e.slot,
                    "decision_at": e.at.isoformat(),
                    "free_seats": e.free_seats_at_decision,
                    "request": o.request,
                    "family": o.family,
                    "wait_class": o.wait_class,
                    "seats": o.seats,
                    "wait_minutes": round(o.wait_minutes_at_decision, 1),
                    "support": o.support,
                    "chosen": "yes" if o.chosen else "no",
                    "eligible_under_policy": not o.policy_reasons,
                    "policy_reasons": "|".join(o.policy_reasons),
                    "response_status": o.response_status or "",
                    "accept_low": o.accept_low,
                    "accept_high": o.accept_high,
                    "observed_accept_prob": (
                        "" if o.observed_accept_prob is None
                        else o.observed_accept_prob
                    ),
                    "probability_weight": o.probability_weight,
                })
    _write_csv(
        out / "counterfactual_decisions.csv",
        [
            "policy", "day", "slot", "decision_at", "free_seats", "request",
            "family", "wait_class", "seats", "wait_minutes", "support",
            "chosen", "eligible_under_policy", "policy_reasons",
            "response_status", "accept_low", "accept_high",
            "observed_accept_prob", "probability_weight",
        ],
        rows,
    )


def write_policy_metrics(
    metrics_by_policy: dict[str, PolicyCounterfactualMetrics], out: Path
) -> None:
    _write_csv(
        out / "policy_comparison.csv",
        [
            "policy", "decisions", "supported_candidates",
            "unsupported_candidates", "chain_candidates",
            "observed_accepts", "observed_declines",
            "not_delivered_kept_separate", "delivery_opportunities",
            "accept_seats_low", "accept_seats_high", "accept_seats_point",
            "observed_share", "bound_width", "pilot", "pilot_reasons",
        ],
        [
            {
                "policy": m.policy,
                "decisions": m.decisions,
                "supported_candidates": m.supported_candidates,
                "unsupported_candidates": m.unsupported_candidates,
                "chain_candidates": m.chain_candidates,
                "observed_accepts": m.observed_accepts,
                "observed_declines": m.observed_declines,
                "not_delivered_kept_separate":
                    m.not_delivered_kept_separate,
                "delivery_opportunities": round(m.delivery_opportunities, 4),
                "accept_seats_low": round(m.accept_seats_low, 4),
                "accept_seats_high": round(m.accept_seats_high, 4),
                "accept_seats_point": round(m.accept_seats_point, 4),
                "observed_share": round(m.observed_share, 4),
                "bound_width": round(m.bound_width, 4),
                "pilot": "yes" if m.pilot else "no",
                "pilot_reasons": "|".join(m.pilot_reasons),
            }
            for m in metrics_by_policy.values()
        ],
    )


def write_registry(registry: Registry, out: Path) -> None:
    _write_csv(
        out / "next_round_grandfathered.csv",
        [
            "offer", "request", "family", "slot", "seats", "sent_at",
            "expires", "terms", "standing_at_effective",
        ],
        [
            {
                "offer": g.offer,
                "request": g.request,
                "family": g.family,
                "slot": g.slot,
                "seats": g.seats,
                "sent_at": g.sent_at.isoformat(),
                "expires": g.expires.isoformat(),
                "terms": g.terms,
                "standing_at_effective": g.standing_at_effective,
            }
            for g in registry.grandfathered
        ],
    )
    _write_csv(
        out / "next_round_slots.csv",
        [
            "slot", "capacity", "priority_class", "base_orders",
            "grandfathered_holds", "free_at_effective",
            "regular_reserve_seats", "protected_headroom",
        ],
        [vars(s) for s in registry.slot_decisions],
    )
    rows = []
    for a in registry.approvals:
        for d in a.request_decisions:
            rows.append({
                "policy": a.policy,
                "approvable": "yes" if a.approvable else "no",
                "pilot": "yes" if a.pilot else "no",
                "reasons": "|".join(a.reasons),
                "ordering": a.ordering,
                "expiry_minutes": a.expiry_minutes,
                "max_regular_wait_minutes": a.max_regular_wait_minutes,
                "regular_floor": a.regular_floor,
                "effective_at": a.effective_at.isoformat(),
                **d,
                "reasons": "|".join(d["reasons"]),
                "reason_text": "|".join(d["reason_text"]),
            })
    _write_csv(
        out / "next_round_policy_checks.csv",
        [
            "policy", "approvable", "pilot", "reasons", "ordering",
            "expiry_minutes", "max_regular_wait_minutes", "regular_floor",
            "effective_at", "request", "family", "slot", "seats",
            "wait_class", "wait_minutes", "eligible", "reasons",
            "reason_text",
        ],
        rows,
    )


def write_audit_trails(trails: list[SeatTrail], out: Path, day: str) -> None:
    _write_csv(
        out / f"audit_{day}.csv",
        [
            "day", "release_event", "freed_slot", "seats",
            "freed_by_family", "basis_offer", "released_at", "valid",
            "reason", "acquired_by_offer", "acquired_by_family",
            "acquired_request", "gate_entry",
        ],
        [vars(t) for t in trails],
    )


def write_markdown_summary(
    ds: Dataset,
    hist: HistoricalMetrics,
    metrics_by_policy: dict[str, PolicyCounterfactualMetrics],
    registry: Registry,
    out: Path,
) -> None:
    lines = [
        "# 候补放号规则回放与反事实评估结论",
        "",
        "## 1. 资料核验",
        "",
        f"- 结构性错误(error):{sum(1 for f in ds.findings if f.severity == 'error')} 项",
        f"- 业务异常(warn):{sum(1 for f in ds.findings if f.severity == 'warn')} 项",
        "- 主要异常:验票回单中存在无法与确认报价或原始订单对账的记录"
        "(gate_unreconciled),已从履约口径剔除并在 findings.csv 披露;",
        "  原票释放仅在家庭订单确实位于被释放场次时生效,空 origin/外部订单不产生释放。",
        "",
        "## 2. 历史回放(legacy 已执行规则)",
        "",
        f"- 回放天数:{hist.days};发出报价:{hist.offers_sent}",
        f"- 确认 {hist.accepted} / 拒绝 {hist.declined} / 过期未作答 {hist.expired_seen}"
        f" / 未送达 {hist.not_delivered} / 仍在占席 {hist.pending_open}",
        f"- 已送达且有作答者的观察接受率:"
        f"{'-' if hist.observed_accept_rate is None else round(hist.observed_accept_rate, 4)}",
        f"- 决策点确认填写率(确认席位/当时可用席位):{round(hist.fill_rate_confirmed, 4)}",
        f"- 未确认报价占席席位(浪费):{hist.seats_held_wasted}",
        f"- 有效原票释放 {hist.origin_releases_valid} 次,条件不成立 {hist.origin_releases_invalid} 次",
        f"- 验票回单 {hist.gate_total} 条:可对账 {hist.gate_reconciled},"
        f"不可对账 {hist.gate_unreconciled}(不纳入任何口径)",
        "- 未送达(not_delivered, seen=no)与收到后拒绝(declined)全程分开统计。",
        "",
        "## 3. 候选规则反事实对比(仅含随机/概率记录支撑的决策点)",
        "",
        "| 政策 | 送达机会(家庭) | 支撑候选 | 无支撑候选 | 链上候选 | 观察接受 | 观察拒绝 | "
        "确认席位下界 | 上界 | 点估计 | 界限宽度 | 结论 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in metrics_by_policy.values():
        lines.append(
            f"| {m.policy} | {round(m.delivery_opportunities, 2)} "
            f"| {m.supported_candidates} | {m.unsupported_candidates} "
            f"| {m.chain_candidates} | {m.observed_accepts} | {m.observed_declines} "
            f"| {round(m.accept_seats_low, 2)} | {round(m.accept_seats_high, 2)} "
            f"| {round(m.accept_seats_point, 2)} | {round(m.bound_width, 2)} "
            f"| {'试行' if m.pilot else '可上线'} |"
        )
    lines += [
        "",
        "界限说明:下界把所有未观察意愿(未送达、过期未作答、旧规则未收到报价)视为拒绝,",
        "上界视为接受;不假设新增候补必然接受。legacy 按 randomization 概率给期望,",
        "A/B 按确定性排序给点估计。界限越宽,证据越弱。",
        "",
        "## 4. 下一轮(" + registry.round_day + ")核准登记",
        "",
        f"- {registry.evidence_note}",
        "- 未到期既有报价(按发出时 legacy 约定继续有效,新政策不得改动):",
    ]
    if registry.grandfathered:
        for g in registry.grandfathered:
            lines.append(
                f"  - {g.offer}: {g.family} 的 {g.seats} 席 {g.slot},"
                f"有效期至 {g.expires.isoformat()},{g.standing_at_effective}"
            )
    else:
        lines.append("  - 无")
    lines.append("")
    for a in registry.approvals:
        lines.append(
            f"- 政策 {a.policy}:{'可上线' if a.approvable else '仅试行'}"
            f"(排序 {a.ordering}, 有效期 {a.expiry_minutes} 分钟,"
            f"普通等待上限 {a.max_regular_wait_minutes} 分钟,"
            f"普通保留比例 {a.regular_floor});原因: {', '.join(a.reasons) or '无'}"
        )
    lines += [
        "",
        "## 5. 持续跟踪与审计",
        "",
        "- 新一轮履约按同一套数据约定回灌:sent/终局/释放/验票均保留真实时刻;",
        "- 审计命令可查指定日期哪些座位由改签腾出、由谁获得、是否验票入园;",
        "- 已用于换票的原票记入 consumed_origins,不可重复释放;",
        "- 规则适用性原因码见 counterfactual_decisions.csv 与 next_round_policy_checks.csv。",
        "",
    ]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
