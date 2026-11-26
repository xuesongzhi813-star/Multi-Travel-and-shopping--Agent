# P10 跨会话偏好与会话历史 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户可在聊天中直接保存住宿偏好，在新会话复用或管理本人偏好，并可在原会话重新查询早期酒店条件。

**Architecture:** PostgreSQL 业务表保存账号偏好，FastAPI 从认证身份调用同一仓储。LangGraph 父图在现有会话锁事务中处理显式保存指令，组装酒店查询时读取当前偏好。会话检查点保存带消息出处的显式查询条件，正式酒店工具重新查询真实本地库存。

**Tech Stack:** Python、FastAPI、Pydantic v2、PostgreSQL、Psycopg 3、SQLAlchemy Core、Alembic、LangGraph、`unittest`。

**Spec:** [2026-11-26-cross-session-preferences-design.md](../specs/2026-11-26-cross-session-preferences-design.md)，用户已批准。

## Global Constraints

- 正式开发目录 `E:/multi-agent/Multi-Travel-and-shopping--Agent`，基线 `p9-faq-hybrid-retrieval` 的 `af4308a`。计划批准后在现有正式工作树创建 `p10-cross-session-preferences`，基线或状态不符时按变更门禁报告。保留现有 `.idea/`。规格与计划随阶段交付进入正式仓库。
- 沿用 `web_app.app.runtime:app`、现有本地启动入口、两份锁和现有服务。默认验收不发收费模型请求。真实存储测试用随机 schema，退出时仅清理本次临时资源。
- 偏好值为 `room_type=standard`、`area=airport|city` 和整数 `max_total_chf_minor=1..1000000`。预算是 CHF 整段住宿总价，以瑞郎分表示上限，含等于上限。
- 归属由服务端认证身份决定。未登录为 `401`，他人会话为 `404`，错误体沿用既有脱敏结构。显式保存无需再次确认，普通条件只用于当前会话请求。偏好删除后旧聊天及旧检查点不得重建有效偏好。
- 按下列任务串行执行。每项开工前复核额度和适用依赖版本，记录失败测试原始输出，再实现并验收。每项验收后显式暂存并独立提交，再追加 `run-log.md`。Gate 4 独立审查并复跑，Gate 5 由用户决定集成方式。
- 下列命令默认使用正式仓库根环境。角色测试使用 `./flight_role_validation/.venv/Scripts/python.exe`。两套环境均加 `-B`，不交换解释器。

## Review Focus

- 并发首次设置两个不同字段，最终读回两者。任务 1。
- `true`、数值字符串、浮点及伪造 `user_id` 不得通过字段验证。任务 2。
- 带引号的保存示例、反向指令、复合动作与中途失败不得写入。任务 3。
- 已填满前五条的非 CHF 或超预算库存不得掩盖第六条合格库存。任务 4。
- 删偏好后回到旧会话进行再查，且另一查询正等待补充，仍只用有效历史和当前条件。任务 5。

## 文件结构和依赖查证

- 业务存储在 `customer_support_chat/app/business/preferences.py` 与 `preference_repository.py`，表定义加入 `business/schema.py`，由 `migrations/versions/0005_user_preferences.py` 升级。API 独立路由在 `web_app/app/preferences.py`，复用运行入口现有认证依赖。该依赖返回的 `Principal` 移到既有 `business/auth.py` 供两个模块共享。
- 保存规则和历史恢复分别放在 `customer_support_chat/app/workflow/preference_commands.py` 与 `hotel_history.py`。保留父图路由、检查点装配和 `hotel_handoff.py` 的邻近职责。酒店 SQL 继续在 `BusinessRepository.list_available_hotels`。
- 新验收复用现有随机 PostgreSQL schema、P3 图重启 fixture、角色启动和 HTTP 验收入口。原有浏览器模板可以展示父图的 `reply`。
- 版本查证清单按任务列出。每个外部概念单独用 Context7 查证后记录关键方法和锁定版本。版本无匹配时走 `spec-coding` 版本门禁。

---

### 任务 1 · 偏好迁移与用户级仓储

**文件** `migrations/versions/0005_user_preferences.py`、`customer_support_chat/app/business/{schema,preferences,preference_repository}.py`、`tests/{test_business_migrations,test_preference_repository}.py`。

**接口** `PreferenceValues` 是含三个可空字段的完整 `TypedDict`。`PreferencePatch` 为相同字段的非全量 `TypedDict`。`PreferenceRepository(settings: DatabaseSettings)` 提供 `async get(user_id: UUID, *, connection: AsyncConnection | None = None) -> PreferenceValues`、`async patch(user_id: UUID, changes: PreferencePatch, *, connection: AsyncConnection | None = None) -> PreferenceValues` 和 `async delete(user_id: UUID) -> None`。

**待查** Alembic 1.20.1 的 schema 限定迁移与升级。SQLAlchemy 2.0.43 的 schema 限定 `Table`、外键和 `CheckConstraint`。Psycopg 3.3.9 的异步连接事务、参数绑定及 `sql.Identifier`。PostgreSQL 19.0 的 `INSERT ... ON CONFLICT ... DO UPDATE RETURNING` 并发行为。

- [ ] 先写迁移/仓储测试。`test_upgrade_from_p9_preserves_data` 从 `0004_hotel_catalog` 升级，断言旧用户、历史消息和库存仍在，`user_preferences` 存在，重复升级成功。`test_database_constraints_and_empty_projection` 断言无记录时三个值为 `None`，数据库拒绝不存在的用户、无效枚举和 `0` / `1000001`。`test_concurrent_first_patch_preserves_disjoint_fields` 并行设置 `area='city'` 与预算 `40000`，断言都保留，另一账号为空。`test_clear_then_delete_twice` 断言显式清除单字段时其他字段保留，删除两次均成功。
- [ ] 运行 `./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_preference_repository.py' -v`。预期首个失败为缺少仓储模块。更新既有迁移表清单，并在实现前运行 `-p 'test_business_migrations.py' -v`，预期缺少 `user_preferences`。
- [ ] 实现 `user_preferences`，主键关联 `users.id` 并随用户删除。迁移 `upgrade() -> None` 创建表及约束，`downgrade() -> None` 删除该表。业务仓储以现有 `BookingRepository` 已验证的 Psycopg 连接方式支持可选既有事务。更新仅包含实际传入的允许字段，用原子 upsert 和 `RETURNING`。查询和删除显式按 `user_id` 过滤。
- [ ] 复跑上述两组，同用例退出 0，并运行 `./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_business_seed.py' -v`。核对临时测试 schema 清理。
- [ ] 显式暂存上述文件，提交 `feat: persist scoped travel preferences`。

### 任务 2 · 已认证的偏好管理 API

**文件** `customer_support_chat/app/business/auth.py`、`web_app/app/{preferences,runtime}.py`、`tests/test_preference_api.py`。

**接口** 从 `runtime` 将现有 `Principal(user_id: UUID, token_digest: str)` 移至 `business/auth.py`。`preference_router(identity_dependency: Callable[..., Awaitable[Principal]]) -> APIRouter` 在 `/api/me/preferences` 注册 GET、PATCH、DELETE。`runtime.create_app` 注册 `app.include_router(preference_router(get_principal))`，lifespan 设置 `app.state.preferences`。API 使用任务 1 的仓储接口。请求模型 `PreferencePatchRequest(BaseModel)` 以 `model_fields_set` / `model_dump(exclude_unset=True)` 区分省略与清空。

**待查** FastAPI 0.116.1 的路由注册、依赖注入及 204 响应。Pydantic 2.11.7 的 strict int、禁止额外字段、`exclude_unset` 与验证。HTTPX 0.27.2 的 ASGI transport 和 cookie 客户端。

- [ ] 先写 `test_requires_authentication_and_isolates_accounts`，断言三个路由未登录均 401，A 设置后三字段正确、B 为空。`test_partial_patch_null_and_empty_delete` 断言省略字段保留、显式 null 清空、两次 DELETE 都 204。`test_invalid_patch_has_no_side_effect` 分别提交 `{}`、额外 `user_id`、`true`、`"40000"`、`40000.0`、非法房型和区域，断言 422 且旧值未变。`test_unavailable_and_readiness` 断言仓储故障为 503 且响应不含底层 SQL，缺新表时就绪为 503。
- [ ] 运行 `./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_preference_api.py' -v`。预期失败因 `/api/me/preferences` 未注册，既有认证路径仍成功。
- [ ] 注册路由，所有读写从 `identity.user_id` 取主键。对仓储连接及查询错误使用现有 `error_response`，错误码为 `preferences_unavailable`。更新 `/health/ready` 表清单。
- [ ] 复跑偏好 API，并以相同命令的 `-p 'test_business_api.py'`、`-p 'test_unified_home.py'` 验收既有 API 和正式入口。
- [ ] 显式暂存上述文件，提交 `feat: expose authenticated preference management`。

### 任务 3 · 聊天明确授权与原子保存

**文件** `customer_support_chat/app/workflow/{preference_commands,parent_graph,checkpoints}.py`、`web_app/app/runtime.py`、`tests/{test_preference_commands,test_preference_chat}.py`。

**接口** `parse_preference_command(content: str) -> PreferenceCommand`，返回 `kind: Literal['ordinary', 'invalid', 'save']` 与 `changes: PreferencePatch | None`。`parse_total_chf_minor(clause: str) -> int | None` 供后续酒店查询重用，整段短语、币种或金额无效时返回 `None`。父图接受可选 `save_preferences: Callable[[UUID, PreferencePatch], Awaitable[PreferenceValues]]`。`GraphRuntime.__init__` 和 `open_graph_runtime` 增加可选 `preference_repository: PreferenceRepository | None = None`，`GraphRuntime._attached(connection)` 为父图绑定同一 Psycopg 事务下的保存回调。

**待查** CPython 3.12.10 的 `re.fullmatch` 与 NFKC 归一化。LangGraph 0.2.76 的 `StateGraph` 节点和状态更新、`langgraph-checkpoint-postgres` 2.0.25 接受外部异步连接的 saver 行为、Psycopg 3.3.9 事务回滚。

- [ ] 先写 `test_parses_only_complete_explicit_commands`。断言 `请记住：我以后住标准房，优先市区，总预算不超过 400 瑞郎` 产出 `standard/city/40000`，`请记住，以后优先标准房` 可单字段保存。普通表达继续原流程。带有保存指令的引号转述和否定、混合业务、重复冲突字段、模糊币种或总价、超过两位小数以及不可识别的余词走澄清。
- [ ] 写真实 PostgreSQL HTTP 测试 `test_save_once_without_new_approval`、`test_rejected_or_pending_never_saves`、`test_save_and_checkpoints_roll_back_together`。断言保存轮 201 和确认回复，查询 API 可见三个值且没有提案。受拒或待审批的消息不改值。制造图写入后提交前故障，断言旧偏好和旧有效检查点保持，已有原始消息按既有契约可查。重新读取状态及恢复原审批不会重放旧保存。
- [ ] 运行两个新增模块，预期失败来自缺少指令解析器和保存接入。现有测试模式为 `./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_preference_commands.py' -v`，聊天模块替换 `-p` 为 `test_preference_chat.py`。
- [ ] 实现在允许分支中识别独立 `请记住` 指令，归一化后解析完整字段清单。以 `，`、`、` 或兼容的半角逗号划分支持短语，支持单句 `我以后住标准房`、`以后优先标准房`、区域词和带瑞郎明确币种的总预算，预算解析用十进制无舍入换算成分。过滤引号转述和否定，归一化单位与边界后再写。保存或澄清分支在 FAQ、角色和执行器之前返回 `recorded` 或 `needs_clarification` 与 `reply`。用上一任务的仓储在会话锁连接写入，事务失败沿用既有图错误响应。
- [ ] 复跑两个新增模块，加跑 `test_graph_api.py`、`test_booking_api.py`、`test_faq_graph.py` 三个模块，要求退出 0。
- [ ] 显式暂存上述文件，提交 `feat: save explicitly authorized preferences from chat`。

### 任务 4 · 实时偏好与酒店总价筛选

**文件** `customer_support_chat/app/workflow/{hotel_handoff,parent_graph,checkpoints}.py`、`customer_support_chat/app/business/repository.py`、`tests/{test_hotel_inventory,test_preference_hotel}.py`、`flight_role_validation/tests/test_hotel_role.py`。

**接口** `HotelQuery` 和 `HotelPending` 增加可空 `max_total_chf_minor: int | None`。`HotelQuery.from_json(payload: dict) -> HotelQuery` 继续接受没有新字段的旧负载。`BusinessRepository.list_available_hotels(query: HotelQuery) -> list[HotelOption]` 签名保持。父图增加可选 `load_preferences: Callable[[UUID], Awaitable[PreferenceValues]]`，通过任务 3 的仓储注入。`resolve_hotel_turn(content: str, pending: HotelPending | None) -> HotelTurn` 扩展本次预算解析与澄清回复中的明确筛选字段。

**待查** SQLAlchemy Core 2.0.43 的分组聚合、`Select.having` 及 `where`、`limit` 组合。PostgreSQL 19.0 的 `GROUP BY` 和 `HAVING` 过滤语义。LangGraph 0.2.76 的节点状态；角色环境 LangChain 1.5.2 和 LangGraph 1.2.19 的版本查证针对现有工具调用集成测试。

- [ ] 先写 `test_applies_chf_budget_to_full_stay_before_limit`，两晚 18000+19000 的 CHF 报价在 37000 边界成立，在 36999 不成立。构造至少五个排序靠前的非 CHF 或不合格报价，第六个合格仍返回。`test_query_payload_accepts_legacy_and_budget` 断言旧负载读回默认 `None`、新预算往返不变，并在角色测试中确认真实工具看见预算约束。
- [ ] 写 `test_new_thread_uses_live_defaults_and_explicit_fields_win`，保存 `city/standard/40000`，新会话用这些过滤值。发送明确机场和预算覆盖时仅覆盖相关字段，无偏好写入。`test_pref_changes_during_stay_clarification_are_applied_late` 在追问晚数时修改或删除偏好，完成查询时按最新值。`test_invalid_trip_budget_does_not_fall_back_to_default` 断言模糊预算先追问并且不发酒店搜索。
- [ ] 运行 `./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_hotel_inventory.py' -v` 及 `-p 'test_preference_hotel.py' -v`，预期新字段或偏好查询行为缺失。
- [ ] 扩展查询模型。按每个字段从显式会话条件到实时偏好填值，加载偏好发生在要调用 `hotel_search` 的分支。预算 SQL 增加 CHF 限定及 `SUM(price_minor) <= max_total_chf_minor`，保留整段有库存校验。通过现有 `HotelQuery.to_json/from_json` 跨根/角色环境传递预算。
- [ ] 复跑两个模块及 `test_hotel_handoff_graph.py`。运行 `./flight_role_validation/.venv/Scripts/python.exe -B -m unittest discover -s flight_role_validation/tests -p 'test_hotel_role.py' -v`，要求退出 0。
- [ ] 显式暂存上述文件，提交 `feat: apply current travel preferences to hotel searches`。

### 任务 5 · 同会话历史恢复与来源校验

**文件** `customer_support_chat/app/workflow/{hotel_history,hotel_handoff,parent_graph}.py`、`tests/{test_hotel_history,test_graph_api}.py`。

**接口** `HotelHistory(TypedDict)` 保存 `request: HotelPending` 与 `source_messages: dict[str, str]`。来源键限定 `reference`、`nights`、`check_out`、`area`、`room_type`、`max_total_chf_minor`，值为 UUID 字符串。`GraphState.last_hotel_query: HotelHistory` 可缺省。活跃 `HotelPending` 保存可选 `source_messages`，持久历史的 `request` 只含明确业务字段。`parse_hotel_replay(content: str) -> HotelReplay | None` 返回 `HotelReplay(changes: HotelPending | None)`，`None` 返回值表示普通消息，`changes=None` 表示无效的再查指令。`load_hotel_history(history: HotelHistory | None, user_id: UUID, thread_id: UUID, load_message: MessageLoader) -> HotelHistory | None` 为异步校验函数，按现有 `MessageLoader` 回查去重来源。`MessageLoader` 仅作为类型导入以避免父图循环依赖。

**待查** LangGraph 0.2.76 的增量状态写入与缺省字段，`langgraph-checkpoint-postgres` 2.0.25 跨应用重启读取检查点，CPython 3.12.10 `TypedDict` 与 UUID 解析。

- [ ] 先写 `test_replay_sources_are_owned_and_present`，对旧检查点无字段、缺少消息、非法来源 UUID、跨用户和跨会话的来源均返回 `None`。`test_failed_search_preserves_history_and_no_stock_updates_it` 断言 `hotel_results` 与 `no_availability` 更新，`unavailable` 或超时保留旧记录。`test_replay_replaces_pending_without_mixing_and_bad_replay_preserves_it` 断言有效再查替换活动待补状态、无效再查不替换。
- [ ] 写真实 PostgreSQL HTTP 用例 `test_replay_after_six_unrelated_turns_and_restart`。同一会话先发 DEMO-A1 航班酒店请求，再给住宿晚数，成功后插入六轮无关对话并重启应用。按历史再查，核对跨午夜后的 `2026-12-15` 到达日期、原晚数和库存重新查询。修改区域时验证仅本次覆盖。`test_replay_after_delete_never_reinstates_saved_preference` 保存市区默认值，保留旧检查点后删偏好并再查，断言不继续使用市区默认值。B 对 A 的会话再查返回 404。
- [ ] 运行 `./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_hotel_history.py' -v` 与 `-p 'test_graph_api.py' -v`，预期新增用例因未识别再查或缺少结构化历史失败。
- [ ] 实现历史结构、按明确字段更新来源和同会话原文校验。保留明确住宿条件及适用的来源，在 `hotel_results` / `no_availability` 分支持久化。再查时从复制的历史请求开始，使用当前明确字段覆盖，晚数或退房日期更新时清除冲突的旧住宿长度约束。缺失来源先提示重新给条件，不清除活动待补。沿用既有航班核验和酒店库存路径，不把历史自动补入普通新会话。
- [ ] 复跑两个模块和 `test_graph_checkpoints.py`、`test_hotel_handoff_graph.py`、`test_faq_api.py`。确认带 P9 旧检查点格式的回归退出 0。
- [ ] 显式暂存上述文件，提交 `feat: restore sourced hotel constraints in the same conversation`。

### 任务 6 · 本机 HTTP 验收和运行指南

**文件** `scripts/verify_local_e2e.py`、`tests/test_verify_preference_e2e.py`、`docs/{RUNBOOK,VERIFICATION}.md`、批准的规格与本计划。

**接口** `verify_preferences(client: httpx.Client, password_a: str, password_b: str) -> None`，仅从既有 `run(live=False)` 的正式应用验收路径调用。命令退出码沿用现有语义。验收脚本可复用 `server` 启动、`login`、`require` 与临时资源清理逻辑。

**待查** HTTPX 0.27.2 的同步 `Client`、JSON `PATCH` 和 HTTP Cookie。FastAPI 0.116.1 的应用生命周期。CPython 3.12.10 的现有子进程和临时资源接口。

- [ ] 先写 `test_verifier_rejects_missing_preferences_route`，使用 `httpx.MockTransport` 为登录返回带有效 `X-Request-ID` 的 204，为首次 `GET /api/me/preferences` 返回 404，断言 `verify_preferences()` 抛出包含 404 的 `RuntimeError`。运行 `./.venv/Scripts/python.exe -B -m unittest discover -s tests -p 'test_verify_preference_e2e.py' -v`，预期因新增验收函数缺失失败。
- [ ] 实现离线正式 HTTP 两账号流程。A 登录后先确认初始偏好为空，随后通过 `请记住` 保存并检查确认回复。验证 B 的偏好为空、A 新会话使用房型区域与预算、修改有效、删除后原会话再查无旧默认值。用临时账号登录，`--live-deepseek` 分支不新增调用。复跑验收函数测试，要求退出 0。
- [ ] 更新运行指南，使用中文展示保存、短期覆盖、修改、删除、原会话再查及状态码。更新验收记录并保存已批准的规格与计划。
- [ ] 从正式根执行 `./.venv/Scripts/python.exe -B scripts/check_local_runtime.py check`、`./.venv/Scripts/python.exe -B -m unittest discover -s tests`、`./flight_role_validation/.venv/Scripts/python.exe -B -m unittest discover -s flight_role_validation/tests`、`./.venv/Scripts/python.exe -B scripts/verify_local_e2e.py`。串行执行，保存各命令原始退出和输出。脚本断言 P10 HTTP 和已有 P6–P9 业务通过，供应商请求为 0，核对本次临时资源已清理。
- [ ] 显式暂存上述文件，提交 `test: verify travel preferences through the local HTTP runtime`。

## Gate 4 与 Gate 5

独立审查检查账号边界、聊天保存事务、偏好删除、旧检查点读取、预算 SQL 和 FAQ 隐私。每项有效发现先加失败测试再修复。对评审后 HEAD 重跑任务 6 四条命令和 `git diff --check af4308a..HEAD`。检查依赖锁、Git 提交和暂存范围，记录基线、提交清单、测试结果及资源清理。合并或推送仅按用户在 Gate 5 的明确决定执行。
