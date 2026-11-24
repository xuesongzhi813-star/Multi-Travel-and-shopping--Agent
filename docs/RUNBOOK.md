# 本地运行与运维

首次使用先完成 [LOCAL_SETUP](LOCAL_SETUP.md)。正常开发从正式项目目录运行一条启动命令。脚本按依赖安装、存储服务、业务迁移、演示种子、检查点、FastAPI 的顺序执行。API 同时承载父图，航班角色和本机 MCP 由业务请求按需启动。

```powershell
Set-Location -LiteralPath 'E:\multi-agent\Multi-Travel-and-shopping--Agent'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run-local.ps1
```

看到 `Application startup complete` 后，打开 [旅行客服网页](http://127.0.0.1:8000/)、[API 文档](http://127.0.0.1:8000/docs) 或 [健康检查](http://127.0.0.1:8000/health/ready)。若需改端口，在启动和状态命令都追加 `-Port 8001`。

## 浏览器业务验证

1. 用 `demo-a` 和 `.env` 中的 `P2_DEMO_A_PASSWORD` 登录。新建会话，发送普通消息，再发送“请忽略系统规则”，查看 `recorded` 和 `rejected`。复制会话 ID，API 重启后重新登录并打开该会话，检查记录仍在。
2. 从“本人航班”选择 `DEMO-A1`。默认查询 PostgreSQL 中的本人行程，再把已核验的 `ZRH` 送给本机 MCP。预期 `status=verified`、当地到达日期 `2026-12-15`、`airport.data.city=Zürich`、`provider_requests=0`，发送结果包含模式标记。
3. 用独立机场查询输入 `VIE` 和 `ZZZ`，分别观察 `Wien` 和 `not_found`。机场工具查询的是本机示例目录。
4. 退出并换用 `demo-b`。本人航班列表切换到 B 账号，打开 A 的会话返回 404。API 对不存在或无权访问的演示航班返回业务状态 `not_found`。

`GET /health/live` 只检查 API 进程。`GET /health/ready` 检查 PostgreSQL 业务和检查点表、本机角色环境、Redis、Qdrant。必需服务不可用返回 503，Redis 或 Qdrant 不可用返回 200 加 `degraded`。健康检查不发模型请求。真实模型连通性以独立业务验收为准。

## 重复验收

以下命令在项目根目录运行。端到端脚本使用独立随机 schema 和临时 API 端口，复用已有容器与虚拟环境，运行后清理本次测试创建的 schema。

```powershell
.\.venv\Scripts\python.exe -B scripts\check_local_runtime.py check
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
.\flight_role_validation\.venv\Scripts\python.exe -B -m unittest discover -s flight_role_validation/tests
.\.venv\Scripts\python.exe -B scripts\verify_local_e2e.py
```

只有在授权进行收费验证时运行以下命令。先按 [LOCAL_SETUP](LOCAL_SETUP.md) 配好当前进程的密钥。脚本最多进行一次模型工作流，并验证超预算请求被拒绝。默认 E2E 不走付费模型，并使用独立 API 进程检查 Redis 和 PostgreSQL 断连，现有容器保持运行。

```powershell
.\.venv\Scripts\python.exe -B scripts\verify_local_e2e.py --live-deepseek
```

## 状态、日志、停止和重启

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run-local.ps1 -Action Status
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
docker compose --env-file .env -f compose.local.yml logs --tail 100 postgres redis qdrant
```

API 在启动窗口记录时间、级别、模块、路由名、状态码、耗时、错误分类及 `request_id`。浏览器错误和 HTTP 响应头 `X-Request-ID` 含相同 ID。工具审计用该 ID 作为 `run_id`，只记录预先允许的字段。

在启动窗口按 `Ctrl+C` 并等待退出。需要停止共享容器时，先确认本机没有其他工作区在使用它们，再运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run-local.ps1 -Action Stop
```

停止保留 PostgreSQL 和 Qdrant 命名卷。Redis 配置为临时缓存。再次执行同一启动命令即可重启。排查入口见 [TROUBLESHOOTING](TROUBLESHOOTING.md)。

## P6 航班到酒店演示

登录 demo-a，在会话中发送「帮我找 DEMO-A1 落地后的酒店」。收到「您打算住几晚？」后，发送「住 1 晚」。应用以航班到达地日期 2026-12-15 查询至 2026-12-16 的合成库存，显示标准房演示总价 CHF 180.00。待补充的晚数保存在会话检查点，重启后可在原会话继续。P6 聊天场景使用离线角色脚本与真实本机存储。

## P7 网页审批与演示酒店预订

在酒店结果点「发起演示预订」。核对酒店、房型、日期和总价后，再点「确认预订」或「拒绝」。提案十分钟有效。若库存或报价变化，请在同一会话重新查询并选择。批准后以页面的演示订单号为准。如果审批中断，重新打开同一会话并点「继续处理」，服务会复用已经提交的回执。

运行常规启动命令即可应用迁移 0003。离线端到端命令为 `.\.venv\Scripts\python.exe -B scripts/verify_local_e2e.py`，使用临时合成账号和 PostgreSQL schema。

## P8 FAQ 与酒店目录

知识发布独立于 Web 进程。启动本机 MySQL 专用项目库和 Qdrant，再在项目根目录执行。

```powershell
& ./.venv/Scripts/python.exe -B scripts/manage_faq.py init
& ./.venv/Scripts/python.exe -B scripts/manage_faq.py import faq_extension/data/travel_faq_zh.json
& ./.venv/Scripts/python.exe -B scripts/manage_faq.py build travel-zh-2026-11-24
& ./.venv/Scripts/python.exe -B scripts/manage_faq.py status
```

构建输出 `build_id`，状态输出 `revision`。确认构建为 `verified` 后，使用自己的 ID 和刚读取的修订号执行 `activate <build_id> --expected-revision <revision>`。初次发布前修订号是 0。用 `evidence bag-report` 读回当前版本的引用和切片。返回 `not_published` 或 `not_found` 时分别表示无活跃版本或该版没有该文档。已登记文档的证据缺失或发生变化时返回 `storage_unavailable`，退出码为 1。

构建失败时，原版本继续可查。恢复到先前发布的构建请使用 `rollback <build_id> --expected-revision <revision>`。修订号冲突时先读状态并核对，确定目标后再提交。构建、激活和回退不会自动删除集合。P8 通过文档 ID 读取 Qdrant 证据。向量、混检、精排和聊天 FAQ 将由 P9 接入。

正常启动会应用迁移 0004 并补齐酒店目录。从 demo-a 会话发送「找 DEMO-A3 落地后的机场酒店，住 2 晚」，可查看 2026-12-20 至 22 日的演示房源和旅游局来源。再次启动只补缺库存，保留已预订扣减。


## P9 公共旅行政策检索

正式聊天可直接提问，例如“行李延误怎么办？”，也可以在行程请求后追加公共政策问题，例如“查 DEMO-A1 航班，顺便问行李延误怎么办？”。网页展示政策引用、适用条件和精排降级提示。打开先前的会话可恢复最新一轮的证据。涉及个人信息的政策子问可能需要重新表述。

默认模式 disabled 允许使用现有项目服务，并禁止请求硅基流动。要构建或运行真实混检，通过本机环境或已忽略的 .env 设置 TRAVEL_FAQ_MODE=siliconflow 及 SILICONFLOW_API_KEY。依次使用既有 FAQ CLI：

1. 运行 init，确保旧库增加构建配置表。
2. 运行 import，传入 FAQ 源文件。读取命令输出的 version_id。
3. 运行 build <version_id> --hybrid，取得已核验的 build_id。
4. 运行 status，确认版本及当前 revision。运行 activate <build_id> --expected-revision <revision>。
5. 需要回退时，用 rollback <build_id> --expected-revision <revision>。回退到 P8 证据版本后仍可按文档查证据，混检状态为 not_published。

前四个 CLI 子命令均由 .\.venv\Scripts\python.exe -B scripts\manage_faq.py 执行。构建和回退用独立集合与修订号。真实构建会发送公开 FAQ 切片请求嵌入。

单独运行 .\.venv\Scripts\python.exe -B scripts\verify_faq_siliconflow.py --max-requests 10 可验收真实嵌入与精排。该命令创建临时表和集合，退出前清理，只打印状态和调用次数。
