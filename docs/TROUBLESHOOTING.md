# 本地故障排查

先记下浏览器提示里的请求 ID，定位启动窗口中同一条记录。提交问题时带路由名、状态码与错误分类。省略密钥、密码、Cookie、完整请求体和私有数据。

## 依赖缺失、Poetry 安装失败或 Docker 启动失败

运行 `py -3.12 --version`、`poetry --version`、`docker compose version`、`docker info --format '{{.ServerVersion}}'`。修复 PATH 后重开 PowerShell。Docker 引擎不可访问时确认 Docker Desktop 已运行且当前账号有权限。下载失败检查锁定包源、镜像仓库和网络。再次运行 `run-local.ps1 -Action Prepare`，确认迁移、种子及检查点均输出 `PASS`。Poetry 旧元数据弃用警告可按退出码区分。

## 端口占用或首页 404

用 `Get-NetTCPConnection -LocalPort 8000 -State Listen` 检查监听进程。使用 `-Port 8001` 启动并访问对应端口。数据库、Redis、Qdrant 的端口冲突在 `.env` 中配置，涉及既有容器和数据卷时先核对 Compose 项目及容器归属。若只有旧 API 首页返回 404，核对终端目录和启动入口。正式入口同时提供根路径与健康检查。

## PostgreSQL 连接失败、配置缺失或迁移失败

运行 `docker compose --env-file .env -f compose.local.yml ps -a` 和 `docker compose --env-file .env -f compose.local.yml logs --tail 100 postgres`。检查 `POSTGRES_PORT`、库名、用户名、密码和两个 schema 名。卷已有数据时沿用原凭据，保留现有卷。运行准备命令并确认 `business_database`、`graph_checkpoints` 为 `true`。若收到 `database_unavailable`，记录请求 ID 和错误类型。现有写操作在数据库事务中执行，连接或语句失败时相应事务回滚。

## Redis 或 Qdrant 不可用

`health/ready` 的可选依赖项显示 `false`，应用可能返回 `degraded`。查看 `docker compose --env-file .env -f compose.local.yml logs --tail 100 redis qdrant`。恢复容器后执行 `docker compose --env-file .env -f compose.local.yml exec -T redis redis-cli ping`，以及 `Invoke-WebRequest http://127.0.0.1:6333/readyz -UseBasicParsing`。若端口已修改，替换为对应端口。恢复后检查两项均为 `true`。

## 登录失败或 Agent 无响应

401 时核对账号及该业务 schema 首次导入的密码，重新执行种子会保留已有账号密码。409 表示会话已有运行中的轮次，稍后重新读取会话。503 `graph_unavailable` 时检查检查点与 PostgreSQL。如果响应含 `message_id`，该消息可能已先保存，重新查询会话状态后再决定是否提交新轮次。普通会话消息当前只产生记录或拒绝；航班查询请从统一网页的本人航班区调用。

## DeepSeek 密钥、限流、超时与预算

`deepseek` 模式缺少 `DEEPSEEK_API_KEY` 会启动失败。先将密钥读入当前进程或保存到 Git 忽略的本机 `.env`，在命令或日志里避免打印。`provider_error` 时检查模型访问权限和网络，`provider_rate_limit` 时遵循提供商额度，`model_budget_exhausted` 表示当前 API 进程的运行预算已用完，`worker_timeout` 表示工作进程耗尽时间预算。模型传输关闭自动重试与重定向，避免重复扣费。日常运行可切回 `direct` 后重启。获授权时通过 RUNBOOK 的单次真实模型脚本复验。

## MCP 或工具进程失败

查看 `role_worker` 是否为 `true`。缺少虚拟环境时运行准备命令。`mcp_unavailable`、`mcp_discovery_failed` 或 `mcp_timeout` 时，用本机解释器运行 `.\flight_role_validation\.venv\Scripts\python.exe -B -m unittest discover -s flight_role_validation/tests -p test_mcp_tools.py`，再通过主应用查询 `ZRH`。独立机场调用故障返回 503 或 504。组合航班查询会保留核验的航班并明确标记机场查询失败。合法但样本未收录的代码返回 `not_found`。
