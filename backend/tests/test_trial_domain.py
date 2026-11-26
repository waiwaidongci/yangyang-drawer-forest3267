"""试用域逻辑的单元测试（仅标准库，python -I -m unittest 可跑）。"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.domain.trial import (  # noqa: E402
    ALPHA, CostPlan, EProcess, TrialDesign, TrialEngine, TrialError,
    build_switch_export, verify_hash,
)
from src.domain.trial.ledger import ReorderLedger  # noqa: E402


def plan(miss=12, retest=3):
    return CostPlan(miss, retest)


class CostTests(unittest.TestCase):
    def test_item_cost_outcomes(self):
        p = plan()
        # 预判优先返测(1) 但实际通过(0) -> 误报
        self.assertEqual(p.item_cost(1, 0), 3)
        # 预判常规(0) 但实际失败(1) -> 漏报
        self.assertEqual(p.item_cost(0, 1), 12)
        # 两种正确判定代价为 0
        self.assertEqual(p.item_cost(1, 1), 0)
        self.assertEqual(p.item_cost(0, 0), 0)

    def test_saving_positive_when_new_cheaper(self):
        p = plan()
        # 旧版漏报(12)，新版正确拦截(0) -> 省 12
        from src.domain.trial import score_item
        item = score_item("x", old_flag=0, new_flag=1, actual_failed=1, plan=p)
        self.assertEqual(item.old_cost, 12)
        self.assertEqual(item.new_cost, 0)
        self.assertEqual(item.saving, 12)

    def test_cost_validation(self):
        with self.assertRaises(ValueError):
            CostPlan(0, 3)
        with self.assertRaises(ValueError):
            CostPlan(12, 101)
        with self.assertRaises(ValueError):
            CostPlan(True, 3)  # type: ignore[arg-type]
        parsed = CostPlan.parse({"miss_cost": "6", "retest_cost": 5})
        self.assertEqual((parsed.missed_failure, parsed.unnecessary_retest), (6, 5))
        with self.assertRaises(ValueError):
            CostPlan.parse({"miss_cost": 6, "retest_cost": 5, "extra": 1})


class LedgerOrderingTests(unittest.TestCase):
    def _lock_three(self, ledger):
        for seq, bid in enumerate(("B1", "B2", "B3"), start=1):
            got = ledger.lock_batch(bid, [
                {"id": f"{bid}-1", "old_flag": 0, "new_flag": 1},
                {"id": f"{bid}-2", "old_flag": 1, "new_flag": 0},
            ])
            self.assertEqual(got, seq)

    def test_must_lock_before_receive(self):
        ledger = ReorderLedger(plan())
        with self.assertRaises(TrialError):
            ledger.receive_inspection("B9", "B9-1", 0)

    def test_duplicate_lock_is_idempotent_but_conflict_rejected(self):
        ledger = ReorderLedger(plan())
        seq1 = ledger.lock_batch("B1", [{"id": "a", "old_flag": 0, "new_flag": 1}])
        seq2 = ledger.lock_batch("B1", [{"id": "a", "old_flag": 0, "new_flag": 1}])
        self.assertEqual(seq1, seq2)
        with self.assertRaises(TrialError):
            ledger.lock_batch("B1", [{"id": "a", "old_flag": 1, "new_flag": 1}])

    def test_out_of_order_completion_respects_preceding_gate(self):
        ledger = ReorderLedger(plan())
        self._lock_three(ledger)
        # 后批 B2 先齐：不能入账（前批 B1 未齐）。
        ledger.receive_batch_outcomes("B2", [{"id": "B2-1", "failed": 1}, {"id": "B2-2", "failed": 0}])
        self.assertEqual(ledger.drain_ready(), [])
        self.assertEqual([b["batch_id"] for b in ledger.pending_batches()], ["B1", "B2", "B3"])

        # B1 齐套后，drain 应按顺序把 B1、B2 一起整批入账。
        ledger.receive_batch_outcomes("B1", [{"id": "B1-1", "failed": 1}, {"id": "B1-2", "failed": 0}])
        posted = ledger.drain_ready()
        self.assertEqual([b.batch_id for b in posted], ["B1", "B2"])
        self.assertTrue(ledger.is_posted(1) and ledger.is_posted(2))
        self.assertFalse(ledger.is_posted(3))

        # 再 drain 不会重复计批。
        self.assertEqual(ledger.drain_ready(), [])

    def test_receive_duplicate_ok_conflict_rejected(self):
        ledger = ReorderLedger(plan())
        ledger.lock_batch("B1", [{"id": "a", "old_flag": 0, "new_flag": 1}])
        ledger.receive_inspection("B1", "a", 1)
        ledger.receive_inspection("B1", "a", 1)  # 幂等
        with self.assertRaises(TrialError):
            ledger.receive_inspection("B1", "a", 0)  # 冲突
        with self.assertRaises(TrialError):
            ledger.receive_inspection("B1", "ghost", 0)  # 不在锁定清单

    def test_unknown_item_and_partial_completion(self):
        ledger = ReorderLedger(plan())
        ledger.lock_batch("B1", [{"id": "a", "old_flag": 0, "new_flag": 1},
                                 {"id": "b", "old_flag": 1, "new_flag": 0}])
        ledger.receive_inspection("B1", "a", 1)
        self.assertFalse(ledger.batch_status(1)["complete"])
        self.assertEqual(ledger.batch_status(1)["missing"], ["b"])
        ledger.receive_inspection("B1", "b", 0)
        self.assertTrue(ledger.batch_status(1)["complete"])


class IdempotencyAndRecoveryTests(unittest.TestCase):
    def test_engine_state_roundtrip_keeps_evidence(self):
        engine = TrialEngine(plan(), TrialDesign.create(1.0, 12))
        engine.lock_batch("B1", [{"id": "a", "old_flag": 0, "new_flag": 1}])
        engine.receive_inspection("B1", "a", 1)
        before = engine.snapshot()
        restored = TrialEngine.from_state(engine.to_state())
        # 恢复后再 drain，与不中断时结果一致。
        r1 = engine.drain()
        r2 = restored.drain()
        self.assertEqual(r1.e_value, r2.e_value)
        self.assertEqual(r1.posted[0].saving, r2.posted[0].saving)
        self.assertEqual(before["status"], "running")
        self.assertEqual(restored.snapshot()["cumulative"], engine.snapshot()["cumulative"])

    def test_retry_drain_does_not_double_count(self):
        engine = TrialEngine(plan(), TrialDesign.create(1.0, 12))
        engine.lock_batch("B1", [{"id": "a", "old_flag": 0, "new_flag": 1}])
        engine.receive_inspection("B1", "a", 1)
        first = engine.drain()
        e_after_first = first.e_value
        for _ in range(5):  # 模拟中断恢复后反复重试
            again = engine.drain()
            self.assertEqual(again.e_value, e_after_first)
            self.assertEqual(again.posted, [])
        self.assertEqual(engine.snapshot()["cumulative"]["batches"], 1)

    def test_ledger_state_roundtrip_midstream(self):
        ledger = ReorderLedger(plan())
        ledger.lock_batch("B1", [{"id": "a", "old_flag": 0, "new_flag": 1}])
        ledger.lock_batch("B2", [{"id": "b", "old_flag": 1, "new_flag": 0}])
        ledger.receive_inspection("B1", "a", 1)
        restored = ReorderLedger.from_state(ledger.to_state())
        self.assertEqual(restored.pending_batches(), ledger.pending_batches())
        # B1 入账后再恢复，B2 仍等待（未收料）。
        restored.drain_ready()
        self.assertTrue(restored.is_posted(1))
        self.assertFalse(restored.is_posted(2))

    def test_restore_then_lock_continues_sequence(self):
        # 回归：恢复后再锁定新批次，顺序号必须接续而不是从 1 重新开始。
        ledger = ReorderLedger(plan())
        ledger.lock_batch("B1", [{"id": "a", "old_flag": 0, "new_flag": 1}])
        ledger.lock_batch("B2", [{"id": "b", "old_flag": 1, "new_flag": 0}])
        restored = ReorderLedger.from_state(ledger.to_state())
        next_seq = restored.lock_batch("B3", [{"id": "c", "old_flag": 0, "new_flag": 1}])
        self.assertEqual(next_seq, 3)
        self.assertEqual(
            [b["batch_seq"] for b in restored.pending_batches()], [1, 2, 3],
        )


class QualificationAndExportTests(unittest.TestCase):
    def test_zero_saving_never_qualifies(self):
        engine = TrialEngine(plan(), TrialDesign.create(1.0, 12))
        # 新旧预判完全一致 -> 每件节省恒为 0。
        for b in range(1, 30):
            bid = f"B{b}"
            engine.lock_batch(bid, [{"id": f"{bid}-{i}", "old_flag": 1, "new_flag": 1}
                                    for i in range(12)])
            engine.receive_batch_outcomes(bid, [{"id": f"{bid}-{i}", "failed": i % 2}
                                                for i in range(12)])
            engine.drain()
        self.assertIsNone(engine.switch_batch_seq)
        self.assertEqual(engine.snapshot()["cumulative"]["saving"], 0)
        self.assertLess(engine.eprocess.e_value, 1.0)

    def test_qualification_and_first_switch_export(self):
        # 让新版每批都明显省工：旧版系统性漏报、新版正确拦截。
        engine = TrialEngine(plan(), TrialDesign.create(1.0, 12))
        switched_seq = None
        for b in range(1, 40):
            bid = f"B{b}"
            engine.lock_batch(bid, [{"id": f"{bid}-{i}", "old_flag": 0, "new_flag": 1}
                                    for i in range(12)])
            # 全部实际失败：旧版每件漏报 12，新版正确 -> 每批省 144。
            engine.receive_batch_outcomes(bid, [{"id": f"{bid}-{i}", "failed": 1}
                                                for i in range(12)])
            result = engine.drain()
            if result.qualified and switched_seq is None:
                switched_seq = result.switched_at_seq
                break
        self.assertIsNotNone(switched_seq)
        self.assertEqual(engine.status, "qualified")
        # 首次达标批次可查，各批代价差可查。
        snap = engine.snapshot()
        self.assertEqual(snap["switch"]["batch_seq"], switched_seq)
        diffs = engine.batch_cost_differences()
        self.assertEqual(len(diffs), switched_seq)
        self.assertTrue(all(d["saving"] == 144 for d in diffs))
        self.assertEqual(snap["evidence"]["e_value"], engine.eprocess.e_value)

        # 达标前不能导出；达标后导出，且幂等同哈希。
        export = build_switch_export(engine, "SWITCH-TEST-1")
        self.assertTrue(verify_hash(export.document))
        again = build_switch_export(engine, "SWITCH-TEST-1")
        self.assertEqual(again.content_hash, export.content_hash)
        self.assertEqual(export.document["decision"], "switch_to_new_model")

        # 已达标不能按未达标结束。
        with self.assertRaises(TrialError):
            engine.end_unsuccessful()

    def test_export_blocked_before_qualification(self):
        engine = TrialEngine(plan(), TrialDesign.create(1.0, 12))
        with self.assertRaises(TrialError):
            build_switch_export(engine, "SWITCH-X")

    def test_end_unsuccessful_keeps_old_model(self):
        engine = TrialEngine(plan(), TrialDesign.create(1.0, 12))
        engine.lock_batch("B1", [{"id": "a", "old_flag": 1, "new_flag": 1}])
        engine.receive_inspection("B1", "a", 0)
        engine.drain()
        info = engine.end_unsuccessful()
        self.assertEqual(info["status"], "ended")
        with self.assertRaises(TrialError):
            build_switch_export(engine, "SWITCH-Y")

    def test_item_evidence_only_after_settlement(self):
        engine = TrialEngine(plan(), TrialDesign.create(1.0, 12))
        engine.lock_batch("B1", [{"id": "a", "old_flag": 0, "new_flag": 1}])
        with self.assertRaises(TrialError):
            engine.item_evidence(1)
        engine.receive_inspection("B1", "a", 1)
        engine.drain()
        evidence = engine.item_evidence(1)
        self.assertEqual(evidence[0]["id"], "a")
        self.assertEqual(evidence[0]["saving"], 12)


class EProcessBoundTests(unittest.TestCase):
    def test_design_rejects_variance_above_bound(self):
        # 有界性：方差上界不能超过最大单件代价平方。
        with self.assertRaises(ValueError):
            TrialDesign.create(1.0, max_item_cost=12, item_variance=145.0)
        design = TrialDesign.create(1.0, 12)
        self.assertEqual(design.item_variance, 144.0)
        self.assertEqual(design.threshold, 1.0 / ALPHA)

    def test_observe_validation(self):
        ep = EProcess(TrialDesign.create(1.0, 12))
        with self.assertRaises(ValueError):
            ep.observe(0, 10)
        with self.assertRaises(ValueError):
            ep.observe(12, float("nan"))

    def test_null_bound_by_monte_carlo(self):
        # 固定种子的零漂移蒙特卡洛：可选停止下误触率必须 ≤ 5%。
        design = TrialDesign.create(1.0, 12)
        rng = random.Random(20261126)
        paths, hits = 4000, 0
        for _ in range(paths):
            ep = EProcess(design)
            for _ in range(40):
                saving = sum(rng.choice((-12, 12)) for _ in range(12))
                ep.observe(12, saving)
                if ep.qualified():
                    hits += 1
                    break
        self.assertLessEqual(hits / paths, ALPHA)

    def test_negative_drift_also_controlled(self):
        design = TrialDesign.create(1.0, 12)
        rng = random.Random(99)
        paths, hits = 2000, 0
        for _ in range(paths):
            ep = EProcess(design)
            for _ in range(40):
                saving = sum(rng.choice((-13, 11)) for _ in range(12))  # 每件 -1
                ep.observe(12, saving)
                if ep.qualified():
                    hits += 1
                    break
        self.assertLessEqual(hits / paths, ALPHA)

    def test_e_process_symmetric_in_saving_sign(self):
        # 闭式只依赖累计节省 A 的平方：相同 |A|、相同件数给出相同 e 值。
        design = TrialDesign.create(1.0, 12)
        positive = EProcess(design)
        negative = EProcess(design)
        positive.observe(12, 60)
        negative.observe(12, -60)
        self.assertAlmostEqual(positive.e_value, negative.e_value, places=12)

    def test_recompute_log_matches_incremental(self):
        # 逐批累加的 log_e 与用最终 (A, N) 一次性复算一致（恢复口径自洽）。
        design = TrialDesign.create(1.0, 12)
        ep = EProcess(design)
        for saving in (36, -12, 48, 0, 24):
            ep.observe(12, saving)
        total_saving, total_items = ep.total_saving, ep.total_items
        rebuilt = ep._compute_log(total_saving, total_items)
        self.assertAlmostEqual(ep.log_e, rebuilt, places=12)


class QualificationPersistenceTests(unittest.TestCase):
    def test_first_qualified_batch_frozen_after_more_postings(self):
        engine = TrialEngine(plan(), TrialDesign.create(1.0, 12))
        first_switch = None
        for b in range(1, 12):
            bid = f"B{b}"
            engine.lock_batch(bid, [{"id": f"{bid}-{i}", "old_flag": 0, "new_flag": 1}
                                    for i in range(12)])
            engine.receive_batch_outcomes(bid, [{"id": f"{bid}-{i}", "failed": 1}
                                                for i in range(12)])
            result = engine.drain()
            if first_switch is None and result.qualified:
                first_switch = result.switched_at_seq
        # 达标后继续入账，首个达标批次不变，状态保持 qualified。
        self.assertIsNotNone(first_switch)
        self.assertEqual(engine.switch_batch_seq, first_switch)
        self.assertEqual(engine.status, "qualified")
        # 序列化-反序列化后首个达标批次与证据一致。
        restored = TrialEngine.from_state(engine.to_state())
        self.assertEqual(restored.switch_batch_seq, first_switch)
        self.assertAlmostEqual(restored.eprocess.e_value, engine.eprocess.e_value, places=12)
        self.assertEqual(restored.snapshot()["switch"], engine.snapshot()["switch"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
