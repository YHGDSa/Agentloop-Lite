# AgentLoop-lite

[![CI](https://github.com/YHGDSa/Agentloop-Lite/actions/workflows/ci.yml/badge.svg)](https://github.com/YHGDSa/Agentloop-Lite/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> 一个最小可审计的 Agent 运行时：6 状态机 · 3 个受治理工具 · 高危操作人工审批（HITL）·
> SQLite 审计（steps / tool_calls / approvals）· 只读 Trace · 30 题脚本化运行时评测。

没有 LangChain / LangGraph。状态迁移表、执行循环、审批闸门、重试与审计都是显式代码——
每一行都能在测试里断言，每一个数字都能用仓库里的脚本复跑。

## 60 秒上手（不需要 API key）

需要 **Python 3.12**（与 CI、开发环境一致；3.10/3.11 上的 pytest 9 没验证过）。

```bash
git clone https://github.com/YHGDSa/Agentloop-Lite.git agentloop-lite
cd agentloop-lite

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
# 如果 python 不是 3.12：python3.12 -m venv .venv / py -3.12 -m venv .venv
python -m pip install -r requirements.txt

python -m streamlit run ui/app.py
```

打开终端给出的地址（默认 `http://localhost:8501`），保持默认的「脚本演示（默认）」模式：

1. 点 **① 正常排查** → Trace 出现 2 个 step（`get_logs`、`get_metrics`），状态 `SUCCEEDED`；
2. 点 **② 高危重启** → 状态 `WAITING_APPROVAL`（工具还没执行）→ 点 **✅ Approve** 变 `SUCCEEDED`，
   再跑一次点 **⛔ Reject** 变 `CANCELLED`；
3. 点 **③ 超时重试** → 同一个 step 下 3 次 attempt：`failed / failed / success`。

看的是状态、步数、每次 attempt 和审批记录，不是聊天框。没有 key 时「真实 LLM」模式被禁用并显示
`Real LLM: not configured`——这是设计，不是故障。

## 复跑测试与评测

```bash
pytest -q                              # 23 passed，离线，不需要 key
python eval/run_eval.py                # 30 条任务 × baseline/delta，写 eval/report.md
EVAL_STRICT=1 python eval/run_eval.py  # CI 模式：delta 不是 30/30 就直接失败
```

**30/30 是脚本化运行时评测**（fake planner + mock tools）：它衡量重试、审批闸门、循环检测这些
控制流是否按设计生效，**不是模型能力评测**。真实模型冒烟需要 key，见「真实 LLM 冒烟」，且不在 CI 里跑。

## 这是什么 / 不是什么

| 是 | 不是 |
|---|---|
| 显式状态机 + 事务边界，状态和证据行同事务落库 | 又一个聊天 demo |
| 高危工具执行前暂停，等人 approve / reject，可审计 | 让模型直接调 shell / 运维接口 |
| 有重试分类、幂等键、循环检测、启动收口的最小运行时 | 通用 Agent 框架 |
| 30 条脚本化任务、baseline/delta 可复跑的控制流评测 | 模型能力/准确率评测 |

## 架构

```text
                       HTTP (FastAPI)
  POST /runs  ─────────────►  routes  ─────────►  executor
  POST /runs/{id}/approve        │                  │
  POST /runs/{id}/cancel         │                  ├─► planner（真实 LLM 或脚本回放）
  GET  /runs/{id}/trace ◄────────┘                  ├─► registry.validate_args（JSON Schema）
                                                    ├─► tools（Mock）── asyncio.wait_for(timeout)
                                                    └─► SQLite: runs / steps / tool_calls / approvals
                                                          ▲
  Streamlit (ui/app.py) ── TestClient（进程内调用，无需另起 uvicorn）
```

- `app/state_machine.py`：6 个状态与允许的迁移；`transition()` 只做 compare-and-set，不 commit。
- `app/executor.py`：plan → validate → 高危拦截 → 执行（超时 + 有界重试）→ 记录；事务边界在这里。
- `app/planner.py`：一次 LLM 调用只产出一个 JSON Plan，坏答案修复一次，两次失败抛 `PlanningError`。
- `app/registry.py` / `app/tools.py`：工具契约（JSON Schema + risk + 幂等 + 超时）与 3 个 Mock 工具。
- `app/routes.py`：7 个 HTTP 端点，异常映射 404 / 409 / 422 / 503。
- `ui/`、`eval/`、`docs/`：演示页、评测与决策记录，不计入运行时文件预算。

## 两种模式

| 模式 | 需要 key | 说明 |
|---|---|---|
| **脚本演示（默认）** | 否 | planner 换成脚本回放，离线可跑，适合演示与录屏 |
| **真实 LLM** | 是 | 只有 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 都存在时才启用；否则单选禁用 |

真实模式配置：

```bash
cp .env.example .env
# 编辑 .env：LLM_BASE_URL / LLM_API_KEY / LLM_MODEL（占位符不要当真实 key）
```

UI 不提供 key 输入框，只显示 `Real LLM: configured / not configured`。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/runs` | 创建并同步执行 run，返回 `201`；可能停在 `WAITING_APPROVAL` |
| `GET` | `/runs/{id}` | 查 run；过期审批会惰性转为 `CANCELLED` |
| `GET` | `/runs/{id}/trace` | 只读视图：run + steps（含 attempts）+ approvals |
| `POST` | `/runs/{id}/approve` | `{"decision":"approve"/"reject","note":...}`；非等待态 409 |
| `POST` | `/runs/{id}/cancel` | 取消 `CREATED` / `RUNNING` / `WAITING_APPROVAL`；终态 409 |
| `GET` | `/tools` | 3 个工具的契约（name/description/schema/risk/idempotent/timeout） |
| `GET` | `/health` | `{"ok": true}` |

## 设计取舍

- **为什么 HITL**：`restart_service` 是 `risk=high` 且非幂等。运行时要在副作用发生前停下来等一个人说
  yes/no。实现是 pending step + `WAITING_APPROVAL`；approve 只填这条 step、不新插 step，
  `step_count` 与 trace 口径不变（D13）。
- **为什么 fail-safe**：崩溃后 pending 工具可能已对外生效。启动时把 `RUNNING` 一律收口为 `FAILED` +
  `OrphanedRun`，要求人工对账，而不是自动重试非幂等操作（D14）。
- **为什么 BASELINE 只用于 eval**：评测需要“去掉运行时治理”的对照组。`BASELINE` 默认 `False`，
  只在 `eval/run_eval.py` 里短暂打开，关闭重试 / 审批 / 循环检测三件事（D16）。
- **为什么脚本化 planner**：T6 测的是控制流，不是模型。脚本回放让结果确定、免费、可进 CI；
  真实模型只出现在 UI 的真实模式和手工冒烟里（D17）。

完整决策日志见 [`docs/DECISIONS.md`](docs/DECISIONS.md)；读过的两个开源项目笔记见
[`docs/REFERENCE-C.md`](docs/REFERENCE-C.md)。

## 真实 LLM 冒烟（手动，需要 key）

`eval/run_llm_smoke.py` 用环境/`.env` 里的配置跑两条真实 Planner 路径，并断言：

1. 普通任务：模型返回合法 JSON Plan → 工具校验并执行 → finish → `SUCCEEDED`；
2. 重启任务：`restart_service` → `WAITING_APPROVAL`，批准前 trace 里没有工具执行 →
   approve 后只执行一次 → `SUCCEEDED`，`step_count` 仍为 1。

```bash
python eval/run_llm_smoke.py
```

它不属于 pytest，也不在 CI 里跑；输出只有模型名、状态、attempt 和脱敏后的错误（key 会被替换成 `***`）。

## 评测

`eval/report.md`（30 条脚本化任务，每条跑 baseline 与 delta 两遍）：

| 指标 | baseline | delta |
|---|---|---|
| 成功率（status == expect） | 22/30 | **30/30** |
| 平均步数 | 1.93 | 1.73 |
| 审批拦截率（approval 组） | 0/8 | **8/8** |
| 循环拦截率（loop 组） | 0/6 | **6/6** |
| 平均重试（failed attempt / 有 tool_calls 的 step） | 0.12 | 0.28 |

**口径：脚本化 planner + Mock 工具，测运行时控制流，不测模型能力。** 失败/差异案例、分组明细和结论
见 [`eval/report.md`](eval/report.md)；重跑会按同样的 30 条任务重新生成这份报告。

## 已知限制

- **非幂等工具崩溃需人工对账**：`restart_service` 已生效但结果没落库时，启动收口只标记 `OrphanedRun`，不自动恢复。
- **惰性超时**：审批过期只在 `GET /runs/{id}`、`GET .../trace` 或 approve 时判定；没人访问的 run 需要外部巡检。
- **SQLite 单进程**：并发保护靠 `UPDATE ... WHERE status=?` 的 CAS，适合单机演示；多实例要换 Postgres 或加锁。
- **真实 LLM 未纳入 pytest**：测试全部离线；真实模型只在 UI 真实模式和 `eval/run_llm_smoke.py` 里验证。
- **trace 全量返回**：没有分页，当前 6 步上限下数据量可控。

## 安全

- key 只来自 `.env` 或环境变量；`.env` 在 `.gitignore` 里，不提交、不打印、不写进 trace。
- UI 不允许输入 key，只显示是否配置。
- LLM 请求 `temperature=0`、`max_tokens=512`，避免无界输出。
- 工具参数用 JSON Schema 校验（`additionalProperties: false`），未知工具与非法参数都会落成可审计的 step。

## 项目结构

```text
agentloop-lite/
├─ app/                 # 运行时（12 个 Python 文件）
├─ tests/               # 离线测试（3 个文件，23 个用例）
├─ ui/app.py            # Streamlit 单页演示（脚本 / 真实双模式）
├─ eval/                # tasks.json、run_eval.py、report.md、run_llm_smoke.py
├─ docs/                # DECISIONS.md、REFERENCE-C.md、DEMO.md
├─ .github/workflows/    # CI：pytest + EVAL_STRICT=1 的脚本化评测（Python 3.12）
└─ README.md · LICENSE · requirements.txt · pytest.ini · .env.example
```

## 开发命令

```bash
pytest -q                              # 23 passed，离线
python eval/run_eval.py                # 重新生成 eval/report.md
python eval/run_llm_smoke.py           # 可选：真实 LLM 两条路径冒烟（需要 key）
streamlit run ui/app.py                # 演示页
```

## License

[MIT](LICENSE) © 2026 The AgentLoop-lite Authors

