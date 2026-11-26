# 展柜抽屉滑轨返测分诊

小型传统机器学习后端。分组留出随机森林、分类误差和阈值。提供台账校验与导入、参数校验与同步计算、结果复核和 CSV 导出。完整业务口径见 [BUSINESS.md](BUSINESS.md)。

## 本地运行

```sh
docker compose up -d --build --wait
curl -fsS http://127.0.0.1:19343/api/health
curl -fsS http://127.0.0.1:19343/api/spec
```

API 默认监听 `127.0.0.1:19343`。接口文档在 `/api/docs`，机器可读规范在 `/api/openapi.json`。数据保存在 SQLite 具名卷中，`docker compose down` 后仍可使用。服务首次启动会装载模拟业务数据。

| 本地演示账号 | 口令 | 权限 |
|---|---|---|
| `analyst@demo.local` | `Demo-2611!` | 导入数据、运行计算、核对结果 |
| `admin@demo.local` | `Demo-2611!` | 分析员权限及结果复核 |
| `viewer@demo.local` | `Demo-2611!` | 查阅来源、已批准的结果并导出 CSV |

## API 工作流

1. `POST /api/auth/login` 接收带 `email`、`password` 的 JSON。后续业务请求通过 `Authorization: Bearer <access_token>` 鉴权。
2. `GET /api/spec` 查看 CSV 列、参数和结果口径。`GET /api/datasets` 查找演示台账，或用 `POST /api/datasets` 提交带 `name`、`csv` 的 JSON。
3. `POST /api/runs` 提交 `dataset_id`、`label` 和可选的 `settings`。响应包含运行 ID 及最终状态。`GET /api/runs/{id}` 返回业务指标、表格或失败原因。
4. 负责人调用 `POST /api/runs/{id}/review`，提交 `decision`（`approved` 或 `rejected`）和 `note`。批准后可通过 `GET /api/runs/{id}/export/{table_key}` 下载业务表。表名和字段从运行结果取得。

## 新旧模型在线试用

在归档回放之外，另提供顺序换版试用：同批件先由新旧两版锁定预判，末检乱序回来后按预判顺序整批入账，用同一套误报/漏报代价核算每批节省，并以 e 过程把"新版平均不省工时误换"的概率控制在 5% 以内。口径与接口见 [`backend/src/domain/trial/README.md`](backend/src/domain/trial/README.md)。

1. `POST /api/model-trials` 创建试用（代价、目标每件节省、可选方差上界）。
2. `POST /api/model-trials/{id}/lock` 锁定一批的新旧预判；末检通过 `POST /inspections`（整批）或 `POST /inspection`（单件）乱序、幂等提交。
3. `POST /api/model-trials/{id}/drain` 按预判顺序整批入账并更新证据，重复调用不重复计批；`GET /api/model-trials/{id}` 可查达标批次与各批代价差。
4. 首次达标（e 值 ≥ 20）后，负责人 `POST /api/model-trials/{id}/export` 导出唯一的正式换版凭证（带哈希、幂等）；未达标可 `POST /end` 维持旧版。

## 代码与配置

- `backend/src/domain` 实现数据规范、模拟样本和业务算法；`backend/src/domain/trial` 为零第三方依赖的新旧模型顺序换版试用（代价、乱序入账、e 过程与换版导出）。
- `backend/src/services` 与 `backend/src/models` 实现台账、计算、用户权限、复核记录和试用持久化（`trials.py`、`models/trial.py`）。
- `backend/src/routes`、`schemas` 和 `controllers` 提供 FastAPI 接口与输入输出约束。
- `backend/migrations` 和 `backend/scripts` 提供 SQLite 初始化和可重复执行的演示数据装载。

可在 `.env` 中配置 `BACKEND_PORT`、`SEED_DEMO` 和 `JWT_SECRET`。演示账号、默认签名密钥及模拟台账用于本地验证。启用演示数据也可执行 `docker compose exec -T backend python -m scripts.seed_demo`。
