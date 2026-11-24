# P8 FAQ and Hotel Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**目标**　交付 24 条中文旅行 FAQ、8 家有来源的苏黎世酒店，以及能构建、核验、激活和回退知识版本的本机工具。

**架构**　MySQL 保存不可变 FAQ 版本、构建记录和发布指针。Qdrant 存储可重建的版本化证据集合，验证通过后由 MySQL 事务控制切换。酒店事实与合成库存复用现有 PostgreSQL 和酒店角色。

**技术栈**　Python 3.12.10、PyMySQL、HTTPX、MySQL 8.0、Qdrant 1.20.1、SQLAlchemy Core、Alembic。

**规格**　[P8 FAQ 与酒店数据](../specs/2026-11-24-faq-hotel-data-design.md)。

## 全局约束

- 从正式目录 `E:/multi-agent/Multi-Travel-and-shopping--Agent` 的 `main`（`6e0f0ae`）在同一工作区切 P8 功能分支，保留原有工作树、配置、数据库卷和未提交的本机文件。
- 两份锁保持现有版本。P8 无付费调用。CLI 明确启动知识操作，正式聊天启动继续沿用 `run-local.ps1`，酒店查询继续沿用 P6 角色和 P7 审批入口。
- 测试使用现有授权 MySQL 库内随机表前缀、随机 Qdrant 集合及临时 PostgreSQL schema。清理限于本次创建的对象。正式 CLI 无自动删除集合的行为。
- 每项先保存 RED 原始输出，再实现、复验、记账和单独提交。业务数据只在合成测试或批准的正式导入路径写入。文档和证据不记录凭据、配置值或访问令牌。

## 文件与回归重点

- `faq_extension/catalog.py` 及归档 JSON 负责字段、独立数据集和稳定摘要。Task 1 验证重复 ID、来源与类别，保证重排不改变版本摘要。
- `faq_extension/mysql_catalog.py` 负责事务导入、构建状态、可回退历史与指针。Task 2 验证原子导入、不可变版本、初次并发激活和 A→B→A 后的旧修订号。
- `faq_extension/publication.py` 与 `scripts/manage_faq.py` 负责显式命令和 Qdrant 构建。Task 3 验证上传中断、核验失败、跨版本读回和已发布回退。
- 酒店归档、PostgreSQL 迁移、种子导入、角色结果及网页负责目录元数据。Task 4 验证旧 JSON、网页 URL、库存扣减保留和跨晚查询。
- Task 5 沿用正式 E2E 脚本，并验收实际 MySQL/Qdrant 的 CLI 发布与恢复，覆盖 PostgreSQL、浏览器和 P7 兼容。

## Context7 查证清单

每项先解析并按锁定版本和概念查询。精确版本未索引时按 spec-coding 版本门禁处理。

- Task 1：CPython 3.12.10 的 `json`、`hashlib`、日期与 URL 解析。资料来源按航空公司、旅游局或酒店官网核证。
- Task 2：PyMySQL 1.2.4 的绑定参数、连接和事务；MySQL Server 8.0 按实机查补丁版本，确认 InnoDB、`SELECT ... FOR UPDATE`、条件更新和 JSON 字段。
- Task 3：Qdrant 1.20.1 REST 的无向量集合、等待写入、索引、分页读取与超时；HTTPX 0.27.2；本机锁定的 LangChain 0.2.17 与 text splitters 0.2.4 的确定性切分。
- Task 4：SQLAlchemy 2.0.43 PostgreSQL INSERT 冲突策略、Alembic 1.20.1 迁移、Pydantic 2.14.4 角色结果及 Pydantic 2.11.7 根环境兼容。
- Task 5：HTTPX 0.27.2、CPython 3.12.10 子进程和临时资源；沿用现有本地服务版本。

## Task 1 · 带来源的 FAQ 语料

**文件**　新增 `faq_extension/catalog.py`、`faq_extension/data/travel_faq_zh.json`、`tests/test_faq_catalog.py`。

**接口**　`FaqDocument(id, category, language, question, answer, applicability, source_url, checked_on)`、`FaqVersion(version_id, digest, documents)` 为共享不可变值。定义 `load_faq_version(path: Path) -> FaqVersion`。对象字段为严格字符串和 ISO 日期，类别为 `baggage | disruptions | changes_refunds | lodging`，语言为 `zh-CN`。版本 ID 和文档 ID 为小写 ASCII 短标识。

- [ ] 写 `test_catalog_has_24_attributed_documents`，断言四类别各有条目、24 个唯一 ID、官方 HTTPS 来源、日期、适用条件与非空问答。写 `test_digest_is_order_independent_but_detects_content_edits` 与 `test_invalid_source_or_duplicate_documents_are_rejected`。禁止同一对象重复 JSON 键。
- [ ] 运行 `& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_faq_catalog.py -v`，确认 RED 为缺少语料或接口。
- [ ] 编写并逐条核对 24 条简体中文问答。摘要取固定字段、稳定 ID 排序、UTF-8 和固定分隔符的规范 JSON SHA-256。保留来源、核对日期及渠道、承运方等真实条件，公开事实自行表述。
- [ ] 复跑定向测试并抽样核对引用可打开。提交 `feat: add attributed Chinese travel FAQ catalog`。

## Task 2 · MySQL 不可变版本和发布指针

**文件**　新增 `faq_extension/mysql_catalog.py`、`tests/test_faq_mysql.py`。

**接口**　`MySqlFaqCatalog(settings, *, table_prefix="travel_faq")`；`initialize() -> None`；`import_version(version: FaqVersion) -> bool`；`read_version(version_id: str) -> FaqVersion`；`add_build(build: FaqBuild) -> None`；`finish_build(build_id: UUID, *, verified: bool, point_count: int, payload_digest: str | None) -> None`；`get_build(build_id: UUID) -> FaqBuild`；`active() -> PublicationPointer`；`activate(build_id: UUID, *, expected_revision: int, rollback: bool = False) -> PublicationPointer`。指针含 revision、版本、构建 ID、集合，可为空的初态修订号 0。构建含版本摘要、唯一集合名及 `building | verified | failed` 状态。

`FaqBuild(build_id, version_id, version_digest, collection, status, point_count, payload_digest)`，其中 `build_id` 为 UUID，`point_count` 在构建完成前为 0。指针为 `PublicationPointer(revision, version_id, build_id, collection)`，后三项在未发布时为空。领域错误区分未找到版本、不可变冲突、未验证构建和过期修订。构建只允许从 `building` 转为一次终态。

- [ ] 写 `test_import_is_atomic_and_same_version_cannot_change`，用实际 MySQL 随机表前缀验证首导、同内容重放、摘要冲突和注入中途失败。写 `test_cas_covers_initial_publication_and_rollback_aba`，模拟两个初次写入、A→B→A 和旧修订号拒绝，断言历史按实际顺序记录。
- [ ] 运行 `& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_faq_mysql.py -v`，确认 RED。
- [ ] 用项目账号在现有库初始化 InnoDB 表，验证所有动态标识符上限及字符集，业务值只使用绑定参数。初始化幂等，FAQ 版本加全文本同事务。发布事务锁单例指针并校验预期修订号、构建完成状态及版本绑定；回退额外要求构建曾成功发布。测试删除随机前缀的已知表。
- [ ] 复跑定向验收，提交 `feat: persist immutable FAQ versions and publication history`。

## Task 3 · Qdrant 验证构建与显式 CLI

**文件**　新增 `faq_extension/publication.py`、`scripts/manage_faq.py`、`tests/test_faq_publication.py`。

**接口**　`FaqPublisher(catalog: MySqlFaqCatalog, client: httpx.Client, *, collection_prefix="travel_faq")`；`build(version_id: str) -> FaqBuild`；`activate(build_id: UUID, *, expected_revision: int, rollback: bool = False) -> PublicationPointer`；`evidence(document_id: str) -> FaqEvidence | None`。CLI 子命令为 `init`、`import <path>`、`build <version_id>`、`activate <build_id> --expected-revision N`、`rollback <build_id> --expected-revision N`、`status`、`evidence <document_id>`。标准输出 JSON，未发布、未命中和依赖故障可区分。

`FaqEvidence` 包含当前 revision、版本、构建 ID、文档 ID 和按原序排列的切片。未发布返回 `not_published`，已发布但没有文档返回 `not_found`。CLI 成功和两种空状态退出 0，参数或领域冲突退出 2，存储故障退出 1。临时命名空间仅由测试注入，正式命令使用默认前缀。

- [ ] 写 `test_publish_failed_build_keeps_old_evidence_and_rebuild_can_rollback`。用真实 MySQL、Qdrant 创建 A，通过受控故障注入使 B 上传中断与读回不匹配，确认 A 的 ID、摘要和读取都一致，再重建 B、发布并回退 A。写 `test_same_document_rebuild_keeps_point_ids_and_a_single_pointer_snapshot`，检查稳定 ID、分页重建、并发修订号和读过程中只取一次指针。
- [ ] 运行 `& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_faq_publication.py -v`，确认 RED。
- [ ] 用现有切分器按确定顺序处理每份问答。切片 ID 依据版本、文档 ID、序号确定，保存源字段与摘要。HTTPX 以限时请求调用本机 Qdrant REST，构建独立 payload 集合，给文档 ID、类别、语言、版本建关键词索引，上传等待完成并分页核验有效载荷。核验失败保留旧指针。回退先验证目标集合仍完整，再用 MySQL CAS 写入。CLI 只使用本机已配置服务，不抓取来源 URL。
- [ ] 复跑真实存储集成并验收 CLI 输出与退出码，提交 `feat: publish and restore verified FAQ evidence collections`。

## Task 4 · 苏黎世酒店目录与合成库存

**文件**　新增 `customer_support_chat/data/zurich_hotels.json`、`customer_support_chat/app/business/hotel_catalog.py`、`migrations/versions/0004_hotel_catalog.py`、`tests/test_hotel_catalog.py`；修改既有业务 schema、`demo_seed.py`、仓储、`HotelOption`、角色 `HotelRow`、网页和必要的迁移断言。

**接口**　`load_hotel_catalog(path: Path) -> HotelCatalog`，公开字段含名称、区域、设施、来源 URL 和 `checked_on`；合成字段包含房型、夜间库存、CHF 金额和演示条款。新增酒店通过同一 `seed_demo` 入口幂等导入。酒店选项增加可选 `source_url`、`checked_on`、`demo`，旧对象缺字段仍可解析，其对外 JSON 省略这三个字段。P7 提案价格和授权维持现有字段。

- [ ] 写 `test_catalog_eight_sourced_hotels_and_demo_only_inventory`、`test_multinight_p6_results_and_reseed_preserve_booked_stock`。后者以 2026-12-20 的 P6 到达日查询两晚，验证一晚无房的酒店未返回。对可用选项完成合成预订后重跑导入，实际数据库库存、订单和回执保持一致。写旧选项 JSON 往返及网页 HTTPS 链接回归。
- [ ] 运行 `& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_hotel_catalog.py -v`，确认 RED。
- [ ] 核对 8 家官方酒店条目。迁移新增可空酒店来源列和演示标记，旧两个酒店和 12 月 15 日既有行保留。按现有主键执行只补缺插入。P6 结果显示来源及演示语义，网页仅把已验证 HTTPS URL 渲染为链接，使用文本节点展示其余字段。
- [ ] 复跑酒店查询、预订、迁移、根与角色插件相关回归，提交 `feat: add sourced Zurich hotels and synthetic stay inventory`。

## Task 5 · 正式入口端到端与运行文档

**文件**　修改 `scripts/verify_local_e2e.py`、`docs/RUNBOOK.md`、`docs/VERIFICATION.md`；正式仓库内保存本规格和计划。扩充 `tests/test_faq_publication.py` 的 CLI 验收。

- [ ] 在实际服务上从 CLI 验证导入、构建、激活、状态、读回、新版失败、回退。校验测试标识必须为本轮新生成，清理后查询确认临时表和集合已移除。
- [ ] 在正式启动和 HTTP 路径验证 `DEMO-A3` 酒店结果及来源字段、少一晚库存的过滤、P7 既有 `DEMO-A1` 预订。用浏览器核对新增酒店演示标记、来源链接和两步确认兼容。
- [ ] 跑完整验收，区分测试、正式启动、真实 HTTP 和浏览器。文档写明显式知识 CLI、故障回退、零付费请求与 P9 接续边界。提交 `docs: document and verify FAQ publishing and demo hotel data`，然后进行独立评审。

```powershell
& ./.venv/Scripts/python.exe -B -m unittest discover -s tests -v
& ./flight_role_validation/.venv/Scripts/python.exe -B -m unittest discover -s flight_role_validation/tests -v
& ./.venv/Scripts/python.exe -B scripts/verify_local_e2e.py
git diff --check
```
