"""首次达标时的正式换版导出。

换版只在 e 值首次越过阈值（默认 20，对应 α=5%）的那个批次触发一次。
导出物为自洽的 JSON 凭证，包含：

* 触发批次与当时的 e 值、阈值、累计每件平均节省；
* 逐批代价差与累计节省（可复核"每批节省多少"）；
* 代价口径与统计设计参数（保证误换上界 ≤ 5% 的前提）；
* 内容哈希，便于交接时防篡改对账。

导出动作幂等：同一试用重复导出返回同一份凭证（同一 export_id 与哈希）。
"""

import hashlib
import json
from dataclasses import dataclass

from .engine import TRIAL_QUALIFIED, TrialEngine
from .ledger import TrialError

EXPORT_KIND = "model-switch-decision"
EXPORT_VERSION = 1


@dataclass(frozen=True)
class SwitchExport:
    export_id: str
    document: dict
    content_hash: str


def build_switch_export(engine: TrialEngine, export_id: str) -> SwitchExport:
    """构造正式换版凭证；未达标则拒绝导出。"""
    if engine.status != TRIAL_QUALIFIED or engine.switch_evidence is None:
        raise TrialError("尚未达到换版证据阈值，不能导出正式换版凭证")
    snapshot = engine.snapshot()
    document = {
        "kind": EXPORT_KIND,
        "export_version": EXPORT_VERSION,
        "export_id": export_id,
        "decision": "switch_to_new_model",
        "rule": "e 过程首次越过 1/α 阈值时换版（可选停止下误换概率 ≤ α）",
        "qualification": engine.switch_evidence,
        "cost_plan": snapshot["costs"],
        "statistical_design": snapshot["design"],
        "evidence_at_export": snapshot["evidence"],
        "batch_cost_differences": snapshot["posted_batches"],
        "cumulative": snapshot["cumulative"],
    }
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    content_hash = hashlib.sha256(payload).hexdigest()
    document["content_hash"] = content_hash
    return SwitchExport(export_id=export_id, document=document, content_hash=content_hash)


def verify_hash(document: dict) -> bool:
    """复核导出凭证内嵌哈希与正文是否一致。"""
    stored = document.get("content_hash")
    if not stored:
        return False
    body = {key: value for key, value in document.items() if key != "content_hash"}
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest() == stored
