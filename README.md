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

## 主链

抄表录入 → 阶梯分段计费 → 账单明细

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 种子与健康门禁

门禁是一个**可重复执行**的只读检查入口，断言：

1. `health.ok` —— `GET /api/health` 返回的 `ok` 为 `true`；
2. `health.project` —— 同一响应的 `project` 字段等于 `ladderbill`；
3. `account.dirty` —— `GET /api/accounts` 至少存在一户名字含「种子」的 dirty 种子户；
4. `account.clean` —— 至少存在一户名字不含「种子」的 clean 对照户；
5. `tier.count` —— `GET /api/tiers` 默认阶梯恰好三档；
6. `tier.up_to_order` —— 三档按 `sort_order` 的 `up_to` 严格单调递增（180 < 260），末档开放（`up_to` 为空）。

全部通过时退出码为 `0`，并打印 `门禁通过，缺项列表: []`；任一缺项时以**非零退出码**退出，
并把**缺项的稳定名称**逐行打印到 **stderr**（不是笼统失败），随后附上每项的具体原因。
门禁只调用公开 HTTP 接口，不写数据库，不修改任何种子数值。

### 调用方式

先在工程根目录起服：

```bash
docker compose up --build -d        # -d 后台运行，便于另开终端执行检查
```

然后在 `backend/` 目录执行（容器内自带 Python 3.12 依赖；也可在宿主机装了 Python 3.11+ 后直接跑，门禁本身只用标准库）：

方式一：独立模块命令

```bash
# 在运行中的 backend 容器内执行
docker compose exec backend python -m app.gate

# 或在宿主机 backend/ 目录直接执行（无需第三方依赖）
cd backend
python -m app.gate                                  # 默认 http://localhost:9100
python -m app.gate --base-url http://localhost:9100
GATE_BASE_URL=http://localhost:9100 python -m app.gate
```

方式二：pytest 用例

```bash
# 离线单测（用桩数据验证各异常分支，不需要起服）
docker compose exec backend pytest app/tests -m "not live" -q

# 真实服务的端到端门禁（标记 live；服务不可达时该用例自动 skip）
docker compose exec backend pytest app/tests -m live -q

# 全部用例
docker compose exec backend pytest app/tests -q
```

宿主机上等价写法：`cd backend && python -m pytest app/tests -q`。

### 异常注入自测（失败可重现，恢复后回到通过）

- **注入一：删掉 dirty 户。** 进入容器用 sqlite 删除种子户后再跑门禁：

  ```bash
  docker compose exec backend python -c "from app.db import connect; c=connect(); c.execute(\"DELETE FROM accounts WHERE name LIKE '%种子%'\"); c.commit(); c.close()"
  docker compose exec backend python -m app.gate
  # 退出码非零；stderr 的缺项列表包含 account.dirty
  ```

- **注入二：停服务。** backend 容器停掉后无法再 `exec` 进容器，请在宿主机执行：

  ```bash
  docker compose stop backend
  cd backend && python -m app.gate
  # 退出码非零；stderr 缺项列表包含 health.ok / health.project / account.* / tier.*
  cd .. && docker compose start backend
  ```

- **恢复：** 恢复种子最简单的方式是重置数据卷后重新起服（种子只在空库时写入）：

  ```bash
  docker compose down -v && docker compose up --build -d
  ```

  恢复后连续执行两次 `python -m app.gate`，两次退出码均为 `0`、输出的缺项列表均为空 `[]`。
