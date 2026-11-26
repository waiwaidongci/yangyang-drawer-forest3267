"""新旧模型试用的误报/漏报代价口径。

与归档回放业务（``src.domain.policy``）保持一致的代价定义：

* 实际失败却仍进常规队列（漏报 FN）：计 ``missed_failure`` 等效分钟；
* 实际通过却被优先返测（误报 FP）：计 ``unnecessary_retest`` 等效分钟；
* 正确优先返测（TP）与正确常规末检（TN）不产生误差代价。

试用按"每一件"分别核算新旧两套预判，再汇总为批次代价，因此批次大小不同也可比较。
"""

import math
from dataclasses import dataclass

MISS_COST_MIN = RETEST_COST_MIN = 1
MISS_COST_MAX = RETEST_COST_MAX = 100


@dataclass(frozen=True)
class CostPlan:
    """一套固定的误报/漏报代价（等效工时分钟）。"""

    missed_failure: int
    unnecessary_retest: int

    def __post_init__(self):
        for name, value in (("漏报代价", self.missed_failure),
                            ("误报代价", self.unnecessary_retest)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name}须为整数")
            if not MISS_COST_MIN <= value <= MISS_COST_MAX:
                raise ValueError(f"{name}须为 {MISS_COST_MIN} 至 {MISS_COST_MAX} 的整数")

    @classmethod
    def parse(cls, settings: dict) -> "CostPlan":
        if not isinstance(settings, dict):
            raise ValueError("代价设置须为键值对象")
        allowed = {"miss_cost", "retest_cost"}
        unknown = set(settings) - allowed
        if unknown:
            raise ValueError("代价设置包含未知参数：" + ", ".join(sorted(unknown)))
        return cls(_as_int(settings.get("miss_cost", 12), "漏报代价"),
                   _as_int(settings.get("retest_cost", 3), "误报代价"))

    def item_cost(self, predicted_flag: int, actual_failed: int) -> int:
        """单件在某个模型下的误差代价。

        predicted_flag=1 表示该模型预判为优先返测，actual_failed=1 表示末检确认失败。
        """
        if predicted_flag == 1 and actual_failed == 0:
            return self.unnecessary_retest  # 误报 FP
        if predicted_flag == 0 and actual_failed == 1:
            return self.missed_failure      # 漏报 FN
        return 0

    def max_item_cost(self) -> int:
        """单件代价的上界，用于零漂移 e 过程的方差保守取值。"""
        return max(self.missed_failure, self.unnecessary_retest)


def _as_int(raw, label: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError(f"{label}须为 {MISS_COST_MIN} 至 {MISS_COST_MAX} 的整数")
    try:
        number = float(raw)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{label}须为整数") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"{label}须为整数")
    value = int(number)
    if not MISS_COST_MIN <= value <= MISS_COST_MAX:
        raise ValueError(f"{label}须为 {MISS_COST_MIN} 至 {MISS_COST_MAX} 的整数")
    return value


@dataclass(frozen=True)
class ItemResult:
    """单件在新旧两套预判下的对账结果。"""

    item_id: str
    old_flag: int
    new_flag: int
    actual_failed: int
    old_cost: int
    new_cost: int

    @property
    def saving(self) -> int:
        """正数表示新版相对旧版省下的等效分钟。"""
        return self.old_cost - self.new_cost


def score_item(item_id: str, old_flag: int, new_flag: int,
               actual_failed: int, plan: CostPlan) -> ItemResult:
    _check_flag(old_flag, "旧版预判")
    _check_flag(new_flag, "新版预判")
    _check_flag(actual_failed, "末检结果")
    return ItemResult(
        item_id=item_id,
        old_flag=old_flag,
        new_flag=new_flag,
        actual_failed=actual_failed,
        old_cost=plan.item_cost(old_flag, actual_failed),
        new_cost=plan.item_cost(new_flag, actual_failed),
    )


def _check_flag(value, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value not in (0, 1):
        raise ValueError(f"{label}须为 0 或 1")


@dataclass(frozen=True)
class BatchSettlement:
    """整批入账后的代价差与逐件证据。"""

    batch_seq: int
    batch_id: str
    item_count: int
    old_cost: int
    new_cost: int
    saving: int
    items: tuple[ItemResult, ...]

    @property
    def mean_saving(self) -> float:
        """每件平均节省（等效分钟），批次大小不同时仍可横向比较。"""
        return self.saving / self.item_count


def settle_batch(batch_seq: int, batch_id: str, items: list[ItemResult]) -> BatchSettlement:
    if not items:
        raise ValueError("批次至少要包含一件总成才能入账")
    old_cost = sum(item.old_cost for item in items)
    new_cost = sum(item.new_cost for item in items)
    return BatchSettlement(
        batch_seq=batch_seq,
        batch_id=batch_id,
        item_count=len(items),
        old_cost=old_cost,
        new_cost=new_cost,
        saving=old_cost - new_cost,
        items=tuple(items),
    )
