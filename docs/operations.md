# 运维手册

## 部署与健康检查

使用 Python 3.12，或按根目录 README 通过 Docker Compose 部署。设置强 `ADMIN_PASS` 和随机 `APP_SECRET`；生产模式设置 `APP_ENV=production`。容器以 UID 10001 运行，`data/` 必须对其可写。只运行一个 worker。外部访问经过 HTTPS 反向代理，Compose 默认仅暴露宿主机回环端口。

- `GET /health` 只表示 HTTP 进程可响应。
- `GET /health/ready` 检查后台监控/告警任务、最近探测心跳和数据库连接；503 应触发运维排查。
- 查看 `data/monitor.db`、`data/monitor.db-wal`、`data/backups/` 与 `data/audit/` 的剩余空间和权限。
- 运行日志为 JSON 行格式，写入标准错误输出和 `data/logs/app.jsonl`；单文件默认 10 MiB、保留 5 个轮转文件。API 响应的 `X-Request-ID` 可用于关联同一次请求的日志；设备事件含 `device_id`。
- 每次升级后执行 `python scripts/self_check.py`，并检查通知渠道测试发送的投递记录。

## 容量规划

理论探测记录数：`设备数 × 86400 ÷ 探测周期秒数 × PROBE_METRIC_RETENTION_DAYS`。例如 1000 台、30 秒一次、保留 14 天，约为每天 288 万条、保留窗口内 4032 万条探测记录。设备级周期、停用设备和网络异常会改变实际数量。

不要把一次模拟探测的数据库大小直接外推到长期运行。先在代表性设备上测量 24 小时的主库、WAL 与备份增量，再按记录数估算所需空间；为 WAL、升级前备份、恢复前备份和审计快照另留余量。若增长过快，优先调大非关键目标周期或缩短指标保留天数，并观察告警准确性。

历史清理每天运行，按 5000 行分批删除。`ALERT_RETENTION_DAYS` 默认 90 天，探测指标默认 14 天，系统日志和通知投递默认 30 天，告警队列默认 90 天。最新状态事件及待投递项受保护。相关配置见 README 环境变量表。

## 备份、升级与回滚

1. 升级前确认 `data/monitor.db` 所在盘有足够空间，并另存 `data/` 目录及 `APP_SECRET` 到安全位置。不要把明文密钥提交到仓库。
2. 停止旧服务，部署新代码后启动。应用版本或数据库迁移版本有变化时，启动流程会在 `data/backups/` 生成 `monitor-pre-upgrade-*.db` 并验证完整性，然后执行 Alembic 迁移。`data/.app-version` 记录上次成功启动的应用版本；升级前备份按 `BACKUP_RETENTION_COUNT` 保留最近 N 份。
3. 检查 `/health/ready`、认证后的 `/api/version`（`migration_status` 应为 `current`）、设备状态、通知渠道和 `scripts/self_check.py`。失败时停止新服务，保留现场库，复制最近的 `monitor-pre-upgrade-*.db` 回 `data/monitor.db`，恢复属主与权限，并恢复旧代码及对应的 `data/.app-version`，再启动旧代码。
4. 上传数据库恢复前会另建 `monitor-pre-restore-*.db`；自动恢复失败时用此文件按同样方式回滚。恢复文件须是完整的 NetSentinel SQLite 库，且不得超过 `MAX_RESTORE_BYTES`。

复制数据库时先停止服务，避免 WAL 中仍有未检查点的数据。正常下载备份使用 SQLite 在线备份 API，包含已提交的 WAL 数据。恢复演练应在隔离环境进行，并验证设备数、状态与迁移版本。

## 常见故障

### 设备显示“待确认”或“疑似异常”

检查设备是否启用、最近探测时间是否过期、失败次数与掉线阈值、ICMP 权限或 TCP/HTTP 连接错误。可在详情中点“立即探测此设备”，再查看 `GET /health/ready` 和系统日志。旧版本曾依赖历史事件推断状态；升级到当前版本后状态存储在设备记录中。若升级后仍长期无状态，先核对迁移版本和数据库写权限，不要通过删除历史记录试图修复。

### `/health/ready` 返回 503

查看响应 `detail` 与应用日志。若后台任务结束或心跳过期，检查数据库锁、网络探测耗时及进程是否持续运行。若最近一轮探测失败，排查 `ping` 可执行文件/NET_RAW、DNS、目标超时、SQLite 空间与权限。恢复后该端点应返回 `ready`。

### 通知未送达

先看渠道测试发送结果与投递记录，再检查静默窗口、维护模式、分组路由、Webhook 出站限制、SMTP 设置和死信告警队列。不要把完整 Webhook URL 或密码粘贴到日志或公开 Issue。

### 数据库空间不足或锁等待

确认 WAL 和备份目录位于有空间的持久卷，检查探测周期与保留策略。不要在服务运行时直接删 `monitor.db-wal`。先备份，再停止服务，按 SQLite 官方工具检查完整性；必要时在维护窗口执行压缩或迁移到更大磁盘。
