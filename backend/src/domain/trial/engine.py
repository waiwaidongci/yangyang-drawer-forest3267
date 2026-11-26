"""新旧模型试用引擎：编排锁定、入账、顺序证据与换版决策。

一个 :class:`TrialEngine` 对应一次完整试用，覆盖业务全链路：

    lock_batch（先锁新旧预判）
        → receive_inspection / receive_batch_outcomes（末检乱序到达）
        → drain（齐套后按预判顺序整批入账，重试不重复计批）
        → 每入账一批喂给 e 过程，首次越阈即标记达标
        → 达标后可导出正式换版决定（只在首次达标时生成）

引擎状态全部可序列化，进程中断后用 :meth:`TrialEngine.from_state` 恢复，
入账与达标判定均幂等。
"""

from dataclasses import dataclass

from .costs import BatchSettlement, CostPlan
from .eprocess import EProcess, TrialDesign
from .ledger import ReorderLedger, TrialError

TRIAL_RUNNING = "running"
TRIAL_QUALIFIED = "qualified"   # 已首次达标，可换版
TRIAL_ENDED = "ended"           # 手动结束且未达标（维持旧版）


@dataclass
class DrainResult:
    """一次 drain 的结果。"""

    posted: list[BatchSettlement]
    e_value: float
    mean_saving: float
    qualified: bool
    switched_at_seq: int | None


class TrialEngine:
    def __init__(self, plan: CostPlan, design: TrialDesign):
        self.plan = plan
        self.design = design
        self.ledger = ReorderLedger(plan)
        self.eprocess = EProcess(design)
        self.status = TRIAL_RUNNING
        self.switch_batch_seq: int | None = None
        self.switch_evidence: dict | None = None

    # ---- 锁定与收料（委托台账，保证先锁后收） -----------------------------
    def lock_batch(self, batch_id: str, predictions: list[dict]) -> int:
        return self.ledger.lock_batch(batch_id, predictions)

    def receive_inspection(self, batch_id: str, item_id: str, actual_failed: int) -> dict:
        return self.ledger.receive_inspection(batch_id, item_id, actual_failed)

    def receive_batch_outcomes(self, batch_id: str, outcomes: list[dict]) -> dict:
        return self.ledger.receive_batch_outcomes(batch_id, outcomes)

    # ---- 顺序入账 + 证据累加 ----------------------------------------------
    def drain(self) -> DrainResult:
        """把当前可入账的齐套批按预判顺序整批入账，并更新 e 过程。

        幂等：已入账批不会再次喂给 e 过程；重复 drain 不改变证据。
        """
        posted = self.ledger.drain_ready()
        for settlement in posted:
            self.eprocess.observe(settlement.item_count, settlement.saving)
            if self.switch_batch_seq is None and self.eprocess.qualified():
                self._mark_qualified(settlement)
        return DrainResult(
            posted=posted,
            e_value=self.eprocess.e_value,
            mean_saving=self.eprocess.mean_saving,
            qualified=self.switch_batch_seq is not None,
            switched_at_seq=self.switch_batch_seq,
        )

    def _mark_qualified(self, settlement: BatchSettlement) -> None:
        self.switch_batch_seq = settlement.batch_seq
        self.status = TRIAL_QUALIFIED
        self.switch_evidence = {
            "batch_seq": settlement.batch_seq,
            "batch_id": settlement.batch_id,
            "e_value": self.eprocess.e_value,
            "threshold": self.design.threshold,
            "mean_saving_per_item": self.eprocess.mean_saving,
            "total_items": self.eprocess.total_items,
            "batches_counted": self.eprocess.batches_counted,
        }

    def end_unsuccessful(self) -> dict:
        """未达标手动结束：维持旧版，冻结试用。达标后不允许以此方式回退。"""
        if self.switch_batch_seq is not None:
            raise TrialError("试用已达标换版，不能按未达标结束")
        self.status = TRIAL_ENDED
        return self.snapshot()

    # ---- 结果查询 ---------------------------------------------------------
    def snapshot(self) -> dict:
        """运行结果总览：状态、达标批次与各批代价差。"""
        posted = self.ledger.posted_batches()
        return {
            "status": self.status,
            "costs": {"miss_cost": self.plan.missed_failure,
                      "retest_cost": self.plan.unnecessary_retest},
            "design": {
                "target_mean_saving": self.design.target_mean_saving,
                "item_variance": self.design.item_variance,
                "alpha": self.design.alpha,
                "threshold": self.design.threshold,
            },
            "evidence": self.eprocess.to_state(),
            "switch": self.switch_evidence,
            "posted_batches": [
                {
                    "batch_seq": b.batch_seq,
                    "batch_id": b.batch_id,
                    "item_count": b.item_count,
                    "old_cost": b.old_cost,
                    "new_cost": b.new_cost,
                    "saving": b.saving,
                    "mean_saving_per_item": b.mean_saving,
                }
                for b in posted
            ],
            "pending_batches": self.ledger.pending_batches(),
            "cumulative": {
                "batches": self.eprocess.batches_counted,
                "items": self.eprocess.total_items,
                "old_cost": sum(b.old_cost for b in posted),
                "new_cost": sum(b.new_cost for b in posted),
                "saving": sum(b.saving for b in posted),
            },
        }

    def batch_cost_differences(self) -> list[dict]:
        """各批代价差（可查的逐批节省明细）。"""
        return [
            {
                "batch_seq": b.batch_seq, "batch_id": b.batch_id,
                "old_cost": b.old_cost, "new_cost": b.new_cost,
                "saving": b.saving, "item_count": b.item_count,
                "mean_saving_per_item": b.mean_saving,
            }
            for b in self.ledger.posted_batches()
        ]

    def item_evidence(self, batch_seq: int) -> list[dict]:
        """某已入账批的逐件证据（预判、真值、两套代价与单件节省）。"""
        for settlement in self.ledger.posted_batches():
            if settlement.batch_seq == batch_seq:
                return [
                    {
                        "id": item.item_id, "old_flag": item.old_flag,
                        "new_flag": item.new_flag, "actual_failed": item.actual_failed,
                        "old_cost": item.old_cost, "new_cost": item.new_cost,
                        "saving": item.saving,
                    }
                    for item in settlement.items
                ]
        raise TrialError(f"批次顺序号 {batch_seq} 尚未入账，无逐件证据")

    # ---- 持久化与恢复 -----------------------------------------------------
    def to_state(self) -> dict:
        return {
            "status": self.status,
            "switch_batch_seq": self.switch_batch_seq,
            "switch_evidence": self.switch_evidence,
            "ledger": self.ledger.to_state(),
            "eprocess": self.eprocess.to_state(),
            "design": {
                "target_mean_saving": self.design.target_mean_saving,
                "item_variance": self.design.item_variance,
                "alpha": self.design.alpha,
                "prior_precision": self.design.prior_precision,
            },
        }

    @classmethod
    def from_state(cls, state: dict) -> "TrialEngine":
        design = TrialDesign(
            target_mean_saving=state["design"]["target_mean_saving"],
            item_variance=state["design"]["item_variance"],
            alpha=state["design"]["alpha"],
            prior_precision=state["design"].get("prior_precision", 1.0),
        )
        ledger = ReorderLedger.from_state(state["ledger"])
        plan = ledger.plan
        engine = cls(plan, design)
        engine.ledger = ledger
        engine.eprocess = EProcess.from_state(design, state["eprocess"])
        engine.status = state["status"]
        engine.switch_batch_seq = state["switch_batch_seq"]
        engine.switch_evidence = state["switch_evidence"]
        return engine
