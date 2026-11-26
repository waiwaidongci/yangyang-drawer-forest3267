"""不同方差规划与超长视野下的误换上界压力测试。"""

import os
import random
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
sys.path.insert(0, BACKEND)

from src.domain.trial.eprocess import EProcess, TrialDesign  # noqa: E402


def simulate(paths, batches, size, draw, design, seed):
    rng = random.Random(seed)
    hits = 0
    expectation_sum = 0.0
    for _ in range(paths):
        process = EProcess(design)
        for _ in range(batches):
            saving = sum(draw(rng) for _ in range(size))
            process.observe(size, saving)
            if process.qualified():
                hits += 1
                break
        expectation_sum += process.e_value
    return hits / paths, expectation_sum / paths


def run():
    # 紧方差规划：历史估计 σ²=9（远小于有界上界 144）。
    tight = TrialDesign.create(target_mean_saving=1.0, max_item_cost=12, item_variance=9.0)
    rng = random.Random(7)
    # 零漂移、真实单件方差恰好 9：±3 对称。
    rate, mean_e = simulate(
        30000, 60, 12, lambda r: r.choice((-3, 3)), tight, seed=11,
    )
    print(f"紧方差 σ²=9, H0 零漂移：误触率 = {rate:.4f} (<=0.05), 末端 E 均值 = {mean_e:.3f}")

    # 默认保守方差、超长视野 200 批（2400 件），零漂移 ±12。
    conservative = TrialDesign.create(target_mean_saving=1.0, max_item_cost=12)
    rate, mean_e = simulate(
        10000, 200, 12, lambda r: r.choice((-12, 12)), conservative, seed=23,
    )
    print(f"保守 σ²=144, H0 200 批长视野：误触率 = {rate:.4f} (<=0.05), 末端 E 均值 = {mean_e:.3f}")

    # 真实分布比规划更嘈杂（规划 9，实际 ±5 → 方差 25）会破坏上界：
    # 这是"错误低估方差"的反面教材，证明默认必须保守、自定义须有历史依据。
    bad_rate, _ = simulate(
        10000, 60, 12, lambda r: r.choice((-5, 5)), tight, seed=31,
    )
    print(f"[反面教材] 规划 σ²=9 但实际 σ²=25（低估方差）：误触率 = {bad_rate:.4f}，"
          "故系统默认采用有界上界 144，自定义更紧值须基于可靠历史方差")

    # 正确做法：规划 σ²=25 覆盖真实 σ²=25，上界恢复有效。
    honest = TrialDesign.create(target_mean_saving=1.0, max_item_cost=12, item_variance=25.0)
    good_rate, _ = simulate(
        10000, 60, 12, lambda r: r.choice((-5, 5)), honest, seed=37,
    )
    print(f"[正确用法] 规划 σ²=25 覆盖实际 σ²=25：误触率 = {good_rate:.4f} (<=0.05)")

    # 确定性零节省（每件 0）：永不达标。
    process = EProcess(conservative)
    ever = any(
        (process.observe(12, 0), process.qualified())[1] for _ in range(50)
    )
    print("每件节省恒为 0：是否误触 =", ever, " 末端 e =", round(process.e_value, 6))


if __name__ == "__main__":
    run()
