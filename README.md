# 12-ladderbill（阶梯电费）

Ladderbill — 居民阶梯电价分段累进（含尖峰系数）

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:4100 |
| API | http://localhost:9100 |

## 种子与健康门禁

起服后可重复执行的检查入口，断言：

1. 库中至少存在一户 **dirty**（种子名含“种子”的偏高户）与一户 **clean**（正常户）；
2. 默认三档阶梯单价齐全，且 `up_to` 严格单调递增（末档允许 `NULL` 开口）；
3. `/api/health` 返回 `ok == true` 且 `project == "ladderbill"`。

### 调用方式

```bash
# 起服（后台）
docker compose up --build -d

# 方式一：独立模块命令（在 backend 容器内执行）
docker compose exec backend python -m app.gate

# 方式二：pytest 用例（含全部异常注入场景）
docker compose exec backend pytest app/tests
```

门禁只读、可重复执行。通过时退出码为 0，stdout 输出缺项列表为空：

```json
{"ok": true, "missing": []}
```

任一断言不通过时**退出码非零**，并在 stderr 逐项打印缺项名称（不是笼统的“失败”），例如：

```
missing: account.dirty
missing: health.reachable
gate failed, missing items: account.dirty, health.reachable
```

缺项名称固定如下：

| 缺项名称 | 含义 |
| --- | --- |
| `account.dirty` | 缺少种子偏高户（dirty） |
| `account.clean` | 缺少正常户（clean） |
| `tiers.three_bands` | 三档阶梯不齐全（或单价为空） |
| `tiers.up_to_monotonic` | 阶梯 `up_to` 非单调递增 |
| `health.reachable` | 健康接口不可达（服务未启动/已停止） |
| `health.ok` | 健康接口 `ok` 不为 `true` |
| `health.project` | 项目标识不等于 `ladderbill` |

目标地址默认 `http://localhost:9100`，可用 `--base-url` 或环境变量 `BASE_URL` 覆盖。

### 异常注入与恢复验证

门禁不会通过改动计费种子或删减断言项来“假绿”，以下两种注入均可稳定复现失败：

```bash
# 注入一：删除 dirty 户 -> 退出码非零，stderr 出现 missing: account.dirty
docker compose exec backend python -c "import sqlite3,os; \
c=sqlite3.connect(os.environ['DATA_DIR']+'/app.db'); \
c.execute(\"DELETE FROM accounts WHERE name LIKE '%种子%'\"); c.commit()"
docker compose exec backend python -m app.gate; echo "exit=$?"

# 恢复种子：按原种子值补回该户（不改动任何计费数值）
docker compose exec backend python -c "import sqlite3,os; \
c=sqlite3.connect(os.environ['DATA_DIR']+'/app.db'); \
c.execute(\"INSERT INTO accounts(id,name,meter_no,note) VALUES (2,'李家(种子偏高)','M-1002','对照：高用量+尖峰')\"); c.commit()"
docker compose exec backend python -m app.gate; echo "exit=$?"   # 0

# 注入二：停服务 -> 退出码非零，stderr 出现 missing: health.reachable
# （backend 已停，用 run 起一个共享数据卷的一次性容器来执行门禁）
docker compose stop backend
docker compose run --rm backend python -m app.gate; echo "exit=$?"

# 恢复服务后连续两次执行，退出码均为 0、缺项列表均为空
docker compose up -d
docker compose exec backend python -m app.gate; echo "exit=$?"   # 0
docker compose exec backend python -m app.gate; echo "exit=$?"   # 0
```

需要完整重置种子时（例如删改了多张表），可重建数据卷，重新执行未改动的种子脚本：

```bash
docker compose down -v && docker compose up --build -d
```

## 主链

抄表录入 → 阶梯分段计费 → 账单明细

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。
