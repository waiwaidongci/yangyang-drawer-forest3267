"""滑轨分诊新旧模型在线试用语。

顺序：先锁定新旧预判 → 乱序收末检 → 按预判顺序整批入账（重试不重复计批）
→ e 过程累计证据 → 首次越阈（默认 e≥20，误换上界 5%）导出换版凭证。
"""

from .costs import CostPlan, ItemResult, BatchSettlement, score_item, settle_batch
from .eprocess import ALPHA, THRESHOLD, EProcess, TrialDesign, projected_items_to_target
from .engine import (
    TRIAL_ENDED, TRIAL_QUALIFIED, TRIAL_RUNNING, DrainResult, TrialEngine,
)
from .exporter import SwitchExport, build_switch_export, verify_hash
from .ledger import LockedBatch, LockedItem, ReorderLedger, TrialError

__all__ = [
    "ALPHA", "THRESHOLD",
    "CostPlan", "ItemResult", "BatchSettlement", "score_item", "settle_batch",
    "TrialDesign", "EProcess", "projected_items_to_target",
    "TrialEngine", "DrainResult",
    "TRIAL_RUNNING", "TRIAL_QUALIFIED", "TRIAL_ENDED",
    "ReorderLedger", "LockedBatch", "LockedItem", "TrialError",
    "SwitchExport", "build_switch_export", "verify_hash",
]
