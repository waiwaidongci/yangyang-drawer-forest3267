# 新旧模型在线试用（顺序换版）

本模块在既有"归档回放"之外，支持把新版分诊模型与当前在用的旧版模型放到同一条产线上
**在线试用**：同批件由两版各自给出预判，末检真值回来后，按同一套误报/漏报代价核算
"新版相对旧版省了多少工"，并用带误换上界保证的顺序统计方法决定是否正式换版。

代码位于 `backend/src/domain/trial/`（零第三方依赖，仅标准库），应用服务在
`backend/src/services/trials.py`，接口在 `backend/src/routes/trials.py`。

## 业务约束到实现的映射

| 业务要求 | 实现 |
| --- | --- |
| 同批件新旧模型先锁定判定，再收末检 | `TrialEngine.lock_batch` 先存两套 0/1 预判；`receive_*` 只能对已锁定件收料，锁定后不可改（`ledger.py`） |
| 按同一套误报漏报代价算每批节省多少 | `costs.py`：FN 计漏报代价、FP 计误报代价；批次 `saving = 旧版代价 − 新版代价`，并给出每件平均节省 |
| 末检乱序到达，后批齐了也等前批，整批按预判顺序入账 | `ReorderLedger` 分批缓冲齐套，`drain_ready` 从 seq=1 起遇到首个未齐套批即停止，严格按预判顺序整批入账 |
| 中断恢复后重试不重复计批 | 引擎状态整体 JSON 落库（`model_trials.engine_state`）；入账与 e 过程只在"首次入账"累加，重复 `drain` 返回空且证据不变 |
| 每批入账后都可凭证据换版；新版平均不省工时误换概率 ≤ 5% | e 过程（`eprocess.py`），阈值 `1/α = 20`；由 Ville 不等式对任意可选停止时刻控制一类错误 |
| 正式导出在首次达标时换版 | `exporter.build_switch_export` 仅在 `qualified` 时生成；`model_trial_exports` 对 trial 唯一约束 + 服务层查重，保证只导出一份、幂等同哈希 |
| 运行结果可查达标批次和各批代价差 | `GET /model-trials/{id}` 返回 `switch`（达标批次）与 `posted_batches`（逐批 old/new/saving）；另有 `batch-differences` 与逐件 `evidence` |

## 5% 误换上界是怎么来的

设第 k 批有 n_k 件、批次总节省 S_k（旧版代价减新版代价，单位等效分钟）。
每件节省 Y 有界：单件代价最大为 `c_max = max(漏报代价, 误报代价)`，故 Y ∈ [−c_max, c_max]，
由 Popoviciu 不等式方差上界 σ² ≤ c_max²（默认即取此保守值，代价 12/3 时 σ² = 144）。

在零假设 H0：每件平均节省 μ ≤ 0（新版不省工）下，把 Y 视为有界方差的次高斯增量，
采用正态混合先验 θ ~ N(0, 1/V0) 构造**乘积型 e 过程**。记累计节省 A = ΣS_j、
累计件数 N = Σn_j、B = σ²N，其闭式为：

```
log E = 0.5·log(V0) − 0.5·log(V0 + B) + A² / (2·(V0 + B))
```

E 在 H0 下是非负上鞅（E[E_k] ≤ 1）。由 **Ville 不等式**，对任意数据驱动的停时 τ：

```
P0( 存在 k ≤ τ 使 E_k ≥ 1/α ) ≤ α，   α = 0.05 → 阈值 20
```

这天然支持"每批入账后都看一眼、达标即停"的可选停止，也支持中断恢复——e 值只依赖
已入账批的 (n, S)，重试不重复计批时证据完全不变。

**关键前提**：σ² 必须是真实每件节省方差的上界。默认用有界性给出的 c_max²，保证成立；
若填更紧的 `item_variance`，必须基于可靠历史数据，且不得超过 c_max²（系统强制校验）。
压力测试 `tools/trial_checks/eprocess_stress.py` 实证：低估方差会使误触率超过 5%，
而覆盖真实方差时上界恢复有效。

实测（`tools/trial_checks/eprocess_montecarlo.py`，固定随机种子）：

- H0 零漂移、20000 条路径、最长 40 批：误触率 0.06% ≤ 5%；
- H0 负漂移（每件 −1）、5000 条路径：误触率 2.4% ≤ 5%；
- H1 每件 +2：全部路径达标，中位仅需 2 批。

## API 工作流

写操作需分析员/负责人（`Writer`），正式换版导出仅负责人（`Admin`），查看接口三角色可用。

1. `POST /api/model-trials`：`{label, settings}` 创建试用。`settings` 含
   `miss_cost`（默认12）、`retest_cost`（默认3）、`target_mean_saving`（默认1.0，仅用于功效沟通）、
   `item_variance`（留空取有界上界）。
2. `POST /api/model-trials/{id}/lock`：`{batch_id, predictions:[{id, old_flag, new_flag}]}`
   锁定一批的新旧预判，返回系统分配的预判顺序号 `batch_seq`。
3. 末检到达（可乱序、可重复幂等）：
   - 整批 `POST /inspections`：`{batch_id, outcomes:[{id, failed}]}`；
   - 单件 `POST /inspection`：`{batch_id, item_id, failed}`。
4. `POST /api/model-trials/{id}/drain`：把当前可入账的齐套批按预判顺序整批入账并更新 e 值，
   返回本次新入账批、当前 e 值、是否达标、待入账批与累计代价。重复调用幂等。
5. `GET /api/model-trials/{id}`：状态、达标批次 `switch`、逐批代价差 `posted_batches`、
   累计节省与 e 过程快照；`GET /batch-differences`、`GET /evidence/{batch_seq}` 单独核对。
6. 达标后 `POST /api/model-trials/{id}/export`（负责人）生成正式换版凭证（仅一份、带 sha256 哈希）；
   未达标可 `POST /end` 维持旧版结束试用。

## 验证方式（本机无需第三方依赖）

```sh
cd backend
python3 -I tests/test_trial_domain.py                       # 21 个域单测
python3 -I ../tools/trial_checks/eprocess_montecarlo.py     # 5% 上界蒙特卡洛
python3 -I ../tools/trial_checks/eprocess_stress.py         # 方差前提压力测试
python3 -I ../tools/trial_checks/service_harness.py         # 服务层+sqlite 持久化/恢复/幂等端到端
```

`service_harness.py` 用标准库 `sqlite3` 提供最小 SQLAlchemy 垫片（仅服务层用到的接口），
加载的是**真实的** `src.services.trials` 与 `src.domain.trial`，用于在未安装后端依赖的环境中
验证落库、中断恢复与重试幂等。
