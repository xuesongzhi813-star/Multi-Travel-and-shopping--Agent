# P6 Flight-to-Hotel Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**目标**　用户在正式会话中选定本人航班，回答住宿晚数后，获得按到达地日期查询的只读酒店结果。

**架构**　根环境的 LangGraph 父图管理输入防护、澄清、持久状态和角色交接。沿用现有角色子进程和工具执行器。酒店角色仅获得宿主核验的查询参数，展示数据来自 PostgreSQL 工具回执。

**技术栈**　Python 3.12.10、FastAPI、LangGraph、LangChain、SQLAlchemy Core、PostgreSQL、MCP。

**规格**　[P6 航班到酒店交接](../specs/2026-11-23-flight-hotel-handoff-design.md)。

## 全局约束

- 从正式仓库 `E:/multi-agent/Multi-Travel-and-shopping--Agent` 的 `main`（复验点 `dcbd6ba`）切出单一功能分支，在同一目录增量开发。保留现有未跟踪的本机文件。设计与计划随该功能交付，现有文档和代码只在相关范围修改。
- 两份锁保持现有版本。保留现有启动入口、会话及工具路由。新增 HTTP 字段仅用于酒店分支。现有通知、会话隔离、线程互斥和 503 契约继续通过回归。
- 默认离线验证使用脚本模型、真实 PostgreSQL 和 MCP。P6 默认零付费调用。库存、订单和审批无业务写入。测试使用临时 schema 和现有合成账号。
- 日期以已核验的当地到达日为入住日。晚数 1–14，窗口起点含、终点不含。新住宿请求替换旧待办，取消时清除。未覆盖城市、无整段库存和依赖失败分别处理。
- 命令从正式根目录运行。用本机既有配置，保留 `P2_ENV_FILE` 覆盖方式，不输出配置值。每项先存 RED 原始输出，再运行 GREEN 和验收并追加留痕，然后提交。

## 文件边界

- 业务数据查询位于 `customer_support_chat/app/business/repository.py`。共享参数和结果形状位于新增 `customer_support_chat/app/workflow/hotel_handoff.py`。
- `flight_role_validation/hotel.py` 负责酒店角色与工具注册。既有 `web_worker.py` 与 `web_app/app/local_tools.py` 负责角色进程的请求、回执和执行期限。
- 现有 `parent_graph.py`、`checkpoints.py` 与 `web_app/app/runtime.py` 负责跨轮状态、会话鉴权、线程锁以及 HTTP 生命周期。现有网页和端到端脚本呈现与验证结果。

## 重点回归

1. 同房型某晚缺货、缺行或更换币种时，整段不合格，归 Task 1。
2. 模型试图改变城市、晚数或房型时，执行器拒绝并落审计，归 Task 2。
3. 「2」在无待办或等待航班时不触发查询，输入防护拒绝也不会消费待办，归 Task 3。
4. 重启后答晚数会重新查验用户与航班。航班已迁移或日期变化时停止并追问或给出不可用，归 Task 3。
5. 角色超时或客户端取消后线程可以再次进入。过期会话结果不会回写，归 Task 4。

## Context7 查证清单

各任务开始前按版本分别解析查询。项目已有调用可以作为定位线索，查证记录以提供对应版本的来源为准。版本未命中时按 spec-coding 版本门禁处理。

- **Task 1**　SQLAlchemy 2.0.43 的异步 `execute`、Core 连接与条件表达式；Python 3.12.10 的 `date.fromisoformat` 和 `timedelta`。
- **Task 2**　LangChain 1.5.2 的 `create_agent`、`after_model` 和调用限制；LangChain Core 1.8.2 的工具运行时与工具消息接口；Pydantic 2.14.4 的严格校验、Schema 和结果投影；MCP SDK 1.30.0 的 stdio 调用生命周期；Python 3.12.10 的 `asyncio.timeout_at`。
- **Task 3**　LangGraph 0.2.76 的异步节点、条件边、状态更新与配置；LangGraph PostgreSQL checkpoint 2.0.25 的 `aget_tuple` 与外部事务连接；Psycopg 3.3.9 的异步事务；Python 3.12.10 的日期和 JSON 序列化。
- **Task 4**　FastAPI 0.116.1 的请求生命周期、响应序列化和现有依赖注入；Pydantic 2.11.7 的严格验证；Python 3.12.10 的 Windows 进程启动与回收、`asyncio` 取消与期限；HTTPX 0.27.2 的真实 HTTP 和 ASGI 测试接口。

## Task 1 · 住宿契约和整段库存

**文件**　新增 `customer_support_chat/app/workflow/hotel_handoff.py`、`tests/test_hotel_inventory.py`；修改 `customer_support_chat/app/business/repository.py`。

**接口**　定义 `HotelQuery(city: str, timezone: str, check_in: date, check_out: date, area: str | None = None, room_type: str | None = None)`、`HotelOption(hotel_id: str, hotel_name: str, area: str, room_type: str, check_in: date, check_out: date, total_minor: int, currency: str)`、`HotelRoleResult(status: Literal["hotel_results", "no_availability", "unavailable"], options: tuple[HotelOption, ...] = ())`。共享类型仅使用标准库，两套环境均可导入。JSON 投影和解析使用 ISO 日期。增加 `BusinessRepository.list_available_hotels(query: HotelQuery) -> list[HotelOption]`。

- [ ] 写 `test_complete_stay_prices_and_filters_room_types`，设 12 月 15–17 日两晚，18000 加 19000 分应返回 37000 CHF。更换房型、缺行、零库存或异币种的组合各自不返回。断言区域和房型筛选以及固定排序。
- [ ] 写 `test_unsupported_city_never_reuses_zurich_inventory`，用真实临时 schema 验证 Zürich 有结果而 Wien 不会取 Zürich 行。验证时间窗边界及 ISO 投影往返。
- [ ] 运行 `& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_hotel_inventory.py -v`，RED 应由缺少模块或仓储方法导致。
- [ ] 实现查询。先限制支持的城市。按日期和正库存选取同一酒店、房型和币种的行，仅保留去重晚数等于日期窗口长度的组合。按币种、总价、酒店 ID、房型排序，最多 5 条。
- [ ] 复跑同一命令，期望通过并清理临时 schema。提交 `feat: query complete hotel stays from demo inventory`。

## Task 2 · 隔离酒店角色与只读工具

**文件**　新增 `flight_role_validation/hotel.py`、`flight_role_validation/tests/test_hotel_role.py`；扩展 `flight_role_validation/web_worker.py`。

**接口**　`register_hotel_tool(registry: ToolRegistry, repository: BusinessRepository, query: HotelQuery) -> None`；`run_hotel_role(query: HotelQuery, *, model: BaseChatModel, repository: BusinessRepository, context: ToolContext, audit_sink: AuditSink, deadline: float) -> HotelRoleResult`，状态为 `hotel_results | no_availability | unavailable`，携带投影选项。角色进程增加 `hotel` 操作，输入为宿主生成的 `HotelQuery`、用户、线程、运行标识和工作预算。现有 `flight` 交接输出增加独立的线程审计支持。

- [ ] 写 `test_model_only_sees_hotel_query_and_tool`，捕获模型输入及工具列表，确认没有航班编号、历史、身份、机场工具，酒店工具返回 37000 CHF 能原样交付。
- [ ] 写 `test_changed_query_is_audited_without_inventory_read`，模型试图改变到达日期或添加冲突条件时仓储调用为零且审计拒绝。写 `test_timeout_is_distinct_from_empty_inventory` 区分 `unavailable`、`no_availability`，限制角色调用轮数。
- [ ] 运行 `& ./flight_role_validation/.venv/Scripts/python.exe -B -m unittest discover -s flight_role_validation/tests -p test_hotel_role.py -v`，RED 应由酒店角色尚未实现导致。
- [ ] 实现最小酒店角色。唯一工具 `search_hotel_inventory` 使用已绑定查询，执行前校验工具信封，工具回执与调用 ID 可核验。通过当前 worker 以脚本模型运行，保留既有操作协议。
- [ ] 复跑同一命令及角色全量，确认零付费请求。提交 `feat: execute scoped hotel recommendations in role worker`。

## Task 3 · 父图交接与澄清检查点

**文件**　修改 `customer_support_chat/app/workflow/hotel_handoff.py`、`parent_graph.py`、`checkpoints.py`（后二者同目录）；新增 `tests/test_hotel_handoff_graph.py`。

**接口**　`resolve_hotel_turn(content: str, pending: HotelPending | None) -> HotelTurn`；`HotelPending` 为内部 JSON 兼容结构，包含等待字段 `reference | nights`、已有约束和最小已核验出处。定义 `FlightLookup(user_id: UUID, thread_id: UUID, reference: str) -> Awaitable[VerifiedArrival | None]`，其中 `VerifiedArrival` 带航班编号、城市、机场、当地日期、时区和工具调用 ID；`HotelSearch(user_id: UUID, thread_id: UUID, query: HotelQuery) -> Awaitable[HotelRoleResult]`。角色适配器的已知超时或故障抛出共享 `RoleUnavailable`，父图捕获并完成不可用结果；`None` 表示未找到本人行程。用可选的角色回调扩充 `build_parent_graph`、`GraphRuntime` 与 `open_graph_runtime`，事务连接创建的 saver 路径同样传递回调。结果新增可选 `reply` 与 `hotel_options`，待办独立于公开结果。

- [ ] 写 `test_midnight_arrival_asks_then_resumes_only_in_own_thread`，机场当地日期为 12 月 15 日。首轮酒店工具零调用，关闭并重开运行时后答「住 1 晚」，查 15–16 日，航班归属重新验证。航班日期变更时不按旧日期查询。
- [ ] 写 `test_reference_then_nights_and_unrelated_input_preserve_state`，验证两轮澄清、独立编号和独立数字的上下文边界、无关和拒绝消息、取消、切换航班、两项时长冲突、1–14 边界及 ISO 退房日。
- [ ] 写 `test_unverified_or_unsupported_and_worker_failure_never_search`，验证假证据、不同编号、非本人、城市未覆盖及超时。已知失败清待办并返回重发提示。
- [ ] 运行 `& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_hotel_handoff_graph.py -v`，RED 应为新分支或状态缺失。
- [ ] 实现防护之后的节点及待办持久化。按同一线程和最新已提交状态读取，用户可见文本由宿主生成。普通消息的旧状态和结果依然可解码。
- [ ] 复跑同一命令与 `test_graph_api.py`、`test_graph_checkpoints.py`。提交 `feat: checkpoint flight-to-hotel clarification in parent graph`。

## Task 4 · 正式 HTTP 和端到端验收

**文件**　修改 `web_app/app/local_tools.py`、`web_app/app/runtime.py`、`web_app/app/templates/local.html`、`scripts/verify_local_e2e.py`、`docs/RUNBOOK.md` 和 `docs/VERIFICATION.md`；扩展 `tests/test_graph_api.py` 与 `tests/test_unified_home.py`。

**接口**　沿用 `POST /api/threads/{thread_id}/messages` 和 `GET /api/threads/{thread_id}`。在酒店分支传回 `reply`、`hotel_options` 及既定状态。扩充 `run_local_tool` 的酒店负载，传递可信线程和运行 ID。通过限时进程等待和退出确认消除超时后继续运行的角色进程，保留现有 Windows 启动方式和普通工具调用契约。

- [ ] 扩充 HTTP 断言。A 可以在重启后补答并得到 15–16 日标准房；B 读与答均 404；A 请求 B 的航班得到相同不可用回复。普通、拒绝及独立航班与机场接口保持原契约。航班与酒店审计可串联到同一会话，首轮无酒店审计。
- [ ] 写超时和取消回归，断言工作进程退出、线程锁释放，下一轮可执行且无延迟写入。补充模板回归，确保显示查询回复和演示选项。
- [ ] 运行 `& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_graph_api.py -v`，RED 应为正式消息入口缺少交接结果或进程收尾。
- [ ] 接入正式 FastAPI 应用和现有网页，保持 JSON 兼容。父图截止时间覆盖全部角色调用，子进程在父图读取结果和提交检查点前结束；为清理预留时间，超时提示重发。通过现有 E2E 脚本覆盖验证和临时资源清理。
- [ ] 运行以下命令，通过后更新正式运行文档并提交 `feat: expose hotel handoff through the travel conversation`。

```powershell
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -v
& ./flight_role_validation/.venv/Scripts/python.exe -B -m unittest discover -s flight_role_validation/tests -v
& ./.venv/Scripts/python.exe -B scripts/verify_local_e2e.py
git diff --check
```

验收留痕分别记录单元、集成、正式启动和业务 E2E。核对锁文件未变、付费请求为零、库存未扣减及临时 schema 已清理。进入 Gate 4 独立评审并处置发现项，通过后再进入 Gate 5。
