# 3 分钟演示脚本 · AgentLoop-lite

> 目标：让面试官在 3 分钟内看到「状态机 + 工具治理 + 人工审批 + Trace + 评测」，
> 而不是看一个聊天框。全程默认**脚本模式**，不需要网络和 API key。

## 0. 准备（演示前 1 分钟，不计入 3 分钟）

```bash
cd agentloop-lite
.venv\Scripts\activate        # 或 source .venv/bin/activate
streamlit run ui/app.py
```

打开页面后确认右上角 `Real LLM: configured / not configured`——两种都行：
- `not configured`：只讲脚本模式，第 6 段改成一句"有 key 时同一页切真实模式"。
- `configured`：第 6 段可以现场点一次真实运行（可选，不强求）。

如果 8501 被占用：`streamlit run ui/app.py --server.port 8502`。

---

## 1. 30 秒｜这是什么

> "这是我做的一个最小 Agent 运行时。它不做聊天，做的是**Agent 真正难的部分**：
> 状态机、工具治理、高危操作的人工审批、可审计的 Trace，以及 30 条任务的运行时评测。
> 代码大约 1200 行，只有 3 个 Mock 工具，但每个边界都能跑出数据。"

指着页面标题下的流程：`plan → validate → 审批闸门 → 执行（超时/重试）→ 记录 → trace`。

## 2. 40 秒｜脚本模式：正常排查

1. 保持默认「脚本演示（默认）」。
2. 目标框保持默认：`查看 api 最近 10 分钟日志和 CPU`。
3. 点 **① 正常排查**。

预期：`status=SUCCEEDED`、`steps=2`；Trace 里两个 step，每个 1 次 `success` attempt。

> "planner 是脚本回放的，所以这一步结果是确定的：先 get_logs 再 get_metrics，然后 finish。
> 每一步的 args、observation、latency、attempt 都在下面。"

## 3. 40 秒｜高危重启：审批闸门

1. 点 **② 高危重启（restart_service，需要审批）**。
2. 页面出现黄色提示：`run 正在等待人工审批：高危工具还未执行`。
3. 先点 **⛔ Reject**。

预期：`status=CANCELLED`，trace 里 step 1 是 `restart_service`、`observation` 为空/被拒绝说明，
`approvals=['reject']`，`tool_calls` 为空。

> "注意工具根本没执行——pending step 只占位，Reject 之后 run 直接 CANCELLED，
> 审计里能看到是谁拒的。"

4. 再点一次 **② 高危重启**，这次点 **✅ Approve**。

预期：`status=SUCCEEDED`，`steps=1`（审批不新插 step），`tool_calls=[1 success]`，`approvals=['approve']`。

> "Approve 只更新那条 pending step，step_count 不会从 1 变 2——这是为了 trace 口径一致。"

## 4. 30 秒｜超时与重试

1. 点 **③ 超时重试（get_logs timeout:2）**。

预期：`status=SUCCEEDED`，step 1 下三条 attempt：`failed(ToolTimeout) / failed(ToolTimeout) / success`。

> "同一个 step 里记了三次 attempt。只有幂等工具才重试，restart_service 失败一次就直接 FAILED；
> 演示里把超时压到 0.2 秒，所以不会卡住。"

## 5. 30 秒｜评测：baseline vs delta

打开仓库里的 `eval/report.md`（可以用编辑器或 `cat`）。

> "30 条脚本化任务，每条跑两遍：baseline 去掉重试/审批/循环检测，delta 是完整运行时。
> 成功率 22/30 → 30/30，审批拦截 0/8 → 8/8，循环拦截 0/6 → 6/6。
> 注意——**这里测的是运行时控制流，不是模型能力**，所以模型再强也不会把这幾個数字变好看。"

如果被问"为什么 baseline 也有 22"：
> "正常路径本来就不需要治理；Delta 的收益集中在危险操作（审批）和坏规划（循环检测/重试）。"

## 6. 20 秒｜真实 LLM（可选）

- 有 key：把模式切到「真实 LLM」，点"运行（真实 LLM）"，等 3–10 秒。
  > "同一个 executor、同一套治理，只是 planner 换成真实模型。"
- 没 key：切不过去是**设计如此**——按钮禁用、提示 `Real LLM: not configured`。
  > "真实模式必须显式配置 .env 才启用，key 不会出现在页面、日志和 trace 里。"

---

## 追问时的三个备答

| 问题 | 一句话回答 |
|---|---|
| 为什么不用 LangGraph？ | 这个项目的价值是"能讲清每个状态和每次写库"，不是复现框架；要框架的时候我会在 T8 的 B 项目里做取舍。 |
| 审批后崩了怎么办？ | 启动时把遗留 `RUNNING` 收口成 `FAILED` + `OrphanedRun`，要求人工对账，不自动重放非幂等工具（D14）。 |
| 并发 approve 会怎样？ | `UPDATE ... WHERE status='WAITING_APPROVAL'` 的 CAS 只有一个能成功，另一个 409；测试里覆盖了第二次 approve 的 409。 |

## 备用：清空演示数据

页面用的是 `ui/demo.db`（已 gitignore）。想从零开始：

```bash
rm ui/demo.db        # Windows: del ui\demo.db
```

不影响 `agentloop-lite/data/agentloop.db`（uvicorn 用的默认库）。
