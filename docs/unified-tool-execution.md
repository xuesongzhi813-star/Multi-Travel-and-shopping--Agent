# P5 统一工具执行

共享执行器从宿主接收身份、角色与运行标识。每次调用先鉴权，再校验参数和投影结果。P4 航班角色只看到 `lookup_flight`，工具回执保留原调用 ID。`lookup_airport_city` 只对 P5 本地验收角色开放。

按本机 P2 配置设置 `P2_ENV_FILE`。要在业务 schema 启用 PostgreSQL 工具审计，从根环境运行 `poetry run python scripts/prepare_business_db.py migrate`。审计表由迁移 0002 创建。

从项目根目录复验两套环境：

```powershell
poetry run python -B -m unittest discover -s tests -v
poetry -P flight_role_validation install --no-root
poetry -P flight_role_validation run python -B -m unittest discover -s flight_role_validation/tests -v
poetry -P flight_role_validation run python -B -m flight_role_validation
```

P5 集成测试运行真实本地 MCP 子进程和 PostgreSQL 临时 schema，测试后清理。默认入口的 DeepSeek 请求数为 0。

执行结果包括 `ok`、`invalid_arguments`、`permission_denied`、`not_found`、`timeout` 和 `tool_error`。可恢复传输故障最多重试一次，两次尝试共享同一工具期限。审计使用独立事务，同一运行与工具调用标识只保留首条。审计写入失败时发出包含运行标识的警告，已完成的只读结果仍可返回。
