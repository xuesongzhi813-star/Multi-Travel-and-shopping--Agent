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
