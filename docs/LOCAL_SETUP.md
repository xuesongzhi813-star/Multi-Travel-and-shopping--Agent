# 本地环境安装

本项目在 Windows PowerShell 上运行 FastAPI，Docker Desktop 管理 PostgreSQL、Qdrant 与 Redis。正式工作区为 `E:\multi-agent\Multi-Travel-and-shopping--Agent`。首次安装需要可访问镜像仓库与 Poetry 配置的包源。

## 工具链与配置

安装 Python 3.12（包含 Windows `py` 命令）、Poetry 2.1 或兼容的 2.x 版本、支持 `up --wait` 的 Docker Compose。启动 Docker Desktop 的 Linux 容器引擎。本机检验使用 Python 3.12.10、Poetry 2.1.3 和 Compose v5.5.0。

```powershell
Set-Location -LiteralPath 'E:\multi-agent\Multi-Travel-and-shopping--Agent'
py -3.12 --version
poetry --version
docker compose version
docker info --format '{{.ServerVersion}}'
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
notepad .env
```

填写 `POSTGRES_PASSWORD`、`P2_DEMO_A_PASSWORD` 和 `P2_DEMO_B_PASSWORD`，两个演示账号使用不同密码。已有 PostgreSQL 卷时沿用创建时的数据库名、用户名与密码。改变 `.env` 不会重置已有卷的账号，也不会改写已导入的密码。首次安装可使用默认端口。系统环境变量优先于 `.env`，项目本地启动脚本会指定同一份根目录 `.env`。

- PostgreSQL 使用 `POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_PORT`（默认 `5432`）。`P2_DB_SCHEMA`（默认 `travel_business`）存业务数据，`P3_CHECKPOINT_SCHEMA`（默认 `travel_checkpoints`）存父图状态。两个 schema 必须不同。迁移已有会话时保持原先成对使用的 schema。
- Redis 和 Qdrant 分别使用 `REDIS_PORT`（`6379`）和 `QDRANT_PORT`（`6333`）。当前正式业务可在它们不可用时运行并返回降级健康状态。
- MySQL 是历史 FAQ 路径使用的独立可选服务。运行连接探针时，配置 `MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_DATABASE`、`MYSQL_USER`、`MYSQL_PASSWORD`。使用本机专用项目账号。
- `TRAVEL_FLIGHT_MODE=direct` 默认直接读取 PostgreSQL 并调用真实的本机 MCP，数据是标注为演示用的合成行程与机场样本目录。`offline` 明确使用脚本模型。`deepseek` 使用真实模型，必须配置 `DEEPSEEK_API_KEY` 和可用网络。`TRAVEL_DEEPSEEK_RUN_LIMIT` 默认每次 API 进程启动最多允许 3 次模型工作流，每个工作流最多 3 次 HTTP 请求。
- 如果终端启动早于 Windows 用户级或系统级环境变量的更新，请重新打开终端。也可从已配置的对应作用域把变量读进当前进程。下列命令只赋值，不打印。

```powershell
if (-not $env:DEEPSEEK_API_KEY) {
    $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
}
if (-not $env:DEEPSEEK_API_KEY) {
    $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'Machine')
}
```

## 安装与初始化

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run-local.ps1 -Action Prepare
```

脚本使用 `poetry.lock` 和 `flight_role_validation/poetry.lock`，在同一个 Git 工作区管理两个内部虚拟环境。API 与旧代码保留当前 LangGraph/LangChain 依赖，航班角色沿用 LangChain v1。用户使用一个启动入口。脚本先准备依赖，然后启动 PostgreSQL，尝试启动 Redis/Qdrant，执行 Alembic 迁移、幂等演示数据导入和 LangGraph 检查点初始化。`.env` 和虚拟环境由 Git 忽略。

应用启动、验证和关停见 [RUNBOOK](RUNBOOK.md)。
