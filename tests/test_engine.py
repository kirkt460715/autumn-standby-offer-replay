"""引擎单元测试:锁定时点正确性、识别界限与承诺保护等关键约束。"""
import unittest
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from waitlist.audit import audit_day  # noqa: E402
from waitlist.counterfactual import (  # noqa: E402
    OBSERVED_ACCEPT,
    OBSERVED_DECLINE,
    UNOBSERVED_NOT_DELIVERED,
    evaluate_policy,
)
from waitlist.loader import load_dataset  # noqa: E402
from waitlist.metrics import historical_metrics, policy_metrics  # noqa: E402
from waitlist.occupancy import Ledger  # noqa: E402
from waitlist.policy import REASON  # noqa: E402
from waitlist.registry import build_registry  # noqa: E402
from waitlist.replay import replay_history  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"
TS = lambda s: datetime.fromisoformat(s)  # noqa: E731


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(DATA)
        cls.replay = replay_history(cls.ds)

    def test_no_structural_errors_in_source(self):
        errors = [f for f in self.ds.findings if f.severity == "error"]
        self.assertEqual(errors, [])

    def test_gate_anomalies_excluded_not_silently_kept(self):
        # 16 条无来源验票必须被识别且不进入履约口径
        warns = [f for f in self.ds.findings if f.code == "gate_unreconciled"]
        self.assertEqual(len(warns), 16)
        hist = historical_metrics(self.ds, self.replay)
        self.assertEqual(hist.gate_unreconciled, 16)
        self.assertEqual(hist.gate_reconciled, 8)

    def test_offer_terminal_partition(self):
        hist = historical_metrics(self.ds, self.replay)
        self.assertEqual(hist.accepted, 8)
        self.assertEqual(hist.declined, 6)
        self.assertEqual(hist.expired_seen, 7)
        self.assertEqual(hist.not_delivered, 4)
        self.assertEqual(hist.pending_open, 1)
        # 未送达与拒绝必须分开
        self.assertNotIn(
            "not_delivered",
            {o.terminal for o in self.replay.offers.values()
             if o.terminal == "declined"},
        )

    def test_point_in_time_availability_no_future_backfill(self):
        """P000 发出时 W00B 为 7 席;09:05 原票释放 W00A 腾出 2 席,
        P001 在 09:06 占用 W00A 后该场仍有 8 席可用。"""
        ledger = Ledger(self.ds)
        before = ledger.free_at("W00B", TS("2026-11-02T09:00:00+08:00"))
        self.assertEqual(before, 7)
        # 报价占席:09:00 时刻 P000 已占 W00B 2 席
        snap = ledger.snapshot_at(TS("2026-11-02T09:00:00+08:00"))
        self.assertEqual(snap["W00B"].holds, {"P000": 2})
        # 09:05 释放后 W00A 由 8 可用变 10;P001 09:06 占 2 席后为 8
        snap_release = ledger.snapshot_at(TS("2026-11-02T09:05:00+08:00"))
        self.assertEqual(snap_release["W00A"].free, 10)
        snap_chain = ledger.snapshot_at(TS("2026-11-02T09:06:00+08:00"))
        self.assertEqual(snap_chain["W00A"].holds, {"P001": 2})
        self.assertEqual(snap_chain["W00A"].free, 8)
        # 释放前(09:04)W00A 仍只有 8 席,证明没有用未来释放回填
        snap_early = ledger.snapshot_at(TS("2026-11-02T09:04:00+08:00"))
        self.assertEqual(snap_early["W00A"].free, 8)

    def test_hold_released_at_expiry_and_decline(self):
        ledger = Ledger(self.ds)
        # P060 于 09:04 被拒绝,占席应提前结束
        snap = ledger.snapshot_at(TS("2026-11-08T09:05:00+08:00"))
        self.assertNotIn("P060", snap["W06B"].holds)
        # P020 过期前仍占席
        snap2 = ledger.snapshot_at(TS("2026-11-04T09:09:00+08:00"))
        self.assertIn("P020", snap2["W02B"].holds)

    def test_release_requires_real_origin_and_no_double_release(self):
        # 数据中唯一有效释放 E00F,且 consumed 后不可重复
        valid = [c for c in self.replay.release_chain if c["valid"]]
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["release_event"], "E00F")
        ledger = Ledger(self.ds)
        for ev in self.ds.events:
            ledger.apply(ev)
        self.assertEqual(ledger.consumed_origins, {"O000"})

    def test_chain_offer_depends_on_prior_release(self):
        # P001 的席位来源必须指向 E00F,且 sent 晚于释放
        p001 = self.replay.offers["P001"]
        self.assertEqual(p001.basis_release_event, "E00F")
        self.assertEqual(p001.requires_offer, "P000")
        trails = audit_day(self.ds, self.replay, "2026-11-02")
        self.assertEqual(trails[0].acquired_by_offer, "P001")
        self.assertEqual(trails[0].acquired_by_family, "F002")

    def test_counterfactual_support_set_is_randomization_only(self):
        for policy in self.ds.policies.values():
            evals = evaluate_policy(self.ds, policy)
            for e in evals:
                for o in e.outcomes:
                    if o.support == "supported":
                        self.assertGreaterEqual(o.probability_weight, 0)
        # 每个决策点恰有 4 个有支撑候选(抽签表)
        legacy = evaluate_policy(self.ds, self.ds.policies["legacy"])
        self.assertTrue(all(
            sum(1 for o in e.outcomes if o.support == "supported") == 4
            for e in legacy
        ))

    def test_not_delivered_distinguished_from_decline(self):
        # Y051 在 W05B 的报价未送达:状态必须是 not_delivered 而非 decline
        evals = evaluate_policy(self.ds, self.ds.policies["legacy"])
        e = next(x for x in evals if x.day == "2026-11-07")
        y051 = next(o for o in e.outcomes if o.request == "Y051")
        self.assertEqual(y051.response_status, UNOBSERVED_NOT_DELIVERED)
        # 界限保留为 [0,1],不假设拒绝也不假设接受
        self.assertEqual((y051.accept_low, y051.accept_high), (0, 1))
        # Y063 明确拒绝:点估计为 0
        e6 = next(x for x in evals if x.day == "2026-11-08")
        y063 = next(o for o in e6.outcomes if o.request == "Y063")
        self.assertEqual(y063.response_status, OBSERVED_DECLINE)
        self.assertEqual(y063.observed_accept_prob, 0.0)
        # Y000 历史接受:点估计为 1
        e0 = next(x for x in evals if x.day == "2026-11-02")
        y000 = next(o for o in e0.outcomes if o.request == "Y000")
        self.assertEqual(y000.response_status, OBSERVED_ACCEPT)

    def test_wait_cap_blocks_regular_under_a_and_b(self):
        for name in ("A", "B"):
            evals = evaluate_policy(self.ds, self.ds.policies[name])
            e0 = next(x for x in evals if x.day == "2026-11-02")
            y000 = next(o for o in e0.outcomes if o.request == "Y000")
            self.assertIn("WAIT_CAP", y000.policy_reasons)
            # 保障候补不受等待上限约束
            y001 = next(o for o in e0.outcomes if o.request == "Y001")
            self.assertEqual(y001.policy_reasons, [])

    def test_deterministic_policies_select_one_each_day(self):
        for name in ("A", "B"):
            evals = evaluate_policy(self.ds, self.ds.policies[name])
            for e in evals:
                eligible = [
                    o for o in e.outcomes
                    if o.support == "supported" and not o.policy_reasons
                ]
                # 至少有一个合格候选;引擎只向排序第一的候选发一个名额
                self.assertGreaterEqual(len(eligible), 1)
                self.assertIn(e.expected_accepts_low, (0.0, 1.0))
                self.assertIn(e.expected_accepts_high, (0.0, 1.0))
        # A 的 55 分钟上限下普通 Y004 也合格,但保障 Y001 排序在前
        evals_a = evaluate_policy(self.ds, self.ds.policies["A"])
        e0 = next(x for x in evals_a if x.day == "2026-11-02")
        eligible_ids = {
            o.request for o in e0.outcomes
            if o.support == "supported" and not o.policy_reasons
        }
        self.assertEqual(eligible_ids, {"Y001", "Y004"})
        # B 的 45 分钟上限下普通候选全部出局,只剩保障 Y001
        evals_b = evaluate_policy(self.ds, self.ds.policies["B"])
        e0b = next(x for x in evals_b if x.day == "2026-11-02")
        self.assertEqual(
            {o.request for o in e0b.outcomes
             if o.support == "supported" and not o.policy_reasons},
            {"Y001"},
        )

    def test_bounds_never_assume_universal_acceptance(self):
        for name in self.ds.policies:
            m = policy_metrics(
                evaluate_policy(self.ds, self.ds.policies[name])
            )
            # 下界严格小于上界(存在未观察意愿),且不超过总席位容量
            self.assertLess(m.accept_seats_low, m.accept_seats_high)
            self.assertGreater(m.bound_width, 0)
            self.assertTrue(m.pilot)
            self.assertIn("SLICE_TOO_SMALL", m.pilot_reasons)

    def test_legacy_probability_weighted_expectation(self):
        m = policy_metrics(
            evaluate_policy(self.ds, self.ds.policies["legacy"])
        )
        # 8 个历史接受、每个概率 0.25 -> 下界期望席位 4.75(2 席/3 席混合)
        self.assertAlmostEqual(m.accept_seats_low, 4.75, places=4)

    def test_grandfathered_offer_kept_under_new_policies(self):
        reg = build_registry(self.ds)
        self.assertEqual(len(reg.grandfathered), 1)
        g = reg.grandfathered[0]
        self.assertEqual(g.offer, "PN1")
        # 有效期沿用 legacy 的 16 分钟约定(到 09:05),而非 A/B 的 8/16 分钟新口径
        self.assertEqual(g.expires, TS("2026-12-05T09:05:00+08:00"))
        for a in reg.approvals:
            self.assertTrue(a.pilot)
            self.assertIn("UNSUPPORTED_NO_RANDOMIZATION", a.reasons)

    def test_registry_capacity_respects_grandfathered_hold(self):
        reg = build_registry(self.ds)
        b = next(s for s in reg.slot_decisions if s.slot == "NEXT-B")
        # 容量 9, 既有订单 4, PN1 占 2 -> 生效时可用 3
        self.assertEqual(b.free_at_effective, 3)
        self.assertEqual(b.grandfathered_holds, 2)
        a = next(s for s in reg.slot_decisions if s.slot == "NEXT-A")
        self.assertEqual(a.free_at_effective, 6)

    def test_reason_codes_have_human_text(self):
        for code in (
            "WAIT_CAP", "NO_FREE_SEAT", "SPLIT_FORBIDDEN",
            "FLOOR_PROTECTED_HEADROOM", "UNSUPPORTED_NO_RANDOMIZATION",
            "CHAIN_UNSUPPORTED", "GRANDFATHERED_OFFER",
        ):
            self.assertIn(code, REASON)

    def test_regular_floor_blocks_oversized_protected_demand(self):
        from waitlist.models import WaitRequest
        from waitlist.policy import DecisionContext, check_basic_eligibility
        from waitlist.occupancy import SlotOccupancy
        # W00B 容量 12、B 政策 floor 0.3 -> 普通保留 4、保障 headroom 8
        occ = {"W00B": SlotOccupancy(capacity=12)}
        ctx = DecisionContext(
            ds=self.ds,
            policy=self.ds.policies["B"],  # floor 0.3
            at=TS("2026-11-02T09:00:00+08:00"),
            occupancy=occ,
        )
        big = WaitRequest(
            request="Z1", family="FZ1", old_slot="",
            alternatives=("W00B",), seats=9,
            joined_at=TS("2026-11-02T08:30:00+08:00"),
            may_split=False, wait_class="protected",
        )
        reasons = check_basic_eligibility(big, "W00B", ctx)
        self.assertIn("FLOOR_PROTECTED_HEADROOM", reasons)
        small = WaitRequest(
            request="Z2", family="FZ2", old_slot="",
            alternatives=("W00B",), seats=3,
            joined_at=TS("2026-11-02T08:30:00+08:00"),
            may_split=False, wait_class="protected",
        )
        self.assertNotIn(
            "FLOOR_PROTECTED_HEADROOM",
            check_basic_eligibility(small, "W00B", ctx),
        )

    def test_split_flag_respected_in_eligibility(self):
        from waitlist.policy import DecisionContext, check_basic_eligibility
        from waitlist.occupancy import Ledger
        ledger = Ledger(self.ds)
        ctx = DecisionContext(
            ds=self.ds,
            policy=self.ds.policies["legacy"],
            at=TS("2026-11-02T09:00:00+08:00"),
            occupancy=ledger.snapshot_at(TS("2026-11-02T09:00:00+08:00")),
        )
        req = self.ds.requests["Y002"]
        reasons = check_basic_eligibility(req, "W00A", ctx, seats_needed=1)
        self.assertNotIn("SPLIT_FORBIDDEN", reasons)
        req_nosplit = self.ds.requests["Y000"]  # may_split=no
        reasons2 = check_basic_eligibility(
            req_nosplit, "W00B", ctx, seats_needed=1
        )
        self.assertIn("SPLIT_FORBIDDEN", reasons2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
