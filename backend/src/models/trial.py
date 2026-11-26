"""新旧模型试用的持久化模型。

引擎本身是纯内存、可序列化的（见 :mod:`src.domain.trial.engine`）。
这里用两张表落库：

* :class:`Trial`：一次试用的配置与引擎快照（JSON），状态变更整体覆盖写；
* :class:`TrialExport`：首次达标时生成的换版凭证（仅一条，唯一约束保证只导出一次）。

所有跨请求的恢复都从 ``engine_state`` 反序列化，因此进程中断后重试不重复计批。
"""

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database import Base


class Trial(Base):
    __tablename__ = 'model_trials'

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(140))
    created_by: Mapped[int] = mapped_column(ForeignKey('accounts.id'))
    created_at: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), index=True)
    # 代价口径与统计设计（创建后冻结）。
    settings: Mapped[dict] = mapped_column(JSON)
    # 引擎完整快照：锁定清单、末检缓冲、已入账批、e 过程累加状态。
    engine_state: Mapped[dict] = mapped_column(JSON)
    qualified_batch_seq: Mapped[int | None] = mapped_column(nullable=True)


class TrialExport(Base):
    __tablename__ = 'model_trial_exports'
    __table_args__ = (UniqueConstraint('trial_id', name='uq_trial_single_export'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey('model_trials.id'), index=True)
    export_id: Mapped[str] = mapped_column(String(64), unique=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[int] = mapped_column(ForeignKey('accounts.id'))
    created_at: Mapped[str] = mapped_column(String(32))
    document: Mapped[dict] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
