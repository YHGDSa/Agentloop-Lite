# REFERENCE-C · AgentLoop-lite 参考卡

> 读于 2026-11-20。只读了两个文件：mini `src/minisweagent/agents/default.py`（main @ `04d809ceab9d`，2026-09-03）与 deepagents `libs/deepagents/deepagents/graph.py`（main @ `654ceb3bc780`，2026-11-20）。目的是借边界，不是复现。

## 1. 仓库 / 日期 / 范围

| 仓库 | 文件 | 版本 | 读的部分 |
|---|---|---|---|
| SWE-agent/mini-swe-agent | `src/minisweagent/agents/default.py` | `04d809ceab9d` | `AgentConfig`、`run/step/query/execute_actions`、`serialize/save` |
| langchain-ai/deepagents | `libs/deepagents/deepagents/graph.py` | `654ceb3bc780` | `create_deep_agent` 签名、中间件装配尾部、`create_agent` 编译 |

deepagents 当前 main 已经长出 permissions/profile/async subagents 等分支；本卡只取与“审批放哪一层”有关的装配顺序，不展开其他中间件。

## 2. mini 的循环（`DefaultAgent`）

- `AgentConfig`：`step_limit=0`、`cost_limit=3.0`、`wall_time_limit_seconds=0`、`max_consecutive_format_errors=3`、`output_path`。
- `run()`：清空 `messages` → 渲染 system + user → `while True: step()`；`FormatError` 计数、`InterruptAgentFlow` 追加消息、其他异常先 `save()` 再抛；`finally: save(output_path)`；最后一条消息 `role == "exit"` 时退出。
- `step()` = `execute_actions(query())`：
  - `query()` 先检查 step/cost/wall-time 上限，超限抛 `LimitsExceeded` / `TimeExceeded`；再 `model.query(messages)`、累加 cost、追加 assistant 消息；
  - `execute_actions()` 对 `message.extra.actions` 逐个 `env.execute()`，把结果格式化成 observation 消息追加。
- 状态就是 `self.messages` + `cost/n_calls/_start_time`；`save()` 每步把 trajectory 写成 JSON，但**从不读回**。

## 3. deepagents 的中间件边界

`create_deep_agent(...)` 本身不跑循环，它装配一个中间件列表，最后 `create_agent(..., checkpointer=checkpointer)` 编译成 LangGraph 图。关键顺序（graph.py L962—L1036）：

1. 基础栈 + 子 Agent 栈；
2. `skills` → `SkillsMiddleware`；
3. `memory` → `MemoryMiddleware`；
4. `interrupt_on`（与 permissions 的 `interrupt` 规则合并）→ `HumanInTheLoopMiddleware`；
5. `create_agent` 编译，`checkpointer` 透传。

因此**审批在 middleware 层**：`interrupt_on` 按工具名声明“执行前停一下”，`HumanInTheLoopMiddleware` 负责在工具调用前中断；真正的中断/恢复由 LangGraph 的 interrupt + `checkpointer` 持久化图状态完成。它不在工具函数内部，也不是单独的业务节点。

## 4. 两个必答问题

**mini 为什么没有 checkpoint？**
因为它是单进程、一次性批处理循环：状态只有 `messages` 和几个计数器，没有人工暂停，也没有“从中间继续”的语义；trajectory 落盘只为事后看，不能恢复。进程死了就重跑任务。加 checkpoint 要引入持久化状态和状态对账，违背它“最小可读”的目标。

**deepagents 把审批放在哪一层？**
放在 agent middleware 层（`HumanInTheLoopMiddleware`），位于中间件栈尾部、`create_agent` 编译之前；`interrupt_on` 只是声明式配置，暂停与恢复靠 LangGraph interrupt + checkpointer，而不是工具自己弹窗。

## 5. 借了什么 / 简化了什么

**借：**

1. mini 的“模型 → 动作 → 观察”线性循环，以及显式上限（C 用 `max_steps`，步数落库）；
2. mini 的“一次 step = 一次模型调用 + 执行该消息里的动作 + 观察消息”粒度 → C 的 `steps` 一行一次迭代；
3. deepagents 的“高危工具执行前拦截”位置 → C 在执行工具前按 `risk` 检查，挂起为 pending step + `WAITING_APPROVAL`（T5）；
4. deepagents 用持久化换恢复的思路 → C 用 SQLite 的 `runs/steps/approvals`，但只保证审批挂起可恢复，不做全图 replay；
5. 两者都把工具参数与观察留在轨迹里 → C 的 trace 记录参数、attempt、latency、error_type。

**简化 / 不借：**

- 不引入 LangChain/LangGraph，不用消息图；状态机 6 状态显式落库。
- 不做 skills / memory / subagents / permissions DSL / 流式，只保留 3 个 Mock 工具。
- 不支持任意工具的 interrupt 配置，只按 `risk=high` 固定规则。
- 不做完整 checkpoint：`RUNNING` 进程崩溃不自动恢复，这是已知限制，不是 bug。
- 不把“借边界”说成“复现 deepagents”。

**一句话：** mini 给了循环与上限，deepagents 给了“审批是中间件、恢复靠持久化”的位置感；C 用 SQLite 状态机把这两点落到可测试的最小实现。
