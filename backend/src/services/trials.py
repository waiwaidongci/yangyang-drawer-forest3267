"""新旧模型试用的应用服务：编排引擎、落库、恢复、导出与权限。

每个写操作都走同一套"加载快照 → 反序列化引擎 → 执行 → 序列化覆盖写"流程，
因此中断恢复后重试不会重复计批（引擎内入账与 e 过程累加均幂等）。
"""

import uuid

from sqlalchemy import select

from src.domain.trial import (
    CostPlan, TrialDesign, TrialEngine, TrialError, build_switch_export,
)
from src.models.trial import Trial, TrialExport
from src.utils.time import utc_stamp

# 与 policy 对齐的代价参数描述，供 settings 校验与接口文档复用。
TRIAL_SETTINGS = [
    {"key": "miss_cost", "label": "漏报代价（等效工时分钟）", "default": 12, "min": 1, "max": 100},
    {"key": "retest_cost", "label": "误报代价（等效工时分钟）", "default": 3, "min": 1, "max": 100},
    {"key": "target_mean_saving", "label": "目标每件平均节省（等效分钟）", "default": 1.0,
     "min": 0.000001, "max": 1000.0},
    {"key": "item_variance", "label": "每件节省方差规划上界（留空取代价有界上界）",
     "default": None, "min": 0.000000001, "max": 10000.0},
]


def validate_settings(settings: dict) -> dict:
    if not isinstance(settings, dict):
        raise TrialError('试用设置须为键值对象')
    allowed = {item['key'] for item in TRIAL_SETTINGS}
    unknown = set(settings) - allowed
    if unknown:
        raise TrialError('试用设置包含未知参数：' + ', '.join(sorted(unknown)))
    plan = CostPlan.parse(settings)
    target_raw = settings.get('target_mean_saving', 1.0)
    try:
        target = float(target_raw)
    except (TypeError, ValueError) as exc:
        raise TrialError('目标每件平均节省须为正数') from exc
    variance = None
    if settings.get('item_variance') is not None:
        try:
            variance = float(settings['item_variance'])
        except (TypeError, ValueError) as exc:
            raise TrialError('每件节省方差规划上界须为正数') from exc
    # 构造时完成范围与有界性校验。
    TrialDesign.create(
        target_mean_saving=target,
        max_item_cost=plan.max_item_cost(),
        item_variance=variance,
    )
    return {
        'miss_cost': plan.missed_failure,
        'retest_cost': plan.unnecessary_retest,
        'target_mean_saving': target,
        'item_variance': variance,
    }


def _build_engine(settings: dict) -> TrialEngine:
    plan = CostPlan(settings['miss_cost'], settings['retest_cost'])
    design = TrialDesign.create(
        target_mean_saving=settings['target_mean_saving'],
        max_item_cost=plan.max_item_cost(),
        item_variance=settings['item_variance'],
    )
    return TrialEngine(plan, design)


def create_trial(session, payload, actor) -> dict:
    settings = validate_settings(payload.settings)
    engine = _build_engine(settings)
    trial = Trial(
        label=payload.label, created_by=actor.id, created_at=utc_stamp(),
        status=engine.status, settings=settings,
        engine_state=engine.to_state(), qualified_batch_seq=None,
    )
    session.add(trial)
    session.commit()
    session.refresh(trial)
    return trial_info(trial)


def _load(session, trial_id: int) -> Trial:
    trial = session.get(Trial, trial_id)
    if trial is None:
        raise LookupError('找不到该试用')
    return trial


def _engine(trial: Trial) -> TrialEngine:
    return TrialEngine.from_state(trial.engine_state)


def _persist(session, trial: Trial, engine: TrialEngine) -> None:
    trial.engine_state = engine.to_state()
    trial.status = engine.status
    trial.qualified_batch_seq = engine.switch_batch_seq
    session.commit()


def lock_batch(session, trial_id: int, payload, actor) -> dict:
    trial = _load(session, trial_id)
    engine = _engine(trial)
    seq = engine.lock_batch(payload.batch_id, payload.predictions)
    _persist(session, trial, engine)
    return {'batch_seq': seq, 'batch_id': payload.batch_id, **engine.ledger.batch_status(seq)}


def receive_outcomes(session, trial_id: int, payload, actor) -> dict:
    trial = _load(session, trial_id)
    engine = _engine(trial)
    status = engine.receive_batch_outcomes(payload.batch_id, payload.outcomes)
    _persist(session, trial, engine)
    return status


def receive_one(session, trial_id: int, payload, actor) -> dict:
    trial = _load(session, trial_id)
    engine = _engine(trial)
    status = engine.receive_inspection(payload.batch_id, payload.item_id, payload.failed)
    _persist(session, trial, engine)
    return status


def drain(session, trial_id: int, actor) -> dict:
    """触发顺序入账与证据更新；重复调用幂等，不重复计批。"""
    trial = _load(session, trial_id)
    engine = _engine(trial)
    result = engine.drain()
    _persist(session, trial, engine)
    snapshot = engine.snapshot()
    return {
        'newly_posted': [
            {'batch_seq': b.batch_seq, 'batch_id': b.batch_id, 'saving': b.saving,
             'old_cost': b.old_cost, 'new_cost': b.new_cost, 'item_count': b.item_count}
            for b in result.posted
        ],
        'e_value': result.e_value,
        'mean_saving': result.mean_saving,
        'qualified': result.qualified,
        'switched_at_seq': result.switched_at_seq,
        'pending_batches': snapshot['pending_batches'],
        'cumulative': snapshot['cumulative'],
    }


def end_trial(session, trial_id: int, actor) -> dict:
    trial = _load(session, trial_id)
    engine = _engine(trial)
    engine.end_unsuccessful()
    _persist(session, trial, engine)
    return trial_info(trial)


def get_trial(session, trial_id: int, actor) -> dict:
    trial = _load(session, trial_id)
    info = trial_info(trial)
    info['result'] = _engine(trial).snapshot()
    return info


def list_trials(session, actor) -> list[dict]:
    rows = session.execute(select(Trial).order_by(Trial.id.desc()).limit(100)).scalars()
    return [trial_info(trial) for trial in rows]


def batch_differences(session, trial_id: int, actor) -> list[dict]:
    trial = _load(session, trial_id)
    return _engine(trial).batch_cost_differences()


def item_evidence(session, trial_id: int, batch_seq: int, actor) -> list[dict]:
    trial = _load(session, trial_id)
    return _engine(trial).item_evidence(batch_seq)


def export_switch(session, trial_id: int, actor, note: str | None = None) -> dict:
    """负责人在首次达标后导出正式换版凭证（幂等，仅一份）。"""
    trial = _load(session, trial_id)
    existing = session.scalar(select(TrialExport).where(TrialExport.trial_id == trial_id))
    if existing is not None:
        return export_info(existing)
    engine = _engine(trial)
    export_id = f'SWITCH-{trial_id}-{uuid.uuid4().hex[:10]}'
    switch = build_switch_export(engine, export_id)
    record = TrialExport(
        trial_id=trial_id, export_id=export_id, content_hash=switch.content_hash,
        created_by=actor.id, created_at=utc_stamp(), document=switch.document, note=note,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return export_info(record)


def get_export(session, trial_id: int, actor) -> dict:
    record = session.scalar(select(TrialExport).where(TrialExport.trial_id == trial_id))
    if record is None:
        raise LookupError('该试用尚未导出换版凭证')
    return export_info(record)


def trial_info(trial: Trial) -> dict:
    return {
        'id': trial.id, 'label': trial.label, 'created_at': trial.created_at,
        'status': trial.status, 'settings': trial.settings,
        'qualified_batch_seq': trial.qualified_batch_seq,
    }


def export_info(record: TrialExport) -> dict:
    return {
        'id': record.id, 'trial_id': record.trial_id, 'export_id': record.export_id,
        'content_hash': record.content_hash, 'created_at': record.created_at,
        'note': record.note, 'document': record.document,
    }
