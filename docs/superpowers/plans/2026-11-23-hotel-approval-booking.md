# P7 Hotel Approval and Booking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**目标**　用户从最新的 P6 酒店结果发起演示提案，在同一网页确认或拒绝。服务可以在重启后恢复审批，批准时对每晚库存至多扣减一次。

**架构**　复用现有线程锁、PostgreSQL 业务 schema 和父图检查点。持锁连接保存提案与暂停检查点。独立事务先持久化用户决定，再由父图恢复到唯一的预订执行节点。预订事务内同时扣减库存并写订单和回执。

**技术栈**　Python 3.12.10、FastAPI、LangGraph、Psycopg、PostgreSQL、SQLAlchemy Core、Alembic。

**规格**　[P7 网页审批与演示酒店预订](../specs/2026-11-23-hotel-approval-booking-design.md)。

## 全局约束

- 从 `E:/multi-agent/Multi-Travel-and-shopping--Agent` 的正式 `main`（`2f43dc3`）在同一目录创建单一 P7 功能分支。保留现有工作树、数据库及未跟踪的本机文件。
- 沿用两份锁及 `run-local.ps1`、`web_app.app.runtime:app`。P6 角色保持只读，预订使用合成库存，无付款、外部预订和默认付费调用。
- 提案十分钟有效。每会话一个待处理提案。状态依据 PostgreSQL，日期区间以入住日含、退房日不含。决定不可覆盖。成功回执优先于过期判断。
- 实际 PostgreSQL 验收使用临时业务与检查点 schema。命令从正式目录运行，优先用已有本机配置。留痕只记路径、状态、脱敏标识及计数。
- 每项依次记录 RED、实现、GREEN、原始验收输出和独立本地提交。完整规格和计划随功能分支交付。

## 文件边界与重点回归

- 业务契约及持久化位于 `customer_support_chat/app/business/booking.py`、`booking_repository.py`，Alembic 定义增量 schema。Task 1 验证既有数据和多次迁移、会话归属、过期、重复发起。
- 库存事务和受控调用位于业务仓储及 `customer_support_chat/app/tooling/booking.py`。Task 2 验证跨会话竞抢、价格等额重分配、跨过过期时间和库存更新中途回滚。
- 父图适配位于 `workflow/parent_graph.py`、`workflow/checkpoints.py`。Task 3 验证提案和暂停原子写入、老状态兼容，以及业务成功后的图提交恢复。
- Web 后端扩展 `web_app/app/runtime.py`，审批协调位于新增 `web_app/app/bookings.py`。Task 4 验证不可信请求、跨账号和并发消息、关闭过期暂停、无需原客户端状态的恢复。
- 页面改动和真实验收沿用正式会话模板与 E2E 脚本。Task 5 验证刷新、拒绝、确认重放、收据和 P6 兼容，且渲染只使用安全文本。

## Context7 查证清单

每项写 RED 前按库、版本及概念分别查证，记录实际匹配版本。未索引或版本不符按 spec-coding 版本门禁处理。

- Task 1：Alembic 1.20.1 的复合约束及部分索引，SQLAlchemy 2.0.43 的 PostgreSQL DDL 类型，Psycopg 3.3.9 的异步连接、标识符组合、绑定参数和 JSONB 适配。
- Task 2：Psycopg 3.3.9 的异步事务和异常回滚；Python 3.12.10 的 `asyncio.timeout`；Compose 锁定的 PostgreSQL 19.0 的 `FOR UPDATE`、数据库时钟和唯一约束。
- Task 3：LangGraph 0.2.76 的静态中断、`aget_state`、`ainvoke(None, config)` 与命名边；LangGraph Checkpoint 2.1.2 和 PostgreSQL saver 2.0.25 的 JSON serde 与外部事务连接；LangChain Core 0.2.43 的 `RunnableConfig`；Psycopg 3.3.9 的事务锁连接生命周期。
- Task 4：FastAPI 0.116.1 的依赖、JSON 响应和 HTTP 例外；Pydantic 2.11.7 的严格整数、枚举及禁用额外字段；HTTPX 0.27.2 的 ASGI 测试与错误路径。
- Task 5：HTTPX 0.27.2 的带 cookie 同源会话与超时；Python 3.12.10 的既有测试进程启动和退出接口。页面沿用已有原生 DOM 和 Fetch。

## Task 1 · 提案、决定与回执存储基础

**文件**　新增 `migrations/versions/0003_hotel_booking.py`、`customer_support_chat/app/business/booking.py`、`booking_repository.py`、`tests/test_booking_repository.py`；修改同目录 `schema.py`、`database.py`、`workflow/checkpoints.py` 的共享连接参数导入和 `tests/test_business_migrations.py` 的表名单。

**接口**　共享类型不引入角色环境依赖。定义 `BookingOwner(user_id: UUID, thread_id: UUID)`、`BookingSelection(message_id: UUID, option_index: int)`、`BookingScope(owner: BookingOwner, action_id: UUID)`、`BookingDecision = Literal["approve", "reject"]`。定义 `ProposalView`，包括 `action_id`、会话内来源、酒店及房型、入住与退房日期、演示总价与币种、到期时间、公开状态、可选决定和 `booking_id`。公开状态使用 `awaiting_decision | decided | booked | rejected | expired | stale`。投影不携带用户身份或每晚库存。仓储接口如下。

- `BookingRepository(settings: DatabaseSettings)`。
- `create_proposal(connection: AsyncConnection, owner: BookingOwner, selection: BookingSelection, option: HotelOption) -> ProposalView`。与调用方共享事务。验证归属和源消息，按可信选项重读整段报价。
- `find_proposal(owner: BookingOwner, selection: BookingSelection, *, connection: AsyncConnection | None = None) -> ProposalView | None`。同一来源同一选项的重发可取历史记录，不同选项的重发为冲突。
- `get_proposal(scope: BookingScope, *, connection: AsyncConnection | None = None) -> ProposalView | None`；`latest_proposal(owner: BookingOwner, *, connection: AsyncConnection | None = None) -> ProposalView | None`；`record_decision(scope: BookingScope, decision: BookingDecision) -> ProposalView`。决定使用自身的短事务并比较数据库时钟。
- 统一领域异常 `ProposalNotFound`、`ProposalConflict`，后者有稳定 `code` 供正式 API 映射。

- [ ] 在旧临时 schema 应用 0002、导入种子、创建线程及消息，再两次升到 head。断言原行可读、新表存在、已有检查点无迁移依赖。直接验证订单和回执的 `action_id` 唯一、单会话待处理部分唯一索引。
- [ ] 写 `test_proposal_is_owned_snapshotted_and_replayable`，选 P6 示例 2026-12-15 入住、次日退房、CHF 18000。断言 10 分钟、逐晚快照、选项重放同一 ID、换选项冲突、陌生账号无结果、报价变化时无提案。写 `test_decision_is_immutable_and_late_decision_cannot_approve`，覆盖重复、相反、已到期决定。
- [ ] 运行 `& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_booking_repository.py -v`。RED 预期为仓储契约缺失。
- [ ] 实现增量迁移与仓储。提案对 `(thread_id, source_message_id)` 唯一，对 `awaiting_decision/decided` 的 `thread_id` 建部分唯一索引。订单按 `action_id` 唯一，回执关联同一 action 的订单，库存保持现有主键。单商业模块使用 Psycopg 绑定参数，业务 schema 用验证及 `Identifier`，按需共享连接。移动现有 `connection_kwargs(settings)` 至 `business/database.py`，保留检查点参数和调用行为。过期在读取时计算有效状态，写入路径可实际关闭；有原决定时先校验决定相容性。
- [ ] 复跑定向测试及 `test_business_migrations.py`，记录清理和旧数据回归。提交 `feat: persist scoped hotel booking proposals and decisions`。

## Task 2 · 单次预订事务与审批写入执行层

**文件**　修改 `business/booking.py`、`business/booking_repository.py`、`tooling/executor.py`；新增 `tooling/booking.py`、`tests/test_booking_transaction.py`。

**接口**　`BookingRepository.resolve_action(scope: BookingScope) -> BookingResolution`。结果包含 `proposal: ProposalView`、`reused_receipt: bool`。使用独立业务事务。新增 `ApprovedBookingExecutor(repository: BookingRepository, audit_sink: AuditSink)` 和 `execute(scope: BookingScope, *, request_id: UUID) -> BookingResolution`。只供父图恢复节点使用，复用统一工具审计的限时写入实现。

- [ ] 写 `test_approved_booking_is_atomic_and_replay_returns_same_order`，真实数据库两晚各减一、订单和回执各一条；并发重复确认都解析至同一个结果。写 `test_unapproved_rejected_expired_or_wrong_owner_never_decrements`，无合法决定或错误归属不进入库存写分支。
- [ ] 写 `test_competing_threads_and_changed_night_quote_are_stale`，两个用户争最后一间房，稳定加锁后只成功一个；对两晚价格一加一减保持总价，仍应判过时。覆盖单晚币种变更、数量变动和缺行。
- [ ] 写 `test_failed_receipt_insert_rolls_back_booking_and_all_stock`。仅在临时 schema 增加拒绝回执写入的数据库触发器，验证库存、订单和回执原样。清除本轮触发器后向原 action 重试可成功。写 `test_existing_receipt_outlives_expiry_but_unexecuted_approval_does_not`，时间通过测试数据更新到期值，避免真实等待。
- [ ] 写审计回归，验证 `action_id` 与请求 ID 关联，审计独立失败仍返回成功回执。运行 `& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_booking_transaction.py -v`。RED 预期为执行契约缺失。
- [ ] 实现事务。锁定提案后先查回执，再检查决定、状态及有效期。已有终态直接返回；未到期且无决定时抛出 `ProposalConflict("decision_required")`。拒绝或到期关闭提案。批准按 `stay_date` 顺序 `FOR UPDATE` 锁定酒店及房型的库存行并比较各晚快照，最后一次使用数据库实际时间检查到期。任一不符返回 `stale`。全部一致后扣减、创建订单、落回执并关闭提案。禁止自动重试写入；失败交给请求方按同一 action 重放。复用独立限时审计，角色工具注册的只读约束保留。
- [ ] 复跑本任务、Task 1 仓储测试及既有 `test_unified_tool_executor.py`。提交 `feat: commit hotel inventory and receipts atomically`。

## Task 3 · 父图暂停及同线程恢复

**文件**　修改 `customer_support_chat/app/workflow/parent_graph.py`、`checkpoints.py`；新增 `tests/test_booking_graph.py`。

**接口**　`BookingGraphState(action_id: UUID | None, awaiting_resolution: bool)`。扩充 `GraphRuntime.read_booking(thread_id: UUID, *, connection: AsyncConnection | None = None) -> BookingGraphState`；`start_proposal(owner: BookingOwner, selection: BookingSelection, *, connection: AsyncConnection) -> ProposalView`；`resume_booking(scope: BookingScope, *, connection: AsyncConnection, request_id: UUID) -> ProposalView`。可选注入仓储及 `ApprovedBookingExecutor`；未注入时维持 P6 行为。API 通过现有锁取得的连接传入以上写路径。

- [ ] 写 `test_proposal_and_pause_commit_or_rollback_together`。在同一持锁连接生成提案并静态暂停在 `apply_booking_decision`；人工中断事务后，另一个连接查不到提案和暂停工作。生命周期重开后正常方案可读取原 P6 推荐及暂停引用。
- [ ] 写 `test_restart_resumes_decision_without_new_message_turn`：持久化决定后停掉图运行时，再打开 runtime 并恢复。预订操作不增加历史轮次。写 `test_receipt_commit_survives_checkpoint_rollback`：独立业务事务成功后使外层图事务回滚，重启按同一 action 恢复，断言订单相同、库存只扣一次。
- [ ] 写 `test_unapproved_or_mismatched_action_cannot_write_and_legacy_graph_stays_readable`。检测暂停节点和 action 匹配，未记录决定时保持暂停或返回可识别冲突，旧 JSON 会话和正常消息按原契约运行。
- [ ] 运行 `& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_booking_graph.py -v`。RED 预期为父图入口缺失。
- [ ] 实现。持锁连接读 P6 `completed`，验证消息为最新 `hotel_results` 且索引合法，选择项仅由已验证状态生成。先查既有提案以处理重放。在同一连接写提案并提交父图暂停检查点，普通消息明确清除输入路由标记，审批不覆盖 `completed`。决定路由先在独立事务持久化，再用 `ainvoke(None, config)` 从同一线程继续。节点从可信状态取 action 和身份，调用受控执行层。若暂停已完成，读取业务终态返回；若仍暂停，确认对应 action 后再恢复。外层图恢复事务不获取提案行锁。
- [ ] 复跑父图定向及既有 `test_graph_checkpoints.py`、`test_hotel_handoff_graph.py`。提交 `feat: pause and resume approved bookings in the parent graph`。

## Task 4 · 正式审批 HTTP 与线程互斥

**文件**　新增 `web_app/app/bookings.py`、`tests/test_booking_api.py`；修改 `web_app/app/runtime.py` 及必要的 `tests/test_graph_api.py` 兼容断言。

**接口**　增加严格请求模型 `HotelProposalRequest(message_id: UUID, option_index: int)`，索引为严格 0–4 整数；`BookingDecisionRequest(decision: Literal["approve", "reject"])`。增加规格中的两条 POST。首次创建 201，重放 200；决定成功或业务终态返回 200。未知归属 404，输入错误 422，冲突 409，故障 503。GET 增加可选 `booking` 投影，包含稳定 `resume_required: bool`，原有 `messages` 与 `graph` 字段保持。页面用该投影识别决定已保存且图待继续。

- [ ] 写 `test_proposal_and_decision_use_trusted_fields_only`。B 无法读、建提案或批准 A 的线程。A 将提案 ID 用在自己的另一个线程收到相同 404。额外金额、币种、日期或身份字段返回 422。本人旧快照和越界选项分别验证 409/422。
- [ ] 写 `test_pending_approval_blocks_message_before_append`。等待确认或恢复时新消息返回 409，数据库历史无此消息；不同应用进程共享 PostgreSQL 线程锁；并发两个同决定请求最多写一笔，争锁失败请求随后可取原回执。
- [ ] 写 `test_decision_and_receipt_recovery_after_lifespan_restart`。注入决定事务之后、图调用前失败，以及订单提交之后、图提交失败。重开应用后 GET 为只读且有可用恢复入口，重发原决定恢复成功。写 `test_expired_pending_does_not_block_the_next_message`，验证无决定的过期提案自动收束后写下一条消息。
- [ ] 运行 `& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_booking_api.py -v`。RED 预期为路由缺失或兼容行为未实现。
- [ ] 接入既有登录、正式 lifespan 和线程锁。消息、提案、决定共享已有进程内限流及 `try_thread_lock`，释放路径统一。读取业务状态及暂停 action 后处理过期和恢复状态，再决定是否写消息。过期提案由消息或决定请求收束，下一份提案从新的推荐轮次生成。对同决定重放保持业务幂等，对相反决定在业务记录存在后返回 409，503 用原有脱敏错误合同并保留可重试 action。
- [ ] 复跑 Task 4 定向和 `test_graph_api.py`。提交 `feat: serve authenticated booking approvals and recovery`。

## Task 5 · 网页确认与正式端到端验收

**文件**　修改 `web_app/app/templates/local.html`、`scripts/verify_local_e2e.py`、`tests/test_unified_home.py`、`docs/RUNBOOK.md`、`docs/VERIFICATION.md` 与正式仓库的 P7 规格和计划副本。

**接口**　每条可选酒店结果提供「发起演示预订」。页面提案展示酒店、房型、日期、总价、币种、到期时间。待决定显示「确认预订」「拒绝」。`resume_required` 且已有决定时展示「继续处理」。成功展示演示订单号。拒绝、超时、过期和库存变化提供各自反馈。显示内容用 `textContent`。

- [ ] 增加正式模板回归及真实 HTTP E2E 验收，覆盖 A 选择、B 拒绝访问、刷新、关闭重开、拒绝无库存变化、批准后重放同一订单。先制造库存或价格变化以验收旧提案作废。由真实 PostgreSQL 检查一笔订单和回执、库存净扣减一间。
- [ ] 运行 `& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_unified_home.py -v`，RED 预期为网页缺少审批交互。实现网页。页面状态以 GET 返回为准，重复点击等待当前响应；恢复动作重交原决定。浏览器级验证确认与拒绝按钮实际触发相应请求，刷新后仍可继续，流程使用隔离合成账号。
- [ ] 跑完整验收并区分单元、数据库、正式启动、真实 HTTP、浏览器交互结果。更新手册与记录，确认两份依赖锁无变化、临时 schema 已清理、付费调用为零。只暂存预期文件并提交 `feat: expose demo booking confirmation in the travel web app`。

```powershell
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -v
& ./flight_role_validation/.venv/Scripts/python.exe -B -m unittest discover -s flight_role_validation/tests -v
& ./.venv/Scripts/python.exe -B scripts/verify_local_e2e.py
git diff --check
```

Gate 4 对完整分支独立评审，按发现项做失败与修复复验。Gate 5 在用户选择方式后执行集成。
