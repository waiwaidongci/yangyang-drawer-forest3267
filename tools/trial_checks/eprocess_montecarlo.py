"""e 过程性质的蒙特卡洛实证（独立验证脚本，与受测代码分离）。

用 `python3 -I tools/trial_checks/eprocess_montecarlo.py` 运行：
隔离模式下显式把 backend 目录加入 sys.path，不依赖当前工作目录或环境变量。
"""

import os
import random
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
sys.path.insert(0, BACKEND)

from src.domain.trial.eprocess import (  # noqa: E402
    EProcess, TrialDesign, projected_items_to_target,
)


def run():
    design = TrialDesign.create(target_mean_saving=1.0, max_item_cost=12)
    print("默认方差上界 =", design.item_variance, " 换版阈值 =", design.threshold)

    rng = random.Random(20261126)

    # H0：零漂移，每件节省对称 ±12（均值 0、方差 144，正好顶到保守方差上界）。
    null_hits = 0
    null_paths = 20000
    final_values = []
    for _ in range(null_paths):
        process = EProcess(design)
        crossed = False
        for _batch in range(40):  # 40 批 × 12 件 = 480 件
            saving = sum(rng.choice((-12, 12)) for _ in range(12))
            process.observe(12, saving)
            if process.qualified():
                crossed = True
                break
        null_hits += crossed
        final_values.append(process.e_value)
    print(f"H0 零漂移误触率 = {null_hits / null_paths:.4f} (要求 <= 0.05, n={null_paths})")
    print("H0 末端 e 值均值 =", round(sum(final_values) / len(final_values), 4), "(非负上鞅 E<=1 参考)")

    # H0：负漂移（新版平均更费工，每件 -1）。
    neg_hits = 0
    neg_paths = 5000
    for _ in range(neg_paths):
        process = EProcess(design)
        for _batch in range(40):
            saving = sum(rng.choice((-13, 11)) for _ in range(12))
            process.observe(12, saving)
            if process.qualified():
                neg_hits += 1
                break
    print(f"H0 负漂移误触率 = {neg_hits / neg_paths:.4f} (要求 <= 0.05, n={neg_paths})")

    # H1：正漂移每件 +2，应在有限批次内达标。
    hit_at = []
    alt_paths = 2000
    for _ in range(alt_paths):
        process = EProcess(design)
        for batch_index in range(1, 80):
            saving = sum(rng.choice((10, 14)) for _ in range(12))
            process.observe(12, saving)
            if process.qualified():
                hit_at.append(batch_index)
                break
    median_batches = sorted(hit_at)[len(hit_at) // 2] if hit_at else None
    print(f"H1 每件 +2：达标比例 = {len(hit_at) / alt_paths:.3f}，达标路径中位批数 = {median_batches}")
    print("规划参考（δ=1）预计件数 =", projected_items_to_target(TrialDesign.create(1.0, 12)))


if __name__ == "__main__":
    run()
