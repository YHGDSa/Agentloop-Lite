# AgentLoop-lite 运行评测报告（T6）

- 日期：2026-11-22
- Python：3.12.15
- 运行器：`eval/run_eval.py`（stdlib + FastAPI TestClient；每条任务一个独立 DB，放 `eval/.tmp/`）
- planner：**脚本化**（每条任务预置 Plan 序列），不调用真实 LLM
- 工具：进程内 Mock（`get_logs` / `get_metrics` / `restart_service`），由 `FAULTS` 注入故障
- 工具 `timeout_s` 统一压到 0.05s：只为缩短评测耗时，超时/重试判定不受影响
- 每条任务跑两遍：`BASELINE=1`（无重试、无审批、无循环检测）与 `BASELINE=0`（完整运行时）

> **评测的是运行时控制流（重试、审批闸门、循环检测、状态机），不是模型能力。**

## 1. 总表

| 指标 | baseline | delta |
|---|---|---|
| 成功率（status == expect） | 22/30 | 30/30 |
| 平均步数 | 1.93 | 1.73 |
| 审批拦截率（approval 组） | 0/8 | 8/8 |
| 循环拦截率（loop 组） | 0/6 | 6/6 |
| 平均重试次数（failed attempt / 有 tool_calls 的 step） | 0.12（6/50） | 0.28（11/40） |

## 2. 分组

| 组 | 条数 | baseline 成功 | delta 成功 |
|---|---|---|---|
| normal | 8 | 8 | 8 |
| failure | 8 | 7 | 8 |
| approval | 8 | 4 | 8 |
| loop | 6 | 3 | 6 |

## 3. 失败与差异案例

### t09（failure）— api 日志查询偶发超时，重试一次

- 期望：`status=SUCCEEDED`、`min_steps=1`
- baseline：`status=FAILED`、`steps=1`、`calls=1`（failed=1）、`approvals=[]`、`error='get_logs timed out after 0.05s (attempt 1)'`
- delta：`status=SUCCEEDED`、`steps=1`、`calls=3`（failed=2）、`approvals=[]`、`error=None`

### t18（approval）— 重启 db（先别动）

- 期望：`status=CANCELLED`、`min_steps=1`
- baseline：`status=SUCCEEDED`、`steps=1`、`calls=1`（failed=0）、`approvals=[]`、`error=None`
- delta：`status=CANCELLED`、`steps=1`、`calls=0`（failed=0）、`approvals=['reject']`、`error=None`

## 4. 结论

1. **重试与超时收口真正起作用**：`t09`（`timeout:2`）在 delta 下 3 次尝试后成功，baseline 一次失败；loop 组 baseline 拦截 0/6、delta 拦截 6/6，说明循环检测把'原地打转直到 max_steps'变成了显式的 `loop detected`。
2. **baseline 也能过的任务**：normal 组 8/8、`t14`（未知工具记录后继续）、以及 `t17`/`t22`/`t23`/`t24`（高危工具直接执行）——Delta 的收益集中在'危险操作'和'坏规划'，正常路径两者等价。
3. **还没覆盖的边界**：真实 LLM 的 JSON 稳定性与修复成功率、并发审批、崩溃恢复、幂等缓存命中、以及 UI/演示链路；这些是 T7 与后续阶段的事，本报告只对控制流负责。
