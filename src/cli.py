"""命令行入口:核验、回放、反事实对比、核准登记、日期审计。

用法(在仓库根目录):
  python3 -I src/cli.py validate --data data
  python3 -I src/cli.py replay --data data --out outputs
  python3 -I src/cli.py compare --data data --out outputs
  python3 -I src/cli.py register --data data --out outputs
  python3 -I src/cli.py audit --data data --out outputs --day 2026-11-02
  python3 -I src/cli.py run-all --data data --out outputs
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from waitlist.audit import audit_day, explain_rule_for_slot  # noqa: E402
from waitlist.counterfactual import evaluate_policy  # noqa: E402
from waitlist.loader import load_dataset  # noqa: E402
from waitlist.metrics import historical_metrics, policy_metrics  # noqa: E402
from waitlist.registry import build_registry  # noqa: E402
from waitlist.replay import replay_history  # noqa: E402
from waitlist.report import (  # noqa: E402
    write_audit_trails,
    write_counterfactual,
    write_findings,
    write_markdown_summary,
    write_policy_metrics,
    write_registry,
    write_replay,
)


def _ensure_out(out: str | None) -> Path | None:
    if not out:
        return None
    path = Path(out)
    path.mkdir(parents=True, exist_ok=True)
    return path


def cmd_validate(args) -> int:
    ds = load_dataset(args.data)
    errors = [f for f in ds.findings if f.severity == "error"]
    warns = [f for f in ds.findings if f.severity == "warn"]
    for f in ds.findings:
        print(f"[{f.severity}] {f.code}: {f.message} refs={','.join(f.refs)}")
    print(f"\n共 {len(errors)} 个错误, {len(warns)} 个业务异常")
    out = _ensure_out(args.out)
    if out is not None:
        write_findings(ds, out)
        print(f"核验结果已写入 {out / 'findings.csv'}")
    return 1 if errors else 0


def cmd_replay(args) -> int:
    ds = load_dataset(args.data)
    replay = replay_history(ds)
    hist = historical_metrics(ds, replay)
    print("=== 历史回放(legacy)===")
    print(f"天数 {hist.days} | 报价 {hist.offers_sent} | 确认 {hist.accepted}"
          f" | 拒绝 {hist.declined} | 过期 {hist.expired_seen}"
          f" | 未送达 {hist.not_delivered} | 占席中 {hist.pending_open}")
    print(f"观察接受率(已送达有作答): {hist.observed_accept_rate}")
    print(f"决策点确认填写率: {hist.fill_rate_confirmed:.4f}")
    print(f"未确认占席席位: {hist.seats_held_wasted}")
    print(f"验票对账: {hist.gate_reconciled}/{hist.gate_total} 可对账,"
          f"{hist.gate_unreconciled} 条不可解释(已剔除)")
    print(f"原票释放: 有效 {hist.origin_releases_valid},"
          f"条件不成立 {hist.origin_releases_invalid}")
    out = _ensure_out(args.out)
    if out is not None:
        write_findings(ds, out)
        write_replay(replay, out)
        print(f"明细已写入 {out}")
    return 0


def cmd_compare(args) -> int:
    ds = load_dataset(args.data)
    replay = replay_history(ds)
    hist = historical_metrics(ds, replay)
    evals_by_policy = {
        name: evaluate_policy(ds, p)
        for name, p in sorted(ds.policies.items())
    }
    metrics_by_policy = {
        name: policy_metrics(evals)
        for name, evals in evals_by_policy.items()
    }
    print("=== 候选规则反事实对比 ===")
    header = (
        f"{'政策':<8}{'送达机会':>8}{'支撑':>6}{'无支撑':>6}{'链上':>4}{'接受':>4}"
        f"{'拒绝':>4}{'未送达':>6}{'席位下界':>10}{'上界':>8}{'点估计':>8}"
        f"{'宽度':>6}  结论"
    )
    print(header)
    for m in metrics_by_policy.values():
        print(
            f"{m.policy:<8}{m.delivery_opportunities:>8.2f}"
            f"{m.supported_candidates:>6}"
            f"{m.unsupported_candidates:>6}{m.chain_candidates:>4}"
            f"{m.observed_accepts:>4}{m.observed_declines:>4}"
            f"{m.not_delivered_kept_separate:>6}"
            f"{m.accept_seats_low:>10.2f}{m.accept_seats_high:>8.2f}"
            f"{m.accept_seats_point:>8.2f}{m.bound_width:>6.2f}  "
            f"{'试行' if m.pilot else '可上线'} {m.pilot_reasons}"
        )
    out = _ensure_out(args.out)
    if out is not None:
        write_counterfactual(evals_by_policy, out)
        write_policy_metrics(metrics_by_policy, out)
        registry = build_registry(ds)
        write_markdown_summary(ds, hist, metrics_by_policy, registry, out)
        print(f"明细与结论已写入 {out}")
    return 0


def cmd_register(args) -> int:
    ds = load_dataset(args.data)
    registry = build_registry(ds)
    print(f"=== 下一轮 {registry.round_day} 核准登记 ===")
    print(registry.evidence_note)
    print("\n未到期既有报价(旧约定保护):")
    for g in registry.grandfathered:
        print(f"  {g.offer} {g.family} {g.slot} {g.seats}席 "
              f"至 {g.expires.isoformat()} ({g.standing_at_effective})")
    print("\n场次容量:")
    for s in registry.slot_decisions:
        print(f"  {s.slot}: 容量 {s.capacity}({s.priority_class}) "
              f"既有订单 {s.base_orders} 旧报价占席 {s.grandfathered_holds} "
              f"生效时可用 {s.free_at_effective} 普通保留 {s.regular_reserve_seats}")
    print("\n政策审批:")
    for a in registry.approvals:
        print(f"  {a.policy}: {'可上线' if a.approvable else '仅试行'} "
              f"原因={a.reasons or '无'}")
        for d in a.request_decisions:
            flag = "合格" if d["eligible"] else "拒批"
            print(f"    [{flag}] {d['request']} -> {d['slot']} "
                  f"({d['wait_class']}, 等待{d['wait_minutes']}分) "
                  f"{d['reason_text']}")
    print(f"\n已消耗原票(不可重复释放): {registry.consumed_origins}")
    out = _ensure_out(args.out)
    if out is not None:
        write_registry(registry, out)
        print(f"登记已写入 {out}")
    return 0


def cmd_audit(args) -> int:
    ds = load_dataset(args.data)
    replay = replay_history(ds)
    trails = audit_day(ds, replay, args.day)
    if not trails:
        print(f"{args.day} 没有改签腾座记录")
    for t in trails:
        print(
            f"{t.release_event}: {t.freed_by_family} 腾出 {t.freed_slot} "
            f"{t.seats} 席 ({'有效' if t.valid else '无效:' + t.reason})"
        )
        if t.acquired_by_offer:
            print(f"  -> 报价 {t.acquired_by_offer} 发给 "
                  f"{t.acquired_by_family}({t.acquired_request})"
                  f" 验票回单 {t.gate_entry or '无'}")
    if args.policy and args.slot:
        evals_by_policy = {
            name: evaluate_policy(ds, p)
            for name, p in sorted(ds.policies.items())
        }
        # 取该日该场的决策时点
        at = next(
            (e.at for e in evals_by_policy[args.policy]
             if e.day == args.day and e.slot == args.slot),
            None,
        )
        if at is None:
            print(f"未找到 {args.day} {args.slot} 的决策点")
            return 1
        explanation = explain_rule_for_slot(
            ds, args.policy, args.slot, at, evals_by_policy
        )
        print(f"\n规则 {args.policy} 对 {args.slot} 的适用性:")
        print(f"  {explanation['headline']}")
        for c in explanation["candidate_reasons"]:
            print(f"  {c['request']}({c['wait_class']}) support={c['support']} "
                  f"响应={c['response_status']} 原因={c['reason_text']}")
    out = _ensure_out(args.out)
    if out is not None:
        write_audit_trails(trails, out, args.day)
        print(f"审计链路已写入 {out / f'audit_{args.day}.csv'}")
    return 0


def cmd_run_all(args) -> int:
    ds = load_dataset(args.data)
    out = _ensure_out(args.out)
    if out is None:
        print("run-all 需要 --out")
        return 2
    replay = replay_history(ds)
    hist = historical_metrics(ds, replay)
    evals_by_policy = {
        name: evaluate_policy(ds, p)
        for name, p in sorted(ds.policies.items())
    }
    metrics_by_policy = {
        name: policy_metrics(evals)
        for name, evals in evals_by_policy.items()
    }
    registry = build_registry(ds)
    write_findings(ds, out)
    write_replay(replay, out)
    write_counterfactual(evals_by_policy, out)
    write_policy_metrics(metrics_by_policy, out)
    write_registry(registry, out)
    for day in sorted({d.day for d in replay.days.values()}):
        write_audit_trails(audit_day(ds, replay, day), out, day)
    write_markdown_summary(ds, hist, metrics_by_policy, registry, out)
    print(f"全部结果已写入 {out}")
    print(f"核验发现 {len(ds.findings)} 项;报告见 {out / 'summary.md'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="候补放号规则回放与评估")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", default="data", help="CSV 数据目录")
    common.add_argument("--out", default="outputs", help="输出目录")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", parents=[common]).set_defaults(func=cmd_validate)
    sub.add_parser("replay", parents=[common]).set_defaults(func=cmd_replay)
    sub.add_parser("compare", parents=[common]).set_defaults(func=cmd_compare)
    sub.add_parser("register", parents=[common]).set_defaults(func=cmd_register)
    p_audit = sub.add_parser("audit", parents=[common])
    p_audit.add_argument("--day", required=True, help="日期 YYYY-MM-DD")
    p_audit.add_argument("--policy", help="可选:解释某政策对某场的适用性")
    p_audit.add_argument("--slot", help="可选:场次编号,与 --policy 同用")
    p_audit.set_defaults(func=cmd_audit)
    sub.add_parser("run-all", parents=[common]).set_defaults(func=cmd_run_all)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
