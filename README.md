# 自来水厂取样合规复核服务（Waterworks Sampling Compliance）

Python 3.11 + FastAPI + SQLite 实现。维护各供水分区的**必测指标**及指标限值的
**左闭右开**（`[valid_from, valid_to)`）生效区间；登记样本（分区 + 采样时刻），
对非负读数做乐观锁录入/更正（更正请求号幂等）；读数齐全且每项指标在采样时刻
**恰有一条**生效限值时方可发布，**任一超限即不合格**。发布保存读数、限值依据与
结论快照；同一读数版本不得重复发布，更正后再发布产生**新版本报告**且旧版保留可查；
发布事务化，并发发布只准一个成功、失败不留部分报告；并可按分区与采样时间范围
（左闭右开、仅取每个样本最新已发布报告）查询合格/不合格与按指标的超限汇总。
质控人员调整限值前，还可在采样时间窗口内以**拟议非负上下限只读重判**目标指标，
基于已发布报告的冻结读数与其他指标原判定模拟结论变化（不读当前读数、不改限值
与历史报告；快照未含目标指标的报告列为不可评估）。

---

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build -d        # 构建并后台启动；首次启动自动建表+写入种子主数据
curl http://localhost:8080/health   # {"status":"ok"}
docker compose logs -f waterworks   # 查看日志
docker compose down                 # 停止（数据保留在命名卷 waterworks-data）
docker compose down -v              # 停止并删除数据卷（清空数据）
```

- 服务地址：`http://localhost:8080`
- Swagger 交互文档：`http://localhost:8080/docs`
- OpenAPI JSON：`http://localhost:8080/openapi.json`

仅用 Dockerfile 直接运行：

```bash
docker build -t waterworks-compliance:latest .
docker run -d --name waterworks-api -p 8080:8080 \
  -v waterworks-data:/data waterworks-compliance:latest
```

## 2. 本地运行（无 Docker）

需要 Python 3.11+：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 可选：手工初始化（应用启动时也会自动执行）
WATERWORKS_DB=./data/waterworks.db python scripts/init_db.py

# 启动
WATERWORKS_DB=./data/waterworks.db uvicorn app.main:app --host 0.0.0.0 --port 8080
# 或 python -m app.main
```

## 3. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WATERWORKS_DB` | `/data/waterworks.db`（容器）/ 本地建议 `./data/waterworks.db` | SQLite 文件路径，目录自动创建 |
| `SEED_ON_STARTUP` | `true` | 启动时是否幂等写入演示主数据（3 指标、P1/P2 分区及 2025 年限值）；`false` 时只建表 |
| `APP_HOST` | `0.0.0.0` | `python -m app.main` 直接启动时的监听地址 |
| `APP_PORT` | `8080` | 监听端口 |

Compose 中通过 `ports: "8080:8080"` 映射宿主机端口；改为如 `"18080:8080"` 即可换宿主机端口。
数据持久化在命名卷 `waterworks-data -> /data`。

### 种子数据

| 分区 | 必测指标 | 限值（2025-01-01T00:00Z ~ 2026-01-01T00:00Z，左闭右开） |
| --- | --- | --- |
| P1 出厂水一号分区 | `turbidity` 浑浊度 | `< 1.0` NTU（max 开端） |
| | `ph` | `[6.5, 8.5)` |
| | `residual_chlorine` 游离余氯 | `< 4.0` mg/L |
| P2 管网水二号分区 | `turbidity` | `< 1.0` NTU |
| | `ph` | `[6.5, 8.5)` |
| | `residual_chlorine` | `[0.05, 4.0)` mg/L（min 闭端） |

示例采样时刻使用 `2025-06-01T08:00:00+08:00`（归一化 UTC 为 `2025-06-01T00:00:00Z`）。

## 4. HTTP API 一览

所有请求/响应均为 JSON。错误统一形如：

```json
{"error": {"code": "version_conflict", "message": "...", "details": {...}}}
```

### 主数据维护

| 方法 | 路径 | 说明 / 状态码 |
| --- | --- | --- |
| POST | `/admin/indicators` | 新建指标 `{code,name,unit}`；重复 `409 indicator_exists` |
| GET | `/admin/indicators` | 指标列表 |
| POST | `/admin/partitions` | 新建分区 `{code,name}`；重复 `409` |
| GET | `/admin/partitions` | 分区列表 |
| GET | `/admin/partitions/{partition}` | 分区详情：必测指标 + 全部限值 |
| POST | `/admin/partitions/{partition}/mandatory-indicators` | 增加必测指标 `{indicator_code}` |
| POST | `/admin/partitions/{partition}/limits` | 新增限值区间，见下；重叠 `422 limit_interval_overlap` |

限值请求体：

```json
{
  "indicator_code": "turbidity",
  "min_value": null,
  "max_value": 1.0,
  "valid_from": "2025-01-01T00:00:00Z",
  "valid_to":   "2026-01-01T00:00:00Z"
}
```

- `min_value` 省略 = 仅上限；`max_value` 省略 = 仅下限；二者至少一个；若都给则必须 `min < max`。
- 时间**必须带时区**；`valid_from >= valid_to` 为无效区间，直接 422。
- 与同分区同指标既有区间**重叠即拒绝**（端点相接，如旧区间右端 = 新区间左端，允许）。
- 只能给该分区的必测指标定义限值（否则 `422 not_mandatory_indicator`）。

### 样本与读数

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/samples` | 登记 `{partition_code, sampled_at}`，返回样本 id，读数版本初始为 0 |
| GET | `/samples/{sample}` | 当前读数、读数版本、已发布报告摘要 |
| PUT | `/samples/{sample}/readings/{indicator}` | 录入/更正读数，见下 |
| POST | `/samples/{sample}/publish` | 发布合规报告 `{expected_version}` |
| GET | `/samples/{sample}/reports` | 报告版本列表 |
| GET | `/samples/{sample}/reports/{report_id}` | 报告快照（读数+限值依据+结论） |
| GET | `/partitions/{partition}/compliance-summary` | **按分区 + 采样时间窗口汇总**，见下 |
| POST | `/partitions/{partition}/limit-change-preview` | **限值调整影响预览（只读模拟）**，见下 |

读数请求体：

```json
{"indicator_code": "ph", "value": 7.2, "expected_version": 0, "request_no": null}
```

- `value >= 0`，负数 422；只能写本分区必测指标。
- **乐观锁**：`expected_version` 必须等于当前读数版本，否则 `409 version_conflict`
  （响应 details 带回 `current_version`）；每次成功写入版本 +1。
- **录入**（该指标尚无读数）：`request_no` 可省略。
- **更正**（覆盖已有读数）：`request_no` 必填，否则 `422 correction_requires_request_no`。
- **幂等**：`request_no` 全局唯一。同号重放直接返回首次结果（不重复加版本）；
  同号不同请求内容（样本/指标/值/预期版本任一不同）返回 `422 idempotency_key_conflict`。

### 发布规则

`POST /samples/{id}/publish`，body `{"expected_version": N}`：

1. `expected_version` 过期 → `409 version_conflict`；
2. 该读数版本已发布过 → `409 already_published`；
3. 必测指标有缺读数 → `422 readings_incomplete`（details 列出缺失项）；
4. 采样时刻某项无生效限值 → `422 no_effective_limit`；多于一条 → `422 multiple_effective_limits`；
5. 全部满足 → `201`，保存并返回快照：每项读数、是否在范围内、违规原因、
   所依据的限值（id、min/max、生效区间），以及总结论 `qualified / unqualified`。
6. 限值判定：`min <= value < max`（min 闭端、max 开端，与生效区间的左闭右开一致）。
7. 更正读数后版本前进，可再次发布，生成**新 report_id / 新版本快照**；旧报告原样保留。
8. 整个发布在单事务内完成（进程写锁 + `UNIQUE(sample_id, reading_version)` 双重保障）：
   并发发布恰好一个成功，其余 409；任何校验失败都不会留下报告。

发布响应示例：

```json
{
  "report_id": 1,
  "sample_id": "…",
  "partition_code": "P1",
  "sampled_at": "2025-06-01T00:00:00Z",
  "reading_version": 3,
  "conclusion": "unqualified",
  "qualified": false,
  "items": [
    {
      "indicator_code": "turbidity",
      "value": 1.0,
      "in_range": false,
      "violations": ["value 1 >= max limit 1 (max is exclusive)"],
      "limit_basis": {
        "limit_id": 1,
        "min_value": null,
        "max_value": 1.0,
        "valid_from": "2025-01-01T00:00:00Z",
        "valid_to": "2026-01-01T00:00:00Z"
      }
    }
  ],
  "violations": [{"indicator_code": "turbidity", "reasons": ["…"]}],
  "published_at": "2026-10-06T02:36:02.048498Z"
}
```

### 合规汇总（按分区 + 采样时间范围）

`GET /partitions/{partition}/compliance-summary?start_at=...&end_at=...`

- 查询参数 `start_at` / `end_at` **必须带时区**（如 `2025-06-01T08:00:00+08:00` 或
  `2025-06-01T00:00:00Z`），服务端归一化为 UTC。
- 样本按 `sampled_at` 落入**左闭右开**窗口 `[start_at, end_at)` 筛选
  （起点时刻计入、终点时刻排除）。
- 窗口内**每个样本只采用其最新一条已发布报告**（`reading_version` 最大者）；
  历史版本不重复统计；**未发布样本不计入**。
- 返回：纳入统计的报告样本总数、合格数、不合格数，以及**按指标统计的超限次数**
  （指标在某份被采用的快照中出现违规即计一次）；无报告时各计数为 0，
  `indicator_violations` 按该分区当前必测指标给出零基线。
- `start_at >= end_at` -> `422 invalid_time_range`；分区不存在 ->
  `404 partition_not_found`；时间不带时区/缺参数 -> `422 validation_error`，
  均使用统一错误信封。

```bash
curl -sS --get "http://localhost:8080/partitions/P1/compliance-summary" \
  --data-urlencode "start_at=2025-06-01T08:00:00+08:00" \
  --data-urlencode "end_at=2025-07-01T08:00:00+08:00"
```

```json
{
  "partition_code": "P1",
  "start_at": "2025-06-01T00:00:00Z",
  "end_at": "2025-07-01T00:00:00Z",
  "total_reported_samples": 2,
  "qualified_count": 1,
  "unqualified_count": 1,
  "indicator_violations": {"ph": 1, "residual_chlorine": 0, "turbidity": 0}
}
```

错误示例（起点不早于终点）：

```json
{
  "error": {
    "code": "invalid_time_range",
    "message": "start_at must be strictly earlier than end_at (half-open [start_at, end_at))",
    "details": {"start_at": "2025-07-01T00:00:00Z", "end_at": "2025-06-01T00:00:00Z"}
  }
}
```

### 限值调整影响预览（只读模拟，不改动任何数据）

`POST /partitions/{partition}/limit-change-preview`

质控人员正式调整某指标限值前，按采样时间窗口预演结论影响。服务端**只读取已发布
报告的冻结快照**，不读取样本当前读数，不修改限值、历史报告或任何主数据。

请求体：

```json
{
  "indicator_code": "turbidity",
  "start_at": "2025-06-01T00:00:00Z",
  "end_at":   "2025-07-01T00:00:00Z",
  "min_value": null,
  "max_value": 0.5
}
```

- 窗口 `[start_at, end_at)` **左闭右开**，时间**必须带时区**（服务端归一化 UTC）；
  `start_at >= end_at` -> `422 invalid_time_range`，不带时区/缺字段 -> `422 validation_error`。
- `min_value` / `max_value` 为**拟议非负上下限**，均须为有限非负数；可只给一个界，
  至少给一个（都不给 -> `422 invalid_limit`）；双界时必须 `min_value < max_value`
  （`422 invalid_limit`）。判定语义与正式限值一致：**下限包含、上限不包含**
  （`min <= value < max`）。
- 分区不存在 -> `404 partition_not_found`；指标不存在 -> `404 indicator_not_found`；
  指标不是该分区必测项 -> `422 not_mandatory_indicator`。

模拟规则：

1. 窗口内**每个样本只取其最新一条已发布报告**的冻结读数与原结论
   （`reading_version` 最大者；未发布样本不出现）。
2. 报告快照**未包含目标指标项**（如该指标在报告发布之后才成为必测项）-> 列入
   `not_evaluable_samples`（带样本标识、采样时刻、报告 id/版本与原因
   `indicator_not_in_report`），**不参与前后合格数**。
3. 对可评估报告，目标指标用**拟议限值**重判，其他指标**沿用原快照判定**；
   合成 `simulated_conclusion`（任一指标违规即 `unqualified`）。
4. 返回可评估样本数、不可评估样本标识、原/模拟合格数（同时给对应不合格数），
   以及结论发生变化样本的标识与前后结论（附冻结读数、目标指标前后是否在范围内
   与模拟违规原因）。窗口内无已发布样本时，计数全为 0、列表为空。

```bash
curl -sS -X POST "http://localhost:8080/partitions/P1/limit-change-preview" \
  -H 'Content-Type: application/json' \
  -d '{"indicator_code":"turbidity",
       "start_at":"2025-06-01T00:00:00Z","end_at":"2025-07-01T00:00:00Z",
       "max_value":0.5}'
```

```json
{
  "partition_code": "P1",
  "indicator_code": "turbidity",
  "start_at": "2025-06-01T00:00:00Z",
  "end_at": "2025-07-01T00:00:00Z",
  "proposed_limit": {"min_value": null, "max_value": 0.5},
  "evaluable_samples": 2,
  "not_evaluable_samples": [],
  "original_qualified_count": 2,
  "original_unqualified_count": 0,
  "simulated_qualified_count": 1,
  "simulated_unqualified_count": 1,
  "changed_samples": [
    {
      "sample_id": "…",
      "sampled_at": "2025-06-01T00:00:00Z",
      "report_id": 12,
      "reading_version": 3,
      "indicator_code": "turbidity",
      "frozen_value": 0.8,
      "original_conclusion": "qualified",
      "simulated_conclusion": "unqualified",
      "target_indicator": {
        "in_range_original": true,
        "in_range_simulated": false,
        "simulated_violations": ["value 0.8 >= max limit 0.5 (max is exclusive)"]
      }
    }
  ]
}
```

不可评估列表示例（指标在报告发布后才加入必测项）：

```json
"not_evaluable_samples": [
  {
    "sample_id": "…",
    "sampled_at": "2025-06-01T00:00:00Z",
    "report_id": 7,
    "reading_version": 1,
    "reason": "indicator_not_in_report"
  }
]
```

## 5. 调用示例

服务启动后任选其一：

```bash
# curl 版（仅依赖 bash/curl/python3 解析 JSON），覆盖全部正常与拒绝路径；
# 第 11 步演示合规汇总；第 12 步演示限值调整影响预览
# （放宽/收紧翻转、不可评估、非法限值/时间窗/必测项等拒绝路径）
bash examples/api_examples.sh

# Python 版（仅标准库，自带断言，无需安装第三方包）；
# 末尾演示合规汇总与限值调整预览的正常路径、不可评估与各类拒绝
python examples/demo_client.py

# 指定地址
BASE=http://127.0.0.1:8080 python examples/demo_client.py
```

## 6. 测试

```bash
pip install pytest httpx
pytest -q
```

41 个用例覆盖：区间重叠/相接/非法区间、必测成员约束、非负校验、版本冲突、
更正请求号缺失/重放/同号异内容、读数不齐、无生效限值、左闭右开边界、
min 闭/max 开判定、重复发布、更正后新旧版本并存、5 线程并发发布仅一个成功、
发布失败不留报告；以及合规汇总的零基线、仅最新报告计入、未发布排除、
左闭右开窗口与时区归一、分区间隔离、按指标超限计数、非法区间/缺分区错误；
限值调整预览的零计数空列表、放宽/收紧翻转、其他指标原违规阻断翻转、
上限开端/下限包含边界、仅取最新冻结快照（忽略未发布的当前读数）、
不可评估样本、单界/双界与零界、左闭右开窗口与时区归一、未发布排除，
以及分区/指标不存在、非必测、时间窗、限值、无时区、负值/非有限值等拒绝路径。

## 7. 项目结构

```
app/
  config.py            # 环境变量配置
  database.py          # SQLite 连接/Schema/读写事务（进程写锁 + WAL）
  timeutil.py          # 时区强制与 UTC 归一化
  errors.py            # 统一业务异常（404/409/422）
  schemas.py           # Pydantic 请求模型
  crud.py              # 核心业务规则（重叠判定/乐观锁/幂等/发布快照）
  seed.py              # 幂等种子主数据
  main.py              # FastAPI 入口、异常处理、lifespan 初始化
  routers/admin.py     # 主数据管理接口
  routers/samples.py   # 样本/读数/发布接口
  routers/summaries.py # 合规汇总查询接口（分区 + 采样时间窗口）
  routers/previews.py  # 限值调整影响预览接口（只读模拟，不改任何数据）
scripts/init_db.py     # 手工初始化脚本
examples/              # curl 与 Python 调用示例
tests/                 # pytest 端到端用例
Dockerfile             # 容器镜像（启动即初始化）
docker-compose.yml     # 一键启动 + 卷持久化 + 健康检查
requirements.txt
```

## 8. 设计要点说明

- **时间**：所有入参时间强制带时区偏移，服务端统一归一化为 UTC 存储与比较；
  库中同时保存 ISO8601 字符串（展示）与毫秒纪元整数（区间比较）。
- **左闭右开**：区间重叠判定 `a.from < b.to && b.from < a.to`，端点相接不算重叠；
  采样时刻命中条件 `valid_from <= t < valid_to`。合格区间 `min <= value < max`。
- **并发**：每线程独立 SQLite 连接，写事务经进程内 `RLock` 串行化并以
  `BEGIN IMMEDIATE` 立即取写锁；发布唯一约束做数据库级兜底，杜绝部分报告。
- **版本**：样本登记后 `reading_version=0`，每次成功写入/更正 +1；发布不改版本，
  报告以读数版本为业务版本，形成可查询、不可变的历史序列。
- **幂等**：更正请求指纹 = `(sample_id, indicator_code, value, expected_version)`
  的规范 JSON，与请求号一起落库；重放返回缓存的原始响应。
- **合规汇总**：SQL 用关联子查询取每个样本 `MAX(reading_version)` 的报告，
  再按 `sampled_at_ms >= start AND sampled_at_ms < end` 左闭右开过滤，
  快照 `violations` 在应用层聚合按指标计数；汇总走只读事务快照，
  未发布样本（reports 中无行）自然被 JOIN 排除。
- **限值调整预览**：纯只读 `read_tx`，复用与汇总相同的“每样本最新已发布报告”
  关联子查询；模拟只基于报告 `snapshot_json` 中冻结的 items/violations——目标指标
  项以拟议界用同一套 `min 闭/max 开` 规则重判，其他指标沿用快照原判定，
  **从不读取 readings 当前值，也不触碰 limits/reports**。快照缺目标指标项即标记
  `indicator_not_in_report` 并排除在前后合格数之外；校验顺序为时间窗 → 限值界 →
  分区/指标/必测成员，全部错误走统一错误信封。
