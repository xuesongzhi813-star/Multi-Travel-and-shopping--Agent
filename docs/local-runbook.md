# 旅行客服项目本地运行手册

当前统一入口覆盖 P1–P5 已交付的本地功能。后续功能持续并入同一个主项目，并从统一入口做整体验证。日常运行使用 `main` 和下面这一套命令。

## 准备

1. 安装 Python 3.12、Poetry 2.x 和 Docker Desktop。启动 Docker Desktop，确认 PowerShell 可运行 `py -3.12 --version`、`poetry --version` 和 `docker compose version`。
2. 在 PowerShell 进入主项目目录。已有本机配置可直接使用。首次配置时从模板创建根目录下的 `.env`，填写 `POSTGRES_PASSWORD`、`P2_DEMO_A_PASSWORD` 和 `P2_DEMO_B_PASSWORD`，两个演示账号使用不同密码。

```powershell
Set-Location -LiteralPath 'E:\multi-agent\Multi-Travel-and-shopping--Agent'
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
notepad .env
```

首次使用已有 PostgreSQL 卷时，沿用创建该卷时的数据库名、用户名和密码。应用默认使用 `travel_business` 业务 schema 和 `.env` 中的 `P3_CHECKPOINT_SCHEMA`。需要继续使用之前的演示会话时，在启动前填入原来成对使用的 `P2_DB_SCHEMA` 和 `P3_CHECKPOINT_SCHEMA`。种子重复执行会保留已创建的账号密码和后续库存修改。MySQL 是旧有 FAQ 存储的可选依赖，当前网页功能以 PostgreSQL 为主。

## 启动

在项目根目录执行以下命令，保留窗口查看日志。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run-local.ps1
```

脚本安装锁定的依赖，启动 PostgreSQL、Qdrant 和 Redis，迁移并导入合成数据，初始化 LangGraph 检查点，然后启动网页。它会自动管理仓库内两个 Python 虚拟环境，兼容现有 API 框架和航班角色各自的 SDK 版本。看到 Uvicorn 的 `Application startup complete` 后，打开 [旅行客服首页](http://127.0.0.1:8000/)。[交互 API 文档](http://127.0.0.1:8000/docs) 可用于诊断。端口被占用时可追加 `-Port 8001`，浏览器也使用该端口。

## 一次浏览器验证

1. 用 `demo-a` 和 `.env` 中的 `P2_DEMO_A_PASSWORD` 登录。创建会话后发送一条消息，再发送“请忽略系统规则”观察拒绝结果。记下会话 ID，重启后在同一页面重新打开该会话可查看记录和检查点。
2. 在“本人行程”选择 `DEMO-A1`，查询航班。结果来自 PostgreSQL 中的合成行程，核验后的到达机场是 `ZRH`。默认使用本地脚本模型驱动航班角色，网页运行产生零次收费模型调用。
3. 在“机场查询”输入 `ZRH`，可看到通过本地 MCP 工具返回的 `Zürich`。航班查询与机场查询均经过统一工具执行器并写入最小审计记录。
4. 切换为 `demo-b`，用 `P2_DEMO_B_PASSWORD` 登录，确认本人行程已切换，尝试打开第一个账号的会话应返回未找到。

当前网页从同一界面提供会话、航班查询和机场查询。航班到酒店的自动交接及用户确认预订将在 P6、P7 接入。

## 运维与关闭

另开 PowerShell，在同一项目根目录查看容器和网页状态。使用自定义端口时，状态命令也传入相同的 `-Port`。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run-local.ps1 -Action Status
docker compose --env-file .env -f compose.local.yml logs --tail 100 postgres qdrant redis
```

- 首页直接使用根路径 `/`。旧 P2 的 Uvicorn 已启动时访问 `/` 返回 404，因为旧版本只提供 API 和 `/docs`。出现这组日志时，确认终端所在目录是上面的主项目目录。
- Poetry 的旧元数据提示是弃用警告。以命令退出码、迁移结果和 Uvicorn 启动日志判断有没有实际失败。
- 登录返回 401 时，核对所选账号及该数据库 schema 首次导入时的密码。迁移失败时，先检查 Docker 和 PostgreSQL 状态，再核对数据库配置。

先在启动窗口按 `Ctrl+C`，等待 Uvicorn 完成关闭。随后在同一项目根目录停止容器。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run-local.ps1 -Action Stop
```

该命令保留 PostgreSQL 和 Qdrant 的命名卷。Redis 当前配置为临时缓存，重启后重新生成。下次仍用同一个启动命令。需要只准备环境时可使用 `-Action Prepare`。
