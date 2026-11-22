# P2 会话与业务数据

P2 使用 P1 的本地 PostgreSQL。先启动 P1 容器。P2 工作树的 `.env` 只需 PostgreSQL 连接信息及两组本地演示密码，可用 `P2_ENV_FILE` 指定配置路径。Git 会忽略真实配置。

```powershell
poetry sync --with local-runtime,business --no-root
poetry run python scripts/prepare_business_db.py migrate
poetry run python scripts/prepare_business_db.py seed
```

种子提供 `demo-a` 和 `demo-b`、六条虚构行程及两条虚构酒店库存。照 `.env.example` 在本机设置不同的 `P2_DEMO_A_PASSWORD` 和 `P2_DEMO_B_PASSWORD`，每组 12 至 256 字符。重复运行导入保留已有密码和库存修改。

本地服务启动命令：

```powershell
poetry run uvicorn web_app.app.runtime:app --host 127.0.0.1
```

Windows 使用 `--reload` 选择 Psycopg 需要的 Selector 事件循环：

```powershell
poetry run uvicorn web_app.app.runtime:app --host 127.0.0.1 --reload
```

用 `POST /api/auth/login` 的 JSON 请求登录，保留服务端 Cookie。创建会话、写入消息和查看本人行程均通过 `/api` 新入口。当前写入仅记录消息，父图由 P3 接入。运行 `poetry run python -m unittest discover -s tests -v` 验收迁移、导入及跨用户隔离。
