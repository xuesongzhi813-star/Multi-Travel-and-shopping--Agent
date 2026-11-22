# P1 本地运行基础

应用在本机运行，PostgreSQL 19.0、Qdrant 1.20.1 和 Redis 8 由独立的 `compose.local.yml` 管理。三个服务只发布到本机回环地址。Windows 上的 MySQL 服务通过项目专用账号使用。

## 启动

1. 将 `.env.example` 复制为本机 `.env`，为 PostgreSQL 设置随机密码，按本机配置填写 MySQL 项目账号。保留 `.env` 的 Git 忽略规则。
2. 确认 PostgreSQL、Qdrant 和 Redis 指定端口空闲。若有占用，在 `.env` 修改对应端口。仓库中的旧 Redis 容器及数据独立保留。
3. 运行以下命令。首次拉取使用 Compose 文件中的固定摘要。PostgreSQL 和 Qdrant 镜像使用已有可访问的公共拉取缓存，Redis 复用本机镜像。

```powershell
docker compose --env-file .env -f compose.local.yml config --quiet
docker compose --env-file .env -f compose.local.yml up -d --wait --wait-timeout 90
```

检查 PostgreSQL 和 Redis 的容器健康状态，并在本机检查 Qdrant 的 `/readyz`。停止服务并保留数据卷时运行：

```powershell
docker compose --env-file .env -f compose.local.yml stop
```

PostgreSQL 和 Qdrant 分别保存于 `postgres-data` 和 `qdrant-data`。Redis 用于可重建的缓存。MySQL 由 Windows 服务管理。

## 验收

在 Windows 本机安装锁定的验收驱动：

```powershell
poetry check --lock
poetry sync --with local-runtime --no-root
poetry run python -m pip check
poetry run python scripts/check_local_runtime.py check
```

MySQL 验收需要本机项目库和专用账号，并在 `.env` 配置 `MYSQL_DATABASE`、`MYSQL_USER`、`MYSQL_PASSWORD`。直接授予项目库 `SELECT`、`INSERT`、`UPDATE`、`DELETE`、`CREATE`、`ALTER`、`DROP`、`INDEX` 和 `CREATE TEMPORARY TABLES` 权限即可。若 MySQL 使用数据库名通配语义，按官方语法把项目库名中的下划线或百分号转义，授权到实际库名。验收分别输出四个服务的结果，MySQL 缺项会返回非零。

使用同一个唯一 ID 验收 PostgreSQL 和 Qdrant 的持久化。验证脚本会等待 Qdrant 就绪，最多等待 30 秒。若中途失败，保留该 ID，检查服务后运行 `cleanup` 清理探针。

```powershell
$probe = [guid]::NewGuid().ToString()
poetry run python scripts/check_local_runtime.py write --probe-id $probe
docker compose --env-file .env -f compose.local.yml up -d --force-recreate --wait --wait-timeout 90 postgres qdrant
poetry run python scripts/check_local_runtime.py verify --probe-id $probe
poetry run python scripts/check_local_runtime.py cleanup --probe-id $probe
```
