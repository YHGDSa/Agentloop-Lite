# DECISIONS · AgentLoop-lite

每条 4 行：决策 / 备选 / 理由 / 什么情况下换。T1—T7 的关键取舍都在这里，发布版本保留原文口径，只把机器相关路径改成相对描述。

## D1 单仓库 + 分层目录

- **决策：** 仓库根就是本项目：`app/`（运行时）、`tests/`（离线测试）、`ui/`（Streamlit 演示）、`eval/`（脚本化评测）、`docs/`（决策与参考卡）。
- **备选：** 把 UI 和评测都塞进 `app/`，或拆成多个仓库。
- **理由：** 运行时、演示层、评测是三类产物；分目录后 README 能按层指路，CI 也只需要跑 `tests/`，不必启动 UI。
- **什么情况下换：** 需要把 UI 单独部署成服务时，把它拆出去或放进独立前端仓库。

## D2 T1 只做骨架，不写状态机迁移

- **决策：** T1 交付 `config/db/schemas/main` + 四张表 + `/health`；`state_machine.py` 与 `transition()` 留到 T2。
- **备选：** 在 T1 一并写 `state_machine.py`。
- **理由：** 先把可验收的最小骨架立住；状态机需要测试和迁移表一起设计，混进骨架会让 T1 的验收标准变模糊。
- **什么情况下换：** 如果 T1 的验收就要求手工演示非法迁移，则把迁移表和测试并入 T1。

## D3 用 `create_app(db_path)` 工厂 + SQLite 文件

- **决策：** 应用工厂接收可选 DB 路径，存入 `app.state.db_path`，启动时 `init_db()`；测试每个用例一个 `tmp_path` 文件。
- **备选：** 模块级全局连接 / 内存 SQLite / 直接读环境变量。
- **理由：** 测试隔离且能验证“重启后数据还在”；SQLite 是项目指定栈，不引入额外服务。
- **什么情况下换：** 多进程写入或需要行级并发时换 Postgres；并发审批先用 `UPDATE ... WHERE status=?` 顶住。

## D4 运行时只依赖 3.10+ 语法

- **决策：** 代码只用 3.10+ 语法（`X | None`、`from __future__ import annotations`），开发机当时是 Python 3.11，后来统一到 3.12。
- **备选：** 一开始就钉死 3.12 并只用 3.12 特性。
- **理由：** 运行时不该被解释器小版本卡住；FastAPI/pytest/httpx 在 3.10+ 都可用。
- **什么情况下换：** 需要 `tomllib`、`TaskGroup` 等 3.11+ 特性时，把最低版本提到 3.11 并在 README 写明。

## D5 `.env` 只留样例，由 dotenv 可选加载

- **决策：** `config.py` 从进程环境读 `LLM_*`；`.env.example` 只放占位符，`.gitignore` 忽略 `.env`；装了 `python-dotenv` 时才加载 `.env`（`override=False`），没装则静默跳过。
- **备选：** 强制依赖 dotenv，或在代码里读 `.env` 文件。
- **理由：** CI 和测试不需要 `.env`；本地演示需要时也不该让“没装 dotenv”变成崩溃。
- **什么情况下换：** 部署到有密钥管理的环境时，把读取路径换成 secrets provider，dotenv 只留给本地。

## D6 用仓库内 `.venv`（Python 3.12）

- **决策：** 在仓库内用 `.venv` + `requirements.txt`，测试、评测和 UI 都走这个解释器。
- **备选：** 用系统 Python 直接装依赖。
- **理由：** 版本可复现、不污染系统环境，`.gitignore` 已忽略；CI 用同一个 `requirements.txt`。
- **什么情况下换：** 交付环境固定别的 Python 版本时，用 `uv venv --python X` 重建并重跑全部验收。

## D7 测试文件控制在 15 个 Python 文件以内

- **决策：** `app + tests` 的 Python 文件数 ≤ 15；审批等新用例并入 `tests/test_runs.py`，不新建 `test_approval.py`。
- **备选：** 每个主题一个测试文件，文件数放宽。
- **理由：** 15 是“运行时 + 测试”的复杂度预算，超过之后维护成本大于主题清晰度；测试按行为分组也能读。
- **什么情况下换：** 项目变大、单文件超过 800 行后，按领域拆分并重新确认预算。

## D8 transition 不 commit，executor 用 with conn 管事务

- **决策：** `state_machine.transition()` 只做 compare-and-set 的 UPDATE，不 commit / 不 rollback；事务边界在调用方（`executor.execute_run` 用 `with conn:` 包住“迁移状态 + 写 step”）。
- **备选：** 让 `transition()` 自己 commit，调用方只管状态。
- **理由：** 审批要“写 approval + 迁移状态”原子完成，执行要“迁移 + 写 step”原子；内部 commit 会把事务拆成两段，崩溃时状态和证据行不一致。
- **什么情况下换：** 换成 Postgres 或引入独立事务层（如 SQLAlchemy session）时，把边界上移，`transition()` 仍只发一条 CAS UPDATE。

## D9 planner 测试全部 mock，不联网

- **决策：** 测试用 `monkeypatch.setattr(app.planner, "plan", fake_plan)` 返回脚本化的 `Plan` 序列；另有一条“只 stub 网络”的测试（stub `planner._chat`）覆盖真实 JSON 解析路径；pytest 永不调用真实 LLM。
- **备选：** 用真实 API 跑测试，或录 vcr 磁带。
- **理由：** 测试要确定、免费、离线可跑；真实模型不稳定，会把断言失败变成模型波动。executor 写成 `from . import planner` 后调 `planner.plan(...)`，保证 monkeypatch 生效。
- **什么情况下换：** 需要评测真实模型时，用 `eval/` 下的脚本走真实 API，不进 pytest。

## D10 重试只给幂等工具，且只给三类错误

- **决策：** 可重试错误固定为 `ToolTimeout` / `RateLimited` / `UpstreamError`；只有 `idempotent=True` 的工具才重试；最多 `settings.max_attempts`（默认 3 = 首次 + 2 次重试），退避 0.2s、0.4s；`PermissionDenied`、validation、unknown_tool 和未分类异常一律不重试。
- **备选：** 所有工具统一重试；或直接上 tenacity 的指数退避 + 抖动。
- **理由：** `restart_service` 非幂等，重试可能重复重启；确定性错误重试只会烧时间和配额。退避写死两档是为了让测试可预期。
- **什么情况下换：** 工具能证明外部幂等（幂等键真的传下去）时，按工具配置可重试；并发量上来后换 tenacity。

## D11 每次 attempt 都写一行 tool_calls

- **决策：** `tool_calls` 每个 attempt 一行（`success` / `failed`；缓存命中写 `attempt=0`、`status='cached'`），与 `steps` 插入同事务；`steps.latency_ms` 记本次调用所有 attempt 的总耗时（含退避等待）。
- **备选：** 只记最终结果；或把 attempts 塞进 `steps.observation` 的 JSON。
- **理由：** “第几次、什么错、多久”是重试逻辑唯一可审计的证据；独立成行后 trace 可以直接 JOIN 出每个 step 的 attempt 列表。
- **什么情况下换：** 高频重试导致行数膨胀、或迁到 Postgres 后，可改成 `jsonb attempts` 字段。

## D12 UI 与 eval 不进 15 文件额度

- **决策：** Streamlit 页面放 `ui/`，30 题与 baseline/delta 放 `eval/`；两者都不计入 `app + tests ≤ 15`。
- **备选：** 把 UI 塞进 `app/`、把评测塞进 `tests/`，或放宽上限。
- **理由：** 15 是“运行时 + 测试”的预算；UI 和评测是演示/测量产物，混进去会让 app 包背演示代码。
- **什么情况下换：** 如果要求“一个目录跑全部”，或 UI 需要复用 app 内部模块导致循环依赖，再统一并重新确认额度。

## D13 审批 = pending step + CAS 迁移，approve 不新插 step

- **决策：** 高危工具先写一条 pending step（`observation/error_type/latency_ms` 全 NULL，创建时 `step_count + 1`），run 迁到 `WAITING_APPROVAL` 并写 `approval_expires_at`；`approve` 只 UPDATE 这条 step、再插 `tool_calls`，绝不 INSERT 新 step；`reject` / `cancel` / `timeout` 都在同一条 step 上写说明并迁 `CANCELLED`。
- **备选：** approve 时新建一条 step；或用内存锁串行化审批。
- **理由：** “一次计划 = 一条 step”是 trace 和 `step_count` 的口径；重复插入会让审批后的步数凭空 +1。CAS 在 SQLite 里天然跨进程。
- **什么情况下换：** 多实例 + 高并发审批时可加 `approvals` 唯一约束或行锁；引入任务队列后把“审批后执行”挪到 worker。

## D14 启动把遗留 RUNNING 收口为 FAILED，不自动 resume

- **决策：** 进程启动时 `recover_orphaned_runs()` 把所有 `RUNNING` 的 run 标记为 `FAILED`（error 写 orphaned；有 pending step 时写 `OrphanedRun`），不重试、不自动执行工具；`WAITING_APPROVAL` 保持原状。
- **备选：** 自动 resume（重新执行 pending 工具）或自动重跑整个 run。
- **理由：** 崩溃点可能在“工具已经对真实世界生效、结果还没落库”之间；自动重跑非幂等操作会二次重启。宁可让人对账，也不要静默重复副作用。
- **什么情况下换：** 工具能提供外部幂等键/回执查询时，可对这类工具自动 resume。

## D15 trace 是只读视图，与 GET /runs 共用惰性超时

- **决策：** `GET /runs/{id}/trace` 只读：run + steps（含每个 attempt）+ approvals；与 `GET /runs/{id}` 共用 `_load_run_with_expiry()`，读 trace 也会把过期审批置为 `CANCELLED` 并留下 timeout 记录；`args_json` 解析失败降级 `{}`。
- **备选：** trace 里再跑一次状态修复；或把 trace 做成流式/分页。
- **理由：** 读接口不该有隐藏写路径，但“过期”是时间属性，读的时候不判就会看到永远 WAITING 的假象；收在一个函数里语义才一致。
- **什么情况下换：** 有了定时巡检后把惰性超时移出读路径；数据量大时加分页。

## D16 BASELINE 只服务 eval，默认 False

- **决策：** `executor.BASELINE = False`；为 True 时只关三件事——单次尝试、高危直接执行、循环检测恒 False；持久化、trace、状态机、幂等键不变。`eval/run_eval.py` 设置后 `finally` 恢复。
- **备选：** 用环境变量或独立分支维护一个 baseline 运行时。
- **理由：** 评测需要同一份代码的“去掉治理”对照组，复制分支会漂移；环境变量会让部署时误开。
- **什么情况下换：** 基线需要更多差异（连持久化都去掉）时，拆成独立的 `eval/baseline_runtime.py`。

## D17 评测用脚本化 planner，不接真实 LLM

- **决策：** `eval/tasks.json` 为每条任务预置 Plan 序列，`run_eval.py` 把 `app.planner.plan` 换成回放器，工具用进程内 Mock；30 条任务两遍（baseline/delta）全离线跑。
- **备选：** 接真实模型跑 30 题，或录 vcr 磁带。
- **理由：** 这一层验证的是控制流（重试、审批、循环检测），模型波动会污染结论；离线还能免费、可重复、进 CI。
- **什么情况下换：** 需要端到端效果时，另写 `eval/run_llm_eval.py` 走真实 API，把本报告当控制流基线。

## D18 UI 默认脚本模式，真实 LLM 可选；ui/ 不占额度

- **决策：** `ui/app.py` 默认「脚本演示」（脚本回放 + Mock 工具，离线可跑）；只有三个 `LLM_*` 都存在时才可切「真实 LLM」，否则禁用并显示 `Real LLM: not configured`；UI 通过 TestClient 在进程内调 API。
- **备选：** 需要另起 uvicorn 的前后端分离页面；或让真实模式默认开启。
- **理由：** 演示第一要求是不依赖网络和 key；真实模式是加分项，不是默认路径。
- **什么情况下换：** 需要多人同时演示或真实并发时，改成 uvicorn + 独立前端。

## D19 key 只从 .env/环境变量读取，不打印不提交

- **决策：** `load_dotenv(..., override=False)`，真实环境变量优先；`.env` 被 gitignore，只留 `.env.example` 占位符；UI 不提供 key 输入，只显示是否配置；日志、trace、README、commit 里都不出现 key；LLM 请求 `temperature=0`、`max_tokens=512`、超时 60s。
- **备选：** UI 上放 key 输入框，或写进 `.streamlit/secrets.toml`。
- **理由：** key 进页面会被截图/录屏带走，进 trace 会被写进 SQLite；读取路径收敛到进程环境是唯一同时满足“演示方便”和“不泄露”的做法。
- **什么情况下换：** 部署到 Vault/KMS 等密钥管理环境时，把 `Settings.from_env` 换成 secrets provider。
