# P4 航班角色验证

P4 从项目根目录运行，使用 `flight_role_validation` 的 Poetry 项目和独立虚拟环境。

```powershell
poetry -P flight_role_validation install --no-root
poetry -P flight_role_validation run python -B -m flight_role_validation
poetry -P flight_role_validation run python -B -m unittest discover -s flight_role_validation/tests -v
```

默认命令使用脚本模型，模型 API 请求数为 0。包含数据库的测试使用临时 PostgreSQL schema，从本机 P2 配置读取连接信息，然后清理本轮合成数据。可用 `P2_ENV_FILE` 指定现有的本机环境文件。

启用真实验收前，先确认本轮使用预算并在测试进程中配置 `DEEPSEEK_API_KEY`。只有在为本轮批准了真实调用时执行一次：

```powershell
poetry -P flight_role_validation run python -B -m flight_role_validation --live
```

单次验收最多发出三次请求，每次最多 384 个输出 token，15 秒单请求限制，40 秒角色总时限。SDK 禁用自动重试，禁止重定向。输出记录实际尝试数、HTTP 状态、可用 token 用量、核验结果和合成航班交接。失败或达到上限时，保留结果并停止追加调用。

目标航班 `DEMO-A1` 的交接到达机场为 `ZRH`，时区为 `Europe/Zurich`，当地到达日期为 `2026-12-15`。
