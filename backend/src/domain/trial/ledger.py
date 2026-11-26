"""末检乱序到达的整排重排缓冲与顺序入账台账。

约束（对应业务口径）
--------------------
1. 每批先由 :mod:`lock` 锁定新旧两套预判，取得预判顺序号 ``batch_seq``（1 起）。
2. 末检记录乱序到达：先进入对应批次的待齐缓冲；一批全部件到齐才"齐套"。
3. 即使后批先齐，也必须等前批入账——按 ``batch_seq`` 严格顺序整批入账。
4. 入账是幂等的：同一批重复提交（中断恢复后重试）不会重复计批；
   e 过程只在"首次入账"时累加一次。
5. 所有状态可序列化到数据库，进程中断后凭持久化状态完整恢复。

本模块不依赖数据库框架，纯内存 + 字典状态，便于单元测试与被服务层复用。
"""

from dataclasses import dataclass, field

from .costs import BatchSettlement, CostPlan, ItemResult, score_item, settle_batch


class TrialError(ValueError):
    """试用流程类错误（锁定/收料/入账违反顺序或一致性）。"""


@dataclass
class LockedItem:
    """一件被锁定的预判（末检到达前不可变）。"""

    item_id: str
    old_flag: int
    new_flag: int


@dataclass
class LockedBatch:
    """一批的锁定预判与齐套缓冲。"""

    batch_seq: int
    batch_id: str
    locked: dict[str, LockedItem]
    received: dict[str, int] = field(default_factory=dict)  # item_id -> actual_failed
    settled: bool = False

    def is_complete(self) -> bool:
        return len(self.received) == len(self.locked) and len(self.locked) > 0

    def missing(self) -> list[str]:
        return sorted(set(self.locked) - set(self.received))


@dataclass
class PostedBatch:
    """已入账批次的不可变记录。"""

    settlement: BatchSettlement
    old_cost: int
    new_cost: int
    saving: int
    item_count: int


class ReorderLedger:
    """管理锁定、乱序收料、顺序入账与幂等恢复。"""

    def __init__(self, plan: CostPlan):
        self.plan = plan
        self._batches: dict[int, LockedBatch] = {}
        self._batch_id_to_seq: dict[str, int] = {}
        self._posted: dict[int, PostedBatch] = {}
        self._next_seq = 1

    # ---- 锁定（先于末检） -------------------------------------------------
    def lock_batch(self, batch_id: str, predictions: list[dict]) -> int:
        """登记一批的新旧预判，返回分配的预判顺序号。

        predictions 每项形如
        ``{"id": 总成编号, "old_flag": 0/1, "new_flag": 0/1}``。
        同一 batch_id 重复锁定且内容一致时返回原顺序号（幂等）；不一致则报错。
        """
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise TrialError("批次编号不能为空")
        batch_id = batch_id.strip()
        items = self._parse_predictions(predictions)
        existing_seq = self._batch_id_to_seq.get(batch_id)
        if existing_seq is not None:
            return self._assert_same_lock(existing_seq, items)
        seq = self._next_seq
        self._next_seq += 1
        self._batches[seq] = LockedBatch(
            batch_seq=seq, batch_id=batch_id,
            locked={item.item_id: item for item in items},
        )
        self._batch_id_to_seq[batch_id] = seq
        return seq

    def _parse_predictions(self, predictions) -> list[LockedItem]:
        if not isinstance(predictions, list) or not predictions:
            raise TrialError("锁定预判须为非空数组")
        parsed: list[LockedItem] = []
        seen = set()
        for entry in predictions:
            if not isinstance(entry, dict) or set(entry) - {"id", "old_flag", "new_flag"}:
                raise TrialError("每条锁定预判只允许 id、old_flag、new_flag 三个字段")
            item_id = entry.get("id")
            if not isinstance(item_id, str) or not item_id.strip():
                raise TrialError("总成编号不能为空")
            item_id = item_id.strip()
            if item_id in seen:
                raise TrialError(f"批次内总成编号重复（{item_id}）")
            seen.add(item_id)
            old_flag = entry.get("old_flag")
            new_flag = entry.get("new_flag")
            self._check_binary(old_flag, "旧版预判")
            self._check_binary(new_flag, "新版预判")
            parsed.append(LockedItem(item_id, old_flag, new_flag))
        return parsed

    @staticmethod
    def _check_binary(value, label: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value not in (0, 1):
            raise TrialError(f"{label}须为 0 或 1")

    def _assert_same_lock(self, seq: int, items: list[LockedItem]) -> int:
        locked = self._batches[seq].locked
        if len(items) != len(locked) or any(
            item.item_id not in locked
            or locked[item.item_id].old_flag != item.old_flag
            or locked[item.item_id].new_flag != item.new_flag
            for item in items
        ):
            raise TrialError("同一批次重复锁定的预判与首次锁定不一致，锁定后不可更改")
        return seq

    # ---- 乱序收料 ---------------------------------------------------------
    def receive_inspection(self, batch_id: str, item_id: str, actual_failed: int) -> dict:
        """接收一条末检确认（允许乱序、允许重复幂等）。"""
        seq = self._require_batch(batch_id)
        batch = self._batches[seq]
        if batch.settled:
            raise TrialError(f"批次 {batch_id} 已入账，末检记录不可再变更")
        self._check_binary(actual_failed, "末检结果")
        if item_id not in batch.locked:
            raise TrialError(f"总成 {item_id} 不在批次 {batch_id} 的锁定清单内")
        previous = batch.received.get(item_id)
        if previous is not None and previous != actual_failed:
            raise TrialError(f"总成 {item_id} 的末检结果与已收记录冲突，且不可更改")
        batch.received[item_id] = actual_failed
        return {
            "batch_seq": seq, "batch_id": batch.batch_id,
            "received": len(batch.received), "expected": len(batch.locked),
            "complete": batch.is_complete(),
        }

    def receive_batch_outcomes(self, batch_id: str, outcomes: list[dict]) -> dict:
        """一次性接收某批的末检结果数组，逐条幂等写入。

        outcomes 每项形如 ``{"id": 总成编号, "failed": 0/1}``。
        """
        seq = self._require_batch(batch_id)
        if not isinstance(outcomes, list) or not outcomes:
            raise TrialError("末检结果须为非空数组")
        snapshot = None
        for entry in outcomes:
            if not isinstance(entry, dict) or set(entry) - {"id", "failed"}:
                raise TrialError("每条末检结果只允许 id、failed 两个字段")
            snapshot = self.receive_inspection(batch_id, entry.get("id"), entry.get("failed"))
        return snapshot or self.batch_status(seq)

    def _require_batch(self, batch_id: str) -> int:
        if not isinstance(batch_id, str) or batch_id.strip() not in self._batch_id_to_seq:
            raise TrialError(f"批次 {batch_id} 尚未锁定预判，不能接收末检")
        return self._batch_id_to_seq[batch_id.strip()]

    # ---- 顺序入账 ---------------------------------------------------------
    def drain_ready(self) -> list[BatchSettlement]:
        """从最早未入账批开始，把所有"前序已入账且自身齐套"的批依次整批入账。

        后批即使先齐也会等待，返回本次实际入账（首次）的批次结算列表，顺序为 seq 升序。
        已入账批不会重复计入，因此中断恢复后重试安全。
        """
        newly_posted: list[BatchSettlement] = []
        for seq in range(1, self._next_seq):
            batch = self._batches.get(seq)
            if batch is None or batch.settled:
                continue
            if not batch.is_complete():
                # 顺序闸门：遇到第一个未齐套的前序批即停止，后面的批继续等待。
                break
            settlement = self._post(batch)
            newly_posted.append(settlement)
        return newly_posted

    def _post(self, batch: LockedBatch) -> BatchSettlement:
        items: list[ItemResult] = []
        for item_id in sorted(batch.locked):
            locked = batch.locked[item_id]
            items.append(score_item(
                item_id, locked.old_flag, locked.new_flag,
                batch.received[item_id], self.plan,
            ))
        settlement = settle_batch(batch.batch_seq, batch.batch_id, items)
        batch.settled = True
        self._posted[batch.batch_seq] = PostedBatch(
            settlement=settlement, old_cost=settlement.old_cost,
            new_cost=settlement.new_cost, saving=settlement.saving,
            item_count=settlement.item_count,
        )
        return settlement

    # ---- 查询与状态 -------------------------------------------------------
    def batch_status(self, seq: int) -> dict:
        batch = self._batches[seq]
        return {
            "batch_seq": seq, "batch_id": batch.batch_id,
            "expected": len(batch.locked), "received": len(batch.received),
            "complete": batch.is_complete(), "settled": batch.settled,
            "missing": batch.missing(),
        }

    def pending_batches(self) -> list[dict]:
        """仍未入账的批次（含等待前序的齐套批），按预判顺序返回。"""
        return [
            self.batch_status(seq)
            for seq in range(1, self._next_seq)
            if not self._batches[seq].settled
        ]

    def posted_batches(self) -> list[BatchSettlement]:
        return [self._posted[seq].settlement for seq in sorted(self._posted)]

    def is_posted(self, seq: int) -> bool:
        return seq in self._posted

    def to_state(self) -> dict:
        """导出可持久化的完整状态。"""
        return {
            "plan": {"missed_failure": self.plan.missed_failure,
                     "unnecessary_retest": self.plan.unnecessary_retest},
            "next_seq": self._next_seq,
            "batches": [
                {
                    "batch_seq": b.batch_seq, "batch_id": b.batch_id,
                    "locked": [
                        {"id": item.item_id, "old_flag": item.old_flag, "new_flag": item.new_flag}
                        for item in b.locked.values()
                    ],
                    "received": dict(b.received),
                    "settled": b.settled,
                }
                for b in (self._batches[s] for s in sorted(self._batches))
            ],
        }

    @classmethod
    def from_state(cls, state: dict) -> "ReorderLedger":
        plan = CostPlan(state["plan"]["missed_failure"], state["plan"]["unnecessary_retest"])
        ledger = cls(plan)
        for raw in state["batches"]:
            ledger.lock_batch(raw["batch_id"], raw["locked"])
            for item_id, failed in raw["received"].items():
                ledger.receive_inspection(raw["batch_id"], item_id, failed)
            if raw["settled"]:
                ledger.drain_ready()
        # 恢复顺序号游标，否则重建后新锁定的批次会从 1 重新编号。
        ledger._next_seq = int(state["next_seq"])
        return ledger
