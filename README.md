# 累计水表读数用量复核 API

基于 FastAPI 的用量复核服务：输入一组带秒精度 UTC 时间戳的累计水表读数、计数器模数与每秒最大计数增量，对每段相邻读数判定满足**非负增量**与**速率上限**的整圈回绕次数是否唯一。唯一时返回分段用量与总用量；无解或多解时返回出错区间与原因，**不猜测圈数**。

此外提供**账单总量对账**接口：在同一组非负增量与速率上限约束下，额外接收账单总用量，以整数区间运算收紧各段仍可行的回绕次数范围；只有圈数被唯一确定的段才给出用量，其余段仅返回范围，冲突时给出明确原因。

还提供**每日用量上下界**接口：将每个相邻读数区间按 UTC 午夜切成子段（每段容量为 `floor(速率 × 秒数)`），在区间总增量 `读数差 + k×模数` 可分配为各子段非负整数用量的前提下，给出覆盖时段内**每个 UTC 自然日**的最小/最大可行整数用量。

## 快速启动（Docker，一键）

```bash
docker compose up --build
```

依赖在镜像构建时自动安装，无需本地环境。启动后（默认监听宿主机 **8000** 端口）：

- API 地址：`http://localhost:8000`
- 用量复核：`POST http://localhost:8000/v1/usage/verify`
- 账单总量对账：`POST http://localhost:8000/v1/usage/reconcile`
- 每日用量上下界：`POST http://localhost:8000/v1/usage/daily-range`
- 交互式文档（Swagger UI）：`http://localhost:8000/docs`
- 健康检查：`http://localhost:8000/health`

### 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| 宿主机端口 | `8000` | 在 `docker-compose.yml` 的 `ports` 中修改，如 `"8080:8000"` |
| 容器内端口 | `8000` | 由 `Dockerfile` 的 `CMD` 指定，一般无需修改 |

无其他必需配置（无数据库、无外部依赖）。

## 本地运行（可选）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt
pytest
```

## API 说明

### `POST /v1/usage/verify`

**请求体**

```json
{
  "modulus": 1000,
  "max_rate_per_second": 20,
  "readings": [
    { "timestamp": "2026-09-24T00:00:00Z", "value": 900 },
    { "timestamp": "2026-09-24T00:00:10Z", "value": 100 }
  ]
}
```

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `modulus` | 整数 | `>= 1`，计数器模数（回绕周期） |
| `max_rate_per_second` | 数值 | `>= 0`，每秒最大计数增量，支持小数（按精确分数计算，无浮点误差） |
| `readings` | 数组 | 至少 2 条；`timestamp` 为秒精度且带时区（建议 UTC，其他时区会换算为 UTC），全序列**严格递增**；`value` 为整数且 `0 <= value < modulus` |

**响应（全部区间唯一，`200`）**

```json
{
  "status": "ok",
  "modulus": 1000,
  "segments": [
    {
      "index": 0,
      "start_timestamp": "2026-09-24T00:00:00Z",
      "end_timestamp": "2026-09-24T00:00:10Z",
      "start_reading": 900,
      "end_reading": 100,
      "elapsed_seconds": 10,
      "wraps": 1,
      "usage": 200
    }
  ],
  "errors": [],
  "total_usage": 200
}
```

**响应（存在出错区间，`200`）**

```json
{
  "status": "error",
  "modulus": 1000,
  "segments": [],
  "errors": [
    {
      "index": 0,
      "start_timestamp": "2026-09-24T00:00:00Z",
      "end_timestamp": "2026-09-24T00:16:40Z",
      "start_reading": 100,
      "end_reading": 200,
      "elapsed_seconds": 1000,
      "reason": "ambiguous",
      "detail": "多解：回绕次数 k 在 [0, 9] 内均满足非负增量与速率上限约束，无法唯一确定用量，不做猜测",
      "min_wraps": 0,
      "max_wraps": 9
    }
  ],
  "total_usage": null
}
```

- `reason = "no_solution"`：没有任何回绕次数同时满足两条约束（通常是增量超过速率上限）。
- `reason = "ambiguous"`：存在多个可行回绕次数，无法唯一确定用量。
- 存在出错区间时，`segments` 仍返回其余唯一判定的区间，但 `total_usage` 为 `null`。

**请求校验失败（`422`）**：时间戳未严格递增、缺时区、含亚秒、读数越界、参数非法等，返回 FastAPI 标准错误明细。

### `POST /v1/usage/reconcile`

账单总量对账。请求体在复核接口字段基础上增加 `total_usage`（非负整数账单总用量），读数、模数、速率等校验与 `/v1/usage/verify` 完全一致。

**请求体**

```json
{
  "modulus": 1000,
  "max_rate_per_second": 10,
  "total_usage": 5000,
  "readings": [
    { "timestamp": "2026-09-24T00:00:00Z", "value": 100 },
    { "timestamp": "2026-09-24T00:16:40Z", "value": 200 },
    { "timestamp": "2026-09-24T00:33:20Z", "value": 100 }
  ]
}
```

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `total_usage` | 整数 | `>= 0`，账单总用量（非负整数） |
| 其余字段 | — | 与 `/v1/usage/verify` 相同 |

**响应（账单可行，`200`，`status = "ok"`）**

```json
{
  "status": "ok",
  "modulus": 1000,
  "total_usage": 5000,
  "total_wraps": 5,
  "segments": [
    {
      "index": 0,
      "start_timestamp": "2026-09-24T00:00:00Z",
      "end_timestamp": "2026-09-24T00:16:40Z",
      "start_reading": 100,
      "end_reading": 200,
      "elapsed_seconds": 1000,
      "min_wraps": 0,
      "max_wraps": 4,
      "wraps": null,
      "usage": null,
      "min_usage": 100,
      "max_usage": 4100
    },
    {
      "index": 1,
      "start_timestamp": "2026-09-24T00:16:40Z",
      "end_timestamp": "2026-09-24T00:33:20Z",
      "start_reading": 200,
      "end_reading": 100,
      "elapsed_seconds": 1000,
      "min_wraps": 1,
      "max_wraps": 5,
      "wraps": null,
      "usage": null,
      "min_usage": 900,
      "max_usage": 4900
    }
  ],
  "conflicts": []
}
```

当某段收紧后圈数唯一（`min_wraps == max_wraps`），`wraps` 与 `usage` 给出确定值，`min_usage` / `max_usage` 为 `null`；仍有多解的段反之，**只给范围不猜测**。所有段确定用量之和恒等于 `total_usage`。

**响应（冲突，`200`，`status = "conflict"`）**

分段原本无解时：

```json
{
  "status": "conflict",
  "modulus": 1000,
  "total_usage": 300,
  "total_wraps": null,
  "segments": [],
  "conflicts": [
    {
      "kind": "segment",
      "index": 1,
      "start_timestamp": "2026-09-24T00:00:10Z",
      "end_timestamp": "2026-09-24T00:00:11Z",
      "start_reading": 300,
      "end_reading": 900,
      "elapsed_seconds": 1,
      "reason": "no_solution",
      "detail": "无解：最可能增量 600 已超出该区间 1s 内允许的最大增量 20（k 需同时满足 k>=0 与 k<=-1，区间为空）",
      "min_wraps": 0,
      "max_wraps": -1
    }
  ]
}
```

账单总量不相容或超界时（`kind = "total"`）：

```json
{
  "kind": "total",
  "reason": "total_incompatible_modulus",
  "detail": "账单总量与模数不相容：总量 B 必须满足 B ≡ 0 (mod 1000)（首尾读数差为 0），收到 5500（余数 500），总回绕次数不是整数",
  "feasible_min_total": 1000,
  "feasible_max_total": 19000,
  "expected_remainder": 0,
  "total_wraps": null
}
```

冲突原因：

- `kind = "segment"`，`reason = "no_solution"`：某段在非负增量/速率上限下原本无解（复用复核接口的逐段判定）。
- `kind = "total"`，`reason = "total_incompatible_modulus"`：账单总量与首尾读数差模 `modulus` 不同余，总回绕次数不是整数；`expected_remainder` 给出应有的余数。
- `kind = "total"`，`reason = "total_out_of_range"`：总回绕次数超出各段可行圈数之和 `[Σk_min, Σk_max]`，即可行账单总量不在 `[feasible_min_total, feasible_max_total]` 内。

存在分段无解冲突时不再进行总量推断；总量冲突时 `segments` 为空。`total_usage` 为负数或非整数等请求校验错误返回 `422`。

### `POST /v1/usage/daily-range`

按 UTC 自然日给出每日最小/最大可行整数用量。请求体与 `/v1/usage/verify` 完全相同（读数、模数、最大每秒速率的校验一致，无额外字段）。

**请求体**

```json
{
  "modulus": 1000,
  "max_rate_per_second": 100,
  "readings": [
    { "timestamp": "2026-09-24T23:59:55Z", "value": 800 },
    { "timestamp": "2026-09-25T00:00:05Z", "value": 50 }
  ]
}
```

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| 字段同 `/v1/usage/verify` | — | `modulus` / `max_rate_per_second` / `readings` 的类型与校验完全复用 |

**响应（可行，`200`，`status = "ok"`）**

```json
{
  "status": "ok",
  "modulus": 1000,
  "days": [
    { "date": "2026-09-24", "min_usage": 0, "max_usage": 250, "segment_count": 1 },
    { "date": "2026-09-25", "min_usage": 0, "max_usage": 250, "segment_count": 1 }
  ],
  "boundary_note": "每个 UTC 自然日的 min_usage/max_usage 均可由某个可行的回绕次数及子段整数分配分别达到；不同日期（以及同一读数区间内各子段）的上界不保证存在同一个分配同时成立，请勿将各日上界相加后作为整体上界使用",
  "conflicts": []
}
```

上例中读数区间跨 UTC 午夜，切成各 5 秒的两个子段（容量各为 `floor(100 × 5) = 500`）；区间增量为 `(50 - 800) + 1×1000 = 250`，可在 `(250,0)` 到 `(0,250)` 间任意整数分配，故每日界为 `[0, 250]`。

- `days`：覆盖时段（首读数到末读数之间）实际被覆盖到的每个 UTC 自然日一行，按日期排序。
- `min_usage` / `max_usage`：当日所有读数区间落在该日的子段用量下界/上界之和，均为非负整数。
- `segment_count`：当日覆盖的读数区间子段数（一个读数区间在一个自然日内至多贡献一个子段）。
- `boundary_note`：**边界独立性说明**——每日的下界和上界都可分别由某个可行 `k` 及子段分配达到；但不同日期的上界（下界亦然）不保证由同一个分配同时满足。例如两个容量 5 的子段承担总量 8，每日界为 `[3, 5]`，可行分配只有 `(3,5)`、`(4,4)`、`(5,3)`，两日不可能同时取 5。

**响应（存在区间无可行分配，`200`，`status = "conflict"`）**

相邻读数跨日时，各子段容量**分别**取整为 `floor(速率 × 秒数)`，其和可能小于整段直接计算的容量。若连满足非负增量的最小总增量都无法容纳，则该区间不存在可行分配；此时返回区间索引与冲突原因，**不返回日用量**：

```json
{
  "status": "conflict",
  "modulus": 1000,
  "days": [],
  "boundary_note": "……",
  "conflicts": [
    {
      "index": 0,
      "start_timestamp": "2026-09-24T23:59:55Z",
      "end_timestamp": "2026-09-25T00:00:05Z",
      "start_reading": 950,
      "end_reading": 100,
      "elapsed_seconds": 10,
      "reason": "no_feasible_allocation",
      "detail": "无可行分配：该区间按 UTC 午夜切成 2 段，各子段容量 floor(速率×秒数) 之和为 10，而满足非负增量的最小总增量已为 150（读数差 -850 + 1×1000）；回绕次数需同时满足 k>=1 与 k<=0，区间为空，无法把整数增量分配到各日子段",
      "min_wraps": 1,
      "max_wraps": 0,
      "total_capacity": 10
    }
  ]
}
```

- `reason = "no_feasible_allocation"`：该区间不存在可行的回绕次数 `k >= 0`，使总增量 `读数差 + k×modulus` 不超过各日子段容量之和（且为非负整数）。
- `total_capacity`：该区间各 UTC 日子段容量之和；`min_wraps` 为非负增量要求的最小圈数，`max_wraps` 为子段容量允许的最大圈数（小于 `min_wraps` 即无解）。
- 多个区间同时冲突时一并列出；只要存在冲突，`days` 即为空。请求校验错误（缺时区、亚秒、不递增、读数越界等）仍返回 `422`。

## 判定规则

对每段相邻读数 `(r1, t1) -> (r2, t2)`，真实增量为 `delta = (r2 - r1) + k * modulus`，其中 `k >= 0` 为整圈回绕次数。约束：

1. 非负增量：`delta >= 0`
2. 速率上限：`delta <= max_rate_per_second * (t2 - t1)`

可行 `k` 构成连续整数区间 `[k_min, k_max]`：

- `k_max < k_min` → 无解（`no_solution`）
- `k_max == k_min` → 唯一解，取该 `k` 计算用量
- `k_max > k_min` → 多解（`ambiguous`），不猜测圈数

### 账单总量对账

设账单总用量为 `B`。各段增量望远镜相消，总量恒等式为：

```
B = Σ delta_i = (r_last - r_first) + modulus * K
```

其中 `K = Σ k_i` 为整条序列的回绕次数总和。因此：

1. `B - (r_last - r_first)` 必须为 `modulus` 的非负整数倍，否则账单总量与模数不相容（`total_incompatible_modulus`）；
2. `K` 必须落在 `[Σ k_min, Σ k_max]` 内，否则超出可行总量（`total_out_of_range`，可行总量区间随之给出）。

固定 `K` 后，由「其余各段圈数之和 = `K - k_i`」的区间和直接反推每段仍可行的圈数范围（**整数区间运算，不枚举组合**）：

```
lo_i' = max(k_min_i, K - Σ_{j≠i} k_max_j)
hi_i' = min(k_max_i, K - Σ_{j≠i} k_min_j)
```

`lo_i' == hi_i'` 的段圈数唯一、确定用量；其余段返回 `[lo_i', hi_i']` 及对应用量范围，不做猜测。

### 每日用量上下界

对读数区间 `(r1, t1) -> (r2, t2)`，按 UTC 午夜切成子段，第 `j` 子段时长 `s_j` 秒、容量

```
c_j = floor(max_rate_per_second × s_j)
```

区间真实增量仍为 `delta = (r2 - r1) + k × modulus`，其中 `k >= 0` 为整数。记 `C = Σ c_j`，则增量能以非负整数分配到各子段（`0 <= x_j <= c_j`，`Σ x_j = delta`）当且仅当 `delta <= C`：容量均为整数，`[0, C]` 内任意整数总量都可逐子段填满。故可行圈数为连续整数区间：

```
k_min = ceil(max(0, r1 - r2) / modulus)
k_max = floor((C - (r2 - r1)) / modulus)
```

`k_max < k_min` 即无可行分配（`no_feasible_allocation`）。注意切日分别取整后 `C` 可能小于 `floor(速率 × 整段秒数)`，因此整段速率判定可行的区间仍可能在此冲突。

对固定子段 `j`，记其余子段容量之和为 `C_{-j} = C - c_j`，给定可行 `k` 时其边际范围为

```
max(0, delta - C_{-j}) <= x_j <= min(c_j, delta)
```

界随 `delta`（即随 `k`）单调增，故子段的精确下界在 `k_min` 处取得、精确上界在 `k_max` 处取得，且每个端点都确实存在完整分配达到：

```
x_j 下界 = max(0, (r2-r1) + k_min×M - C_{-j})
x_j 上界 = min(c_j,    (r2-r1) + k_max×M)
```

同一天内多个独立读数区间的子段界直接求和，即得该日 `min_usage` / `max_usage`。各读数区间互不耦合，故某日的日界可由各区间独立选取分配而达到；但同一区间内不同子段（分属不同日）的极值一般互斥，所以**每日边界可分别达到，不保证各日上界（或下界）在同一分配下同时成立**，响应的 `boundary_note` 对此明确注明。

## 项目结构

```
app/
  main.py   # FastAPI 应用、请求/响应模型与校验
  core.py   # 回绕次数判定、总量对账与跨日子段界（Fraction 精确计算）
tests/
  test_api.py
Dockerfile
docker-compose.yml
requirements.txt        # 运行时依赖
requirements-dev.txt    # 测试依赖
```
