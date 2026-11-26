"""新旧模型试用接口的入参/出参模型。"""

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class TrialCreate(_Strict):
    label: str = Field(min_length=2, max_length=140)
    settings: dict[str, float | None] = Field(default_factory=dict, max_length=20)


class Prediction(_Strict):
    id: str = Field(min_length=1, max_length=80)
    old_flag: int = Field(ge=0, le=1)
    new_flag: int = Field(ge=0, le=1)


class BatchLock(_Strict):
    batch_id: str = Field(min_length=1, max_length=80)
    predictions: list[Prediction] = Field(min_length=1, max_length=5000)


class Outcome(_Strict):
    id: str = Field(min_length=1, max_length=80)
    failed: int = Field(ge=0, le=1)


class BatchOutcomes(_Strict):
    batch_id: str = Field(min_length=1, max_length=80)
    outcomes: list[Outcome] = Field(min_length=1, max_length=5000)


class SingleOutcome(_Strict):
    batch_id: str = Field(min_length=1, max_length=80)
    item_id: str = Field(min_length=1, max_length=80)
    failed: int = Field(ge=0, le=1)


class TrialEnd(_Strict):
    note: str | None = Field(default=None, max_length=500)


class ExportCreate(_Strict):
    note: str | None = Field(default=None, max_length=500)


class TrialInfo(BaseModel):
    id: int
    label: str
    created_at: str
    status: str
    settings: dict
    qualified_batch_seq: int | None
