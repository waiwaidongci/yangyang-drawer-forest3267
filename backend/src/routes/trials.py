"""新旧模型试用路由。

写操作要求分析员/负责人（Writer），正式换版导出仅负责人（Admin），
查看接口三角色可用。所有写接口内部幂等，可安全重试。
"""

from fastapi import APIRouter

from src.controllers.dependencies import Actor, Admin, Db, Writer
from src.schemas.trials import (
    BatchLock, BatchOutcomes, ExportCreate, SingleOutcome, TrialCreate, TrialEnd,
)
from src.services import trials

router = APIRouter(prefix='/model-trials')


@router.get('')
def list_all(session: Db, actor: Actor):
    return trials.list_trials(session, actor)


@router.post('', status_code=201)
def create(payload: TrialCreate, session: Db, actor: Writer):
    return trials.create_trial(session, payload, actor)


@router.get('/{trial_id}')
def detail(trial_id: int, session: Db, actor: Actor):
    return trials.get_trial(session, trial_id, actor)


@router.post('/{trial_id}/lock')
def lock(trial_id: int, payload: BatchLock, session: Db, actor: Writer):
    return trials.lock_batch(session, trial_id, payload, actor)


@router.post('/{trial_id}/inspections')
def inspections(trial_id: int, payload: BatchOutcomes, session: Db, actor: Writer):
    return trials.receive_outcomes(session, trial_id, payload, actor)


@router.post('/{trial_id}/inspection')
def inspection(trial_id: int, payload: SingleOutcome, session: Db, actor: Writer):
    return trials.receive_one(session, trial_id, payload, actor)


@router.post('/{trial_id}/drain')
def drain(trial_id: int, session: Db, actor: Writer):
    return trials.drain(session, trial_id, actor)


@router.post('/{trial_id}/end')
def end(trial_id: int, payload: TrialEnd, session: Db, actor: Writer):
    return trials.end_trial(session, trial_id, actor)


@router.get('/{trial_id}/batch-differences')
def differences(trial_id: int, session: Db, actor: Actor):
    return trials.batch_differences(session, trial_id, actor)


@router.get('/{trial_id}/evidence/{batch_seq}')
def evidence(trial_id: int, batch_seq: int, session: Db, actor: Actor):
    return trials.item_evidence(session, trial_id, batch_seq, actor)


@router.post('/{trial_id}/export', status_code=201)
def export(trial_id: int, payload: ExportCreate, session: Db, actor: Admin):
    return trials.export_switch(session, trial_id, actor, payload.note)


@router.get('/{trial_id}/export')
def get_export(trial_id: int, session: Db, actor: Actor):
    return trials.get_export(session, trial_id, actor)
