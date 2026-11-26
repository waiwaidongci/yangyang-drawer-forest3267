"""顺序证据（e 过程）与 5% 误换上界。

业务目标
--------
对每一批先锁定新旧两套预判，再收末检真值，按同一套误报/漏报代价核算
"新版相对旧版每件平均节省" ``Y``。试用允许在**任意批次后**凭累计证据换版，
并要求：若新版平均并不省工（零或负的每件节省），从试用开始到结束，
发生"误换"的概率不超过 5%。

方法
----
采用零假设下的**非负上鞅（e 过程）**。设第 k 批有 n_k 件、批次总节省 S_k、
每件节省方差不超过 σ²（单件代价有界，可用最大单件代价平方作保守上界），
把"每件节省"建模为有界方差的次高斯增量，在零假设 H0: μ ≤ 0 下构造
零均值正态混合 e 过程（Howard et al., 2021 的混合线性边界）：

    E_k = ∫_0^∞ exp( Σ_j [θ·S_j − (σ²·θ²/2)·n_j] ) · p(θ) dθ

取混合先验 θ ~ N(0, 1/V0) 的闭式形式。该乘积型 e 过程在 H0 下为非负上鞅，
E[E_k] ≤ 1，故由 Ville 不等式，对任意（数据驱动、可选停止的）停时 τ：

    P0( 存在 k≤τ 使 E_k ≥ 1/α ) ≤ α，   α = 0.05 → 阈值 20。

这与"看多少批、何时停止、是否中断恢复"无关，天然满足可选停止与恢复重试；
e 值只依赖已入账批次的 (n, S)，重试不重复计批时证据不变。

数值实现用对数空间的 log-sum-exp 形式，避免长试用下指数溢出。
"""

import math
from dataclasses import dataclass

ALPHA = 0.05
# Ville 阈值：e 值达到 1/α 即判达标换版。
THRESHOLD = 1.0 / ALPHA  # 20.0
# 混合先验 N(0, 1/V0) 的先验精度尺度；只影响功效，不影响一类错误控制。
DEFAULT_PRIOR_PRECISION = 1.0
MIN_TARGET = 1e-6
MAX_TARGET = 1000.0
MIN_VARIANCE = 1e-9


@dataclass(frozen=True)
class TrialDesign:
    """试用的统计设计参数。

    target_mean_saving: 希望证实的每件平均节省下限 δ（等效分钟，须 > 0）。
        零假设为 μ ≤ 0，备择为 μ ≥ δ；δ 仅用于功效沟通与导出，不参与 e 值计算。
    item_variance: 每件节省 Y 的方差规划上界 σ²。默认 None 时由代价上界给出
        保守值 max(miss,retest)²，保证有界性结论成立；可由历史数据给更小值以提效。
    alpha: 误换上界，固定 0.05。
    """

    target_mean_saving: float
    item_variance: float
    alpha: float = ALPHA
    prior_precision: float = DEFAULT_PRIOR_PRECISION

    @property
    def threshold(self) -> float:
        return 1.0 / self.alpha

    @classmethod
    def create(cls, target_mean_saving: float, max_item_cost: int,
               item_variance: float | None = None, alpha: float = ALPHA,
               prior_precision: float = DEFAULT_PRIOR_PRECISION) -> "TrialDesign":
        target = _finite_float(target_mean_saving, "目标每件平均节省")
        if not MIN_TARGET <= target <= MAX_TARGET:
            raise ValueError(f"目标每件平均节省须在 {MIN_TARGET} 到 {MAX_TARGET} 之间")
        if not math.isfinite(alpha) or not 0.0 < alpha < 0.5:
            raise ValueError("误换上界 alpha 须在 0 到 0.5 之间")
        if not math.isfinite(prior_precision) or prior_precision <= 0:
            raise ValueError("先验精度尺度须为正数")
        if item_variance is None:
            # 单件节省 ∈ [-c_max, c_max]，Popoviciu 上界 σ² ≤ (range)²/4 = c_max²。
            variance = float(max_item_cost) ** 2
        else:
            variance = _finite_float(item_variance, "每件节省方差上界")
            if variance < MIN_VARIANCE:
                raise ValueError(f"每件节省方差上界不能小于 {MIN_VARIANCE}")
            # 不允许比有界性给出的保守上界还大得离谱，但允许更小（历史估计更紧）。
            if variance > float(max_item_cost) ** 2 + 1e-9:
                raise ValueError("每件节省方差上界不能超过最大单件代价的平方（有界性结论）")
        return cls(target_mean_saving=target, item_variance=variance,
                   alpha=alpha, prior_precision=prior_precision)


def _finite_float(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}须为有限数值")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label}须为有限数值")
    return number


@dataclass
class EProcess:
    """可序列化、可恢复的零漂移混合 e 过程累加状态。

    持久化字段即恢复所需的全部信息：log_e、累计件数与累计节省。
    """

    design: TrialDesign
    log_e: float = 0.0
    total_items: int = 0
    total_saving: float = 0.0
    batches_counted: int = 0

    @property
    def e_value(self) -> float:
        """当前累计 e 值（始终为正；初始为 1）。"""
        return math.exp(self.log_e)

    @property
    def mean_saving(self) -> float:
        """截至目前每件平均节省。"""
        return self.total_saving / self.total_items if self.total_items else 0.0

    def observe(self, batch_item_count: int, batch_saving: float) -> float:
        """并入一个已入账批次，返回更新后的 log e 值。

        闭式增量（先验 θ ~ N(0, 1/V0)，记 V0=prior_precision，σ²=v）：
            令 A = Σ S_j， B = v·Σ n_j，
            log E = log V0/2 − log(V0+B)/2 + A² / [2(V0+B)]。
        每批只需累加 A、B，天然幂等可恢复。
        """
        if not isinstance(batch_item_count, int) or batch_item_count <= 0:
            raise ValueError("入账户数须为正整数")
        saving = _finite_float(batch_saving, "批次节省")
        self.total_items += batch_item_count
        self.total_saving += saving
        self.batches_counted += 1
        self.log_e = self._compute_log(self.total_saving, self.total_items)
        return self.log_e

    def _compute_log(self, total_saving: float, total_items: int) -> float:
        v0 = self.design.prior_precision
        b = self.design.item_variance * total_items  # σ² · 累计件数
        # log E = 0.5·log(V0) − 0.5·log(V0+B) + A²/(2(V0+B))
        return 0.5 * math.log(v0) - 0.5 * math.log(v0 + b) + (total_saving ** 2) / (2.0 * (v0 + b))

    def qualified(self) -> bool:
        """e 值是否已越过换版阈值（首次达标即触发）。"""
        return self.log_e >= math.log(self.design.threshold)

    def to_state(self) -> dict:
        return {
            "log_e": self.log_e,
            "total_items": self.total_items,
            "total_saving": self.total_saving,
            "batches_counted": self.batches_counted,
            "e_value": self.e_value,
            "mean_saving": self.mean_saving,
            "threshold": self.design.threshold,
            "qualified": self.qualified(),
        }

    @classmethod
    def from_state(cls, design: TrialDesign, state: dict) -> "EProcess":
        process = cls(design=design)
        process.log_e = float(state["log_e"])
        process.total_items = int(state["total_items"])
        process.total_saving = float(state["total_saving"])
        process.batches_counted = int(state["batches_counted"])
        return process


def projected_items_to_target(design: TrialDesign) -> int | None:
    """功效沟通：在真实每件节省恰为 δ 时，约需多少件才有望触阈（近似）。

    混合 e 过程在 μ=δ 下 log E ≈ δ²·n/(2σ²)（主导项），令其等于 log(1/α)。
    仅作运行前规划参考，不参与判定。
    """
    required_log = math.log(design.threshold)
    denominator = design.target_mean_saving ** 2 / (2.0 * design.item_variance)
    if denominator <= 0:
        return None
    return max(1, math.ceil(required_log / denominator))
