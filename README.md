# 科学计算任务运营服务

这是一个面向科研平台、实验室和计算中心的 Python 后端，使用 FastAPI 与 SQLite 管理参数模板、计算任务提交、优先级排队、工作者领取、取消、失败重试、租约恢复、用户配额、结果版本和管理员人工干预记录。服务同时保留用户、角色、会话和审计等基础能力，所有运行数据都在单个本地数据库文件中，不需要另行部署数据库、缓存、消息队列或浏览器界面。

## 已有能力

- 参数模板：保存参数类型、必填项、数值范围、默认值、最大运行时间和最大尝试次数。
- 任务提交：根据模板校验参数，使用用户与幂等键避免重复创建，并保存项目、提交人和输入摘要。
- 排队领取：按优先级和进入队列的顺序分配任务，工作者可声明算法能力并获得有期限的租约。
- 执行回执：工作者可以续租、提交结果或报告失败；可重试错误使用确定的退避时间重新排队。
- 失败恢复：租约过期后可由恢复入口将任务重新排队，达到最大尝试次数的任务转为失败。
- 配额控制：可保存用户、角色或项目的排队数、运行数和每日提交上限；当前提交路径执行用户配额。
- 结果版本：每次成功回执保存不可变结果、指标摘要和内容摘要，任务指向当前结果版本。
- 人工干预：取消、人工重试、优先级调整和批量操作均保留操作者、原因、前后状态和批次标识。
- 登录与角色：基础管理模块提供管理员初始化、用户、角色、会话和细粒度权限。
- 链路协调：登记地面链路时间片、租户配额与消息优先级，提供预留、释放、抢占、延期、跨片迁移与结算接口，每次决策保留当时的带宽快照。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/compute-operations.db`。可以复制 `.env.example` 并通过 `TOWNSHIP_DATABASE_PATH` 指定其他本地路径。

## 数据库初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动 API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

计算任务摘要位于 `/api/compute/summary`，模板、配额、提交、领取、回执和人工操作接口统一使用 `/api/compute` 前缀。

## 卫星链路带宽协调

链路协调接口统一使用 `/api/link` 前缀，覆盖多颗实验卫星同时回传模型中间结果时的地面链路带宽分配：

- 登记：`POST /timeslices` 登记链路时间片（起止时刻、总带宽 kbps、结算单价），`PUT /quotas` 设置租户在每片的最大带宽与活动预留数，`POST /priority-classes` 登记消息优先级（rank 越大越紧急，`may_preempt` 标记是否允许抢占）。
- 预留：`POST /reservations` 按 `(tenant, message_key)` 幂等预留；同一消息重试返回既有记录、不重复扣减，同键不同参数返回 409。容量或配额不足时创建 `rejected` 记录并保存原因，拒绝是终态，新的尝试需使用新的消息键。
- 抢占：紧急类别在容量不足时自动抢占 rank 严格更低的预留（按 rank 升序、创建时间降序逐个释放，直到容量足够）；若抢占全部低优先级预留仍不足，则不执行任何抢占并记录拒绝。配额校验先于抢占，紧急遥测也不能造成配额透支。`POST /reservations/{id}/preempt` 支持管理员手动抢占并记录原因。
- 释放与延期：`POST /reservations/{id}/release` 幂等释放；`POST /reservations/{id}/defer` 释放当前片占用后尝试进入目标片（不触发抢占），容纳不下则进入 `deferred` 等待状态，可再次延期。
- 迁移：`POST /timeslices/{id}/rollover` 关闭源时间片，未完成传输按 rank 降序、创建时间升序迁入目标片（未指定时取 starts_at 最早的开放片）；容纳不下转为 `deferred`，无后继时间片则标记 `expired`。
- 结算：`POST /timeslices/{id}/settle` 仅允许已关闭时间片，账单 = Σ kbps × 占用秒数 × 时间片单价，按租户聚合并明细到每条消息；重复结算返回 409。
- 审计：每次决策（reserve/reject/release/preempt/defer/migrate/settle）都保存当时的时间片带宽快照，`GET /decisions`、`GET /reservations/{id}` 可查看被拒绝或被抢占的原因，`GET /bills` 查看最终账单。

## 测试

```bash
python -m pytest
```

测试覆盖参数规则、幂等提交、配额拒绝、优先级领取、能力匹配、租约续期、失败退避、结果版本、取消、人工重试、批量操作和租约恢复，以及链路协调的幂等预留、配额拒绝、紧急抢占、释放、延期、跨片迁移、结算账单与决策快照，并保留身份与既有科学计算模块的回归用例。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
python -m app.cli compute-demo
python -m app.cli link-demo
```

`smoke` 在进程内检查根路径和健康接口，`compute-demo` 会创建示例参数模板、提交一个计算任务并让匹配能力的工作者领取，用于快速确认核心运营链路。`link-demo` 通过本地 HTTP 接口完整走一遍链路协调流程：登记优先级与时间片、普通数据预留、紧急遥测自动抢占、跨时间片迁移和结算账单，可重复执行。

## 目录结构

```text
app/
  compute/         计算模板、配额、任务、结果版本和人工干预
  link/            链路时间片、租户配额、优先级、预留、抢占、延期、迁移与结算
  api/             用户、角色、认证、审计和系统管理接口
  core/            时钟、安全、异常和分页能力
  repositories/    通用 SQLite 查询
  seismic/         既有地震计算示例领域
  services/        身份、审计和通用后台任务服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、计算运营、链路协调和身份回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL、busy timeout 和同步写入策略。提交、领取、回执和人工干预使用即时事务；任务领取通过条件更新避免同一条排队记录被重复领取。服务保存 UTC 时间字符串，测试可以注入固定时钟验证退避、租约到期和跨日配额。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
