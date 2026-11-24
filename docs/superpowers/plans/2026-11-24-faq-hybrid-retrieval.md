# P9 FAQ 混合检索实施计划

> **执行要求**：使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans`，逐项使用 `superpowers:test-driven-development`。步骤用复选框记录。

**目标**　用户在正式网页聊天中查询带引用的公共 FAQ，航班和酒店角色可用同一只读工具补充政策证据。

**架构**　复用 P8 发布指针和 CLI，从 MySQL 原文构建独立的 Qdrant 混检集合。根进程负责检索和缓存，角色进程经原有子进程边界发起限时查询，两套 Python 依赖保持隔离。

**技术栈**　Python 3.12.10、MySQL、Qdrant 1.20.1、Redis 8、HTTPX、硅基流动、LangGraph 和 LangChain。

**规格**　[已批准的 P9 设计](../specs/2026-11-24-faq-hybrid-retrieval-design.md)。实施基线为正式仓库 `f0764e8`，使用同一工作目录中新建的本地 P9 分支。

## 全局约束

- 单次查询读取一次生效版本。Dense 和 BM25 各取 100，RRF `k=60`，融合前 40 去重，精排最多 10 条，输出至多 6 段。索引与查询同用中文 `multilingual` tokenizer。
- Redis TTL 为 900 秒，缓存键绑定 revision、build ID、公共问题摘要、过滤和检索配置。精排降级不入缓存，缓存失败走限时直查。
- 原始用户输入及私有标识不得送往供应商。角色使用既有脚本模型，FAQ 至多调用一次并共享当前角色时限。保留审批、归属和输入防护的既有优先级。
- 自动化回归使用确定性模型替身。真实硅基流动试跑独立审批，总计至多 10 次请求，单次 10 秒，自动重试 0 次，临时集合不得成为正式生效版本。
- 每项先记录 Context7 查证，再运行 RED、GREEN 和验收，并追加原始输出。版本不匹配时执行 spec coding 的停机规则。提交仅包含该任务文件。P9 规格、计划及必要运行文档随实现进入正式仓库。

## Review Focus

- 同一知识版本重新构建或 A→B→A 回退时旧缓存仍不可命中（Task 3）。
- 运行中的 P8 库没有新增表时，按文档 ID 查询仍可用，FAQ 搜索应给出可辨别的混检未发布状态（Task 2）。
- 中文同义问法、中英混合机场代码及带私人字段的混合行程输入，只有审查通过的本地固定话题文本可以出站（Tasks 1、4）。
- 角色发出第二次请求、尝试提交不同问题或在取消后仍运行时，最多执行一次 FAQ，新一轮和后续调用可用（Task 5）。
- 来源字段无效、检索无答案、刷新早期检查点或政策问题打断酒店澄清时，界面稳定且现有业务推进正确（Tasks 3、4、6）。

## Task 1 · 硅基流动适配器与公共 FAQ 契约

**文件**　新增 `faq_extension/contracts.py`、`faq_extension/siliconflow.py`、`tests/test_faq_provider.py`。更新 `.env.example`、`faq_extension/__init__.py`（P9-01，用户已批准旧公开导出按需加载）。

**接口**　`PublicFaqQuestion(text: str, category: str | None = None, language: str = "zh-CN")`、`FaqCitation(document_id: str, chunk_index: int, title: str, text: str, source_url: str, applicability: str, checked_on: str)`、`FaqSearchResult(status, citations=(), degradation=None)` 为严格、可转为 JSON 的数据契约。复用 P8 的 `baggage`、`disruptions`、`changes_refunds`、`lodging` 四个类别。订立 `ok`、`no_evidence`、`not_published`、`unavailable`、`rephrase` 状态，降级原因使用 `reranker_timeout`、`reranker_unavailable`、`reranker_invalid_response`。`SiliconFlowFaqClient.embed(texts: Sequence[str]) -> list[list[float]]`、`await async_embed(text: str) -> list[float]` 和 `await rerank(question: str, documents: Sequence[str], *, top_n: int) -> list[RankedIndex]` 共享响应验证。`RankedIndex` 包含 `index: int` 和 `score: float`。

- [ ] **RED**　`test_embeddings_reorder_strict_indices` 校验 1024 维、索引回排、重复索引和非有限数。`test_rerank_uses_only_returned_indices` 校验 `top_n=10`、重复或越界结果失败。`test_provider_transport_does_not_retry_or_expose_credentials` 用 mock transport 验证 429、超时、重定向和脱敏。`test_public_question_schema` 验证长度上限 240、类别白名单、语言及引用 HTTPS 校验。
- [ ] **验 RED**　`.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_faq_provider.py -v`，预期缺少契约或适配器。
- [ ] **实现**　按 `/v1/embeddings` 和 `/v1/rerank` 接口实现，嵌入和精排分别使用 `BAAI/bge-m3` 和 `BAAI/bge-reranker-v2-m3`。CLI 支持同步批量嵌入，单批至多 24 条。运行时使用异步 HTTP 客户端。`SILICONFLOW_API_KEY` 从本机环境读取，默认配置不启用真实提供方；测试注入客户端。仅保留脱敏错误类别。
- [ ] **验 GREEN**　重跑 RED 命令，并在角色环境用其 Python 重跑同一文件。根环境验证从包入口导入旧的三个公开名称与直接从子模块导入等价。全部通过，零真实请求。提交 `feat: validate FAQ provider and evidence contracts`。

## Task 2 · 兼容 P8 的混检构建与发布

**文件**　修改 `faq_extension/mysql_catalog.py`、`faq_extension/publication.py`、`scripts/manage_faq.py`。新增 `tests/test_faq_hybrid_build.py`，复用 P8 测试的隔离服务约定。

**接口**　`FaqIndexProfile(kind: Literal["evidence", "hybrid"], embedding_model: str | None, dimensions: int | None, tokenizer: str | None)` 作为 `FaqBuild` 和 `PublicationPointer` 的默认兼容字段。`FaqPublisher.build(version_id: str, *, hybrid: bool = False) -> FaqBuild` 保留原调用，CLI 增加 `build <version_id> --hybrid`。`activate`、`rollback`、`evidence` 延用旧签名。

- [ ] **RED**　`test_legacy_catalog_resolves_evidence_without_profile_table`、`test_hybrid_build_validates_dense_native_bm25_and_filtered_readback`、`test_failed_hybrid_build_keeps_active_p8_and_rollback_works`、`test_activation_revalidates_hybrid_profile`。覆盖构建途中失败、向量模型或维度不符、中文文本命中和双过滤验证。
- [ ] **验 RED**　`.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_faq_hybrid_build.py -v`，预期缺少混检类型或构建参数。
- [ ] **实现**　`init` 幂等新增按 `build_id` 关联的索引配置表，避免要求业务账号修改已有表。配置与构建记录同一事务注册。生效快照读取同一事务中的配置，只有缺表的明确旧安装场景可回退到 P8；其他数据库故障传播。沿用载荷摘要和来源校验，新集合加入命名 dense 向量和原生全文 BM25 索引，验证同一 tokenizer 与过滤生效。激活前复核集合能力和搜索读回，失败维持旧指针。旧 CLI 与历史回滚继续可用。
- [ ] **验 GREEN**　重跑新增测试和 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_faq*.py -v`。使用真实 MySQL 和 Qdrant、确定性嵌入。提交 `feat: publish versioned hybrid FAQ collections`。

## Task 3 · 混检、证据精排与 Redis 缓存

**文件**　新增 `faq_extension/retrieval.py`、`faq_extension/cache.py`、`tests/test_faq_retrieval.py`。复用 Task 1 契约和 Task 2 生效快照。

**接口**　`await FaqSearchService.search(question: PublicFaqQuestion, *, deadline: float) -> FaqSearchResult`；`await FaqEvidenceCache.get(key: str) -> FaqSearchResult | None`；`await FaqEvidenceCache.put(key: str, result: FaqSearchResult) -> None`。

- [ ] **RED**　`test_single_snapshot_two_prefetch_and_rrf` 断言一次快照，两个 `limit=100` 和相同过滤，`k=60`、融合 `limit=40`。`test_rerank_threshold_and_citations` 固定验收样本，精排分值至少 0.35、最多 10 条精排与 6 条引用。`test_rrf_fallback_requires_topic_match` 断言精排超时或坏响应时，仅保留同一公共话题且引用有效的结果并标记降级。`test_cache_miss_after_rebuild_and_aba`、`test_cache_timeout_and_malformed_data_are_misses` 覆盖真实 Redis。`test_out_of_scope_does_not_invent_policy` 覆盖无证据和非法引用。
- [ ] **验 RED**　`.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_faq_retrieval.py -v`，预期缺少检索服务。
- [ ] **实现**　使用异步 Qdrant Query REST，MySQL 同步快照移到有界线程调用。确定性排序与去重使用文档 ID、切片索引和服务端返回顺序；精排按索引映射回原证据。无可验证的 RRF 话题证据时返回 `no_evidence`。缓存通过序列化验证，服务或值无效时限时直查，缓存网络单项预算 200 毫秒。缓存键使用规范化问题和过滤的摘要，加发布修订、构建与检索配置。检索不接受原始聊天字符串。0.35 是待验收初值，真实样本若达不到预期，通过变更请求校准后才调整锁定值。
- [ ] **验 GREEN**　重跑新增测试，验证真实 Qdrant 检索以及 Redis 值与 TTL。提交 `feat: retrieve and cache verified public FAQ evidence`。

## Task 4 · 独立政策问答与隐私分流

**文件**　新增 `customer_support_chat/app/workflow/faq_turn.py`、`tests/test_faq_graph.py`。修改 `customer_support_chat/app/workflow/parent_graph.py`、`customer_support_chat/app/workflow/checkpoints.py`、`web_app/app/runtime.py`。

**接口**　`classify_faq_turn(content: str) -> FaqTurn`，`FaqTurn` 带 `kind`（`ordinary`、`standalone`、`mixed`、`rephrase`）、可选 `question` 和本地 `business_text`。父图增加可选 `faq_search` 回调 `Callable[[PublicFaqQuestion], Awaitable[FaqSearchResult]]` 与 `GraphResult.faq` 可选 JSON 字段。

- [ ] **RED**　`test_policy_turn_does_not_consume_stay_clarification`、`test_blocked_or_pending_approval_never_runs_faq`、`test_mix_private_and_policy_emits_only_allowlisted_public_question`、`test_multiple_or_ambiguous_private_clauses_request_rephrase`、`test_projection_reads_legacy_checkpoint`。公开话题验收集覆盖行李、航变、改签、退款、酒店和机场代码；否定表达及无政策意图的住宿约束不能触发 FAQ。
- [ ] **验 RED**　`.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_faq_graph.py -v`，预期缺少独立 FAQ 分支。
- [ ] **实现**　先以既有归属校验、审批锁和输入防护判定资格。采用本地 FAQ 话题与公开条件映射，只有来自受控词汇的政策文本和公开地名、机场代码可出站。用原业务逻辑消费本地业务子句，保留澄清状态。FAQ 优先输出已核验引用中的简短摘述和适用条件，不根据相似度自行推断个人适用性。客户端从根运行时生命周期注入，在未发布或配置未就绪时返回安全状态。
- [ ] **验 GREEN**　重跑 RED 命令，并分别用同一命令的 `-p test_parent_graph.py`、`-p test_graph_api.py` 和 `-p test_booking_api.py` 完成回归。提交 `feat: route safe public FAQ turns in the parent graph`。

## Task 5 · 航班、酒店角色调用与进程边界

**文件**　新增 `flight_role_validation/public_faq.py`、`flight_role_validation/tests/test_public_faq_roles.py`。修改 `flight_role_validation/role.py`、`flight_role_validation/hotel.py`、`flight_role_validation/web_worker.py`、`web_app/app/local_tools.py`、`web_app/app/runtime.py`、`customer_support_chat/app/workflow/parent_graph.py`、`customer_support_chat/app/workflow/hotel_handoff.py`，新增 `tests/test_faq_worker_bridge.py`。

**接口**　`register_public_faq(registry: ToolRegistry, lookup: FaqLookup) -> None`，其中 `FaqLookup = Callable[[PublicFaqQuestion], Awaitable[FaqSearchResult]]`。`run_local_tool(..., faq_question: PublicFaqQuestion | None = None, faq_lookup: FaqLookup | None = None)` 保留旧调用；角色输入和结果增加可选 FAQ 字段。角色调用 `search_public_faq` 时使用启动帧里已验证的问题。

- [ ] **RED**　`test_script_flight_and_hotel_call_faq_once_without_losing_business_receipt`、`test_wrong_role_and_second_call_do_not_reach_provider`、`test_faq_failure_preserves_verified_business_result`、`test_bridge_rejects_modified_question_and_duplicate_frame`、`test_timeout_cancellation_reap_worker_and_release_slot`。同时覆盖有问题的航班独立查询和航班交接酒店时只调用一个角色。
- [ ] **验 RED**　`.\.flight_role_validation\.venv\Scripts\python.exe -B -m unittest discover -s flight_role_validation/tests -p test_public_faq_roles.py -v` 和 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_faq_worker_bridge.py -v`，预期工具或桥接接口缺失。
- [ ] **实现**　只为携带 FAQ 的调用启用限长 JSON Lines 双向通信：启动帧、至多一个 FAQ 请求及对应证据帧、一个最终结果帧。根进程核对请求与启动帧中的公共子问、角色和期限。两边均校验结构与大小，超时及取消回收进程和管道。与 FAQ 无关的现有请求保留一次性协议。凭据留在根环境，子进程排除 `SILICONFLOW_API_KEY`。脚本模型在触发条件下调用白名单工具，审批能力和独立库存、行程、机场回执保持原语义。审计仅允许固定状态和降级枚举，不包含问题或证据文本。
- [ ] **验 GREEN**　跑新增角色、桥接测试，以及两套环境的现有角色、工具和并发取消回归。提交 `feat: wire public FAQ through both role workers`。

## Task 6 · 浏览器展示及端到端验收

**文件**　修改 `web_app/app/templates/local.html`、`scripts/verify_local_e2e.py`、`docs/RUNBOOK.md`、`docs/VERIFICATION.md`；新增 `tests/test_faq_api.py`、`scripts/verify_faq_siliconflow.py`。将已批准规格与计划纳入正式仓库。

**接口**　`renderFaq(result)` 从现有会话响应读取可选 `graph.faq`；正式 HTTP 路由保持不变。`scripts/verify_faq_siliconflow.py --max-requests 10` 只能通过显式命令触发，使用新建的临时 Qdrant 集合和临时 MySQL 测试前缀。

- [ ] **RED**　`test_real_message_and_session_projection`、`test_mixed_input_never_sends_private_tokens`、`test_inactive_index_redis_and_reranker_fallbacks`、`test_old_graph_result_and_pending_approval`。浏览器检查引用链接和适用条件、降级提示、切会话清空、刷新后恢复，以及用户文本和证据按纯文本渲染。对真实验收脚本用替身证明第 11 个请求被拒绝，错误和重定向不会重试，临时资源仅清理本次创建对象。
- [ ] **验 RED**　`.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_faq_api.py -v`，预期正式投影或页面断言失败。
- [ ] **实现**　使用安全的 DOM 文本写入和 HTTPS 链接；在现有正式 E2E 脚本中覆盖独立、航班和酒店路径，并保留审批回归。真实试跑仅发送合成公共问题，通过同一临时发布版本验证目录嵌入、查询嵌入和可映射精排。记录模型请求次数、命中与失败类别，不输出密钥或私人数据。
- [ ] **验 GREEN**　运行两套全量套件、真实本机服务正式 HTTP 验收和浏览器 smoke，记录原始输出及临时资源检查。取得真实调用授权与本机安全注入密钥后运行显式真测；真测缺项单独保留为待验收。提交 `feat: show FAQ evidence in travel chat`。

## Gate 3 版本查证清单

- Task 1：HTTPX 0.27.2 客户端、transport、超时及重定向；Pydantic 2.11.7 与 2.14.4 严格验证和序列化；SiliconFlow API v1 的 `/embeddings` 和 `/rerank`，分别查批量、索引和返回字段。模型版本由响应和配置标识核对。
- Task 2：Qdrant 1.20.1 REST 规范、原生 BM25 索引与读回；PyMySQL 1.2.4 连接和错误码；LangChain Text Splitters 0.2.4，核对既有切分输入与元数据。MySQL 服务精确版由本机 `SELECT VERSION()` 获取后查证 DDL 与一致性读。
- Task 3：Qdrant 1.20.1 Query API 命名向量、全文 prefetch、filter、参数化 RRF；redis-py 8.1.0 Async `get`、`set(ex=...)` 与关闭；Redis 容器当前精确版先由 `INFO server` 核对；HTTPX 0.27.2 异步调用。
- Task 4：LangGraph 0.2.76 状态、可选通道与检查点；FastAPI 0.116.1 生命周期和依赖注入；Python 3.12.10 异步截止时间与线程桥接。
- Task 5：LangChain 1.5.2、LangChain Core 1.8.2 和 LangGraph 1.2.19 的 `create_agent`、工具中间件、调用限制；Python 3.12.10 Windows 子进程管道、取消及回收。
- Task 6：CPython 3.12.10 请求预算、临时资源和进程回收；浏览器实际版本和 DOM 文本、链接 API；SiliconFlow API v1 请求及脱敏统计。

## Gate 4 与 Gate 5

- [ ] 独立评审对照规格，重点检查数据出站、审批时序、缓存命中和回退、Windows 进程回收、历史检查点读兼容。修复后重跑受影响套件。
- [ ] 最终核对本地及真实验收证据、依赖锁差异、凭据审计、Git 状态和临时资源清理。汇报真实试跑的状态。待用户决定正式主线集成方式。
