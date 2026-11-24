# 整改验证记录

验证日期为 2026-11-23（Asia/Shanghai）。项目根目录为 `E:\multi-agent\Multi-Travel-and-shopping--Agent`。下列命令在该目录执行。测试使用 Windows、Python 3.12.10、Poetry 2.1.3 和 Docker Compose v5.5.0。

## 工具链、回归和真实启动

- **PASS 工具链与依赖**。执行 `py -3.12 --version`、`poetry --version`、`docker compose version` 和 `docker compose --env-file .env -f compose.local.yml ps -a`。正常启动脚本安装两份锁文件的环境，依次启动容器、迁移、导入种子和准备检查点。
- **PASS 存储集成**。`.\.venv\Scripts\python.exe -B scripts\check_local_runtime.py check` 返回 PostgreSQL、Qdrant、Redis、MySQL 全部 PASS。MySQL 使用专用数据库连接和临时表。Redis 测试写入一次有 TTL 的随机键。Qdrant 通过 readiness。此结果仅证明存储服务可连接。
- **PASS 回归**。`.\.venv\Scripts\python.exe -B -m unittest discover -s tests` 通过 44 项。`.\flight_role_validation\.venv\Scripts\python.exe -B -m unittest discover -s flight_role_validation/tests` 通过 20 项。包含单元、真实 PostgreSQL、本机 MCP 和 mock transport。回归阶段的模型请求为零。
- **PASS 真实启动**。E2E 在临时端口使用正式命令 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run-local.ps1 -Port <port>`。收到 `GET /health/ready` 的 200 和 `status=ok`，访问 `GET /`、`/docs`、`/health/live`。HTTP 请求经过真实 Uvicorn 进程。

- **PASS 用户现有配置**。用正式根目录的现有 .env 从 8000 启动，-Action Status 返回 HTTP 200 与五项依赖正常。实际用 demo-a 登录、读取行程并退出。验收退出后端口释放。
- **PASS 配置失败退出**。在子进程选择 deepseek 模式并传入空白密钥，运行标准 launcher，验证明确缺少 DEEPSEEK_API_KEY 且退出码为 1。没有模型请求。

## 业务端到端

运行 `.\.venv\Scripts\python.exe -B scripts\verify_local_e2e.py`。脚本内的业务请求清单和断言可重复执行，使用本机真实流程及合成演示数据。

- **PASS 认证和隔离**。`/api/auth/login` 验证成功和无效密码，`/api/me/itineraries` 验证登录前 401 和本人行程。另一个账号访问原会话，读写均返回 404，航班查询返回 `not_found`。
- **PASS LangGraph**。`POST /api/threads` 创建会话。向 `/api/threads/{id}/messages` 提交普通和受限消息，检查返回 `recorded` 和 `rejected`。API 退出后从同一个 PostgreSQL 检查点恢复并增加轮次。
- **PASS 航班与机场**。`POST /api/tools/flight` 联查 `DEMO-A1`，核验当地到达日期 `2026-12-15` 和 MCP 返回的 `Zürich`。使用同一 `X-Request-ID` 查询 PostgreSQL 工具审计，确认两个工具均实际执行。该流程产生零次收费调用。
- **PASS 否定用例**。`POST /api/tools/airport` 用无效参数收到 422，有效但样本未覆盖的代码收到 `not_found`。
- **PASS 真实 DeepSeek**。`.\.venv\Scripts\python.exe -B scripts\verify_local_e2e.py --live-deepseek` 通过。仅运行一次工作流，实际模型 HTTP 请求为 2 次。应用核验模型输出、调用本机 MCP 并落审计。下一次航班请求返回 429，未启动第二次模型工作流。
- **PASS 依赖故障**。用独立 API 实例连接不可达的 Redis 端口，readiness 返回 200/degraded，登录和行程仍可用。模拟不可达 PostgreSQL 端口时，readiness 返回 503、liveness 返回 200、登录返回脱敏的 database_unavailable。真实容器没有被停掉。
- **FAIL 已修复**。初次自动关闭用共享控制台事件，曾打断当时还在登录的验收，也曾令测试监听在关闭检查时仍存在。改用独立隐藏控制台，并验证端口释放；默认与模型验收都已复跑通过。
- **BLOCKED 外部业务**。WooCommerce、WordPress、GoHumanLoop 的旧入口仍需经核验的沙箱、访问权限和明确副作用边界。当前运行未调用这些服务。
- **BLOCKED 后续阶段**。FAQ 向量混合检索、跨会话偏好和质量评估尚未交付，未执行其业务验收。Qdrant 和 MySQL 探针通过不能作为 FAQ 通过依据。

E2E 仅移除该次运行创建的随机 schema。既有数据、历史工作区与本机配置保留。

## P6 航班到酒店交接

- **PASS 正式 HTTP**。运行 `.\.venv\Scripts\python.exe -B scripts/verify_local_e2e.py`。使用正式 launcher 启动，创建隔离会话发送 DEMO-A1 的酒店请求，首次只追问住宿晚数。关闭并重启服务后，demo-b 无法读取或补答，demo-a 可以补答并获得 2026-12-15 至 2026-12-16 的苏黎世演示库存。
- **PASS 回归**。验证整段库存、跨用户行程隔离、会话恢复、拒绝无关澄清、冲突房型或区域的追问、角色工具权限、审计关联以及进程超时和取消回收。HTTP 集成验证库存数量保持一致。
- **运行模式**。P6 使用脚本模型、真实本机 MCP 与 PostgreSQL。P6 验收的付费模型请求为 0。

## P7 网页审批与演示酒店预订

- **正式 HTTP**。离线端到端脚本验证拒绝、报价变化、重复批准和跨账号访问。查询实际 PostgreSQL 行，核对库存净扣减一次、订单和回执各一笔。
- **事务与恢复**。在临时 schema 验证竞争最后一间房、夜价等额互换仍作废、回执插入失败整体回滚、决定保存后和订单提交后中断时的同会话恢复。
- **浏览器**。正式页面的选房、确认前刷新、拒绝、重新发起和成功回执在独立本机 Chrome 中验证。测试使用合成账号，付费请求为零。

## P8 FAQ 与酒店数据

- **知识发布集成**。真实 MySQL 和 Qdrant 使用隔离表及集合，验证不可变导入、读回摘要、构建失败保留旧版、修订号冲突和显式回退。
- **酒店与库存**。以 DEMO-A3 的到达日期查询两晚，校验缺货跨晚过滤、网站来源和演示标记。真实 PostgreSQL 预订后重新导入，库存、订单和回执保持一致。
- **运行入口**。正式启动、实际 HTTP 和独立 Chrome 验证 P8 酒店展示及 P7 确认兼容。验收使用合成库存及临时对象，付费模型请求为零。
