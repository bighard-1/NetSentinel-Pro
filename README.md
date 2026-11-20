# NetSentinel Pro

NetSentinel Pro 是一套面向内网资产的轻量级 NOC 监控系统，适合长期值守服务器、交换机管理地址、防火墙、网关、摄像头、门店网络设备和 HTTP/TCP 服务端点。项目采用 FastAPI、SQLite、异步探测引擎和单页大屏控制台实现，重点关注部署简单、状态直观、告警可追踪、误操作可审计可恢复。

## 功能总览

### 监控看板

- 科技感大屏首页，按卡片墙展示所有监控目标。
- 离线设备自动置顶并以红色高亮，便于第一时间定位故障资产。
- 支持分组标签栏、状态筛选、设备名/IP 搜索。
- 每张卡片显示独立探测倒计时，支持不同设备使用不同探测周期。
- 看板将待确认、疑似异常、在线、离线、维护、暂停和停止分别呈现；可切换卡片与表格视图、排序和分页。视图偏好保存在浏览器中，并写入 URL hash 便于分享。
- 卡片显示状态持续时间、最近成功、失败原因及数据过期提示；提供浅色主题、大屏模式和连接中断提示。
- 暂停和停止的设备仍保留在监控墙中，不会误以为资产被删除。

### 资产与探测

- 单个添加、批量导入、编辑、删除设备。
- 支持 ICMP、TCP 端口、HTTP 状态码三类探测。
- 支持单设备探测周期配置。
- 记录探测延迟、HTTP 状态码、失败原因、最近趋势和 24 小时失败率。
- 设备详情页展示最近事件、探测趋势、维护模式、在线率和告警统计。
- 监控首页提供中英文主要文案切换；Vue 运行时随项目本地提供，内网部署无需访问外部 CDN。
- 设备详情提供单设备立即探测、24 小时与 7 天聚合趋势，以及 30 分钟、2 小时、今晚结束的维护快捷预设。

### 状态判定

系统将设备状态明确划分为三类：

- 在线：最近一次已被系统确认探测成功。
- 离线：连续探测失败次数达到掉线阈值后确认离线。
- 未知：系统尚未获得足够证据确认在线或离线，例如刚添加尚未完成首次探测，或失败次数尚未达到离线阈值。

探测方式说明：

- ICMP：Ping 成功视为本轮可达。
- TCP：指定端口连接成功视为本轮可达。
- HTTP：未配置期望码时，2xx/3xx 视为成功；配置期望码后必须精确匹配。

### 告警策略与通知

- 支持全局掉线阈值、恢复阈值、探测周期。
- 支持按分组覆盖阈值和静默窗口。
- 支持维护模式，维护期间继续探测和记录，但抑制通知。
- 支持全局/分组告警静默窗口。
- 支持飞书、企业微信、钉钉、邮件、通用 Webhook。
- 支持每个分组绑定多个通知渠道。
- 支持渠道测试发送和通知投递记录。

### 批量操作

- 支持批量选择模式，默认不污染卡片视图。
- 批量暂停、批量停止、批量改分组、批量改探测周期。
- 批量删除必须输入本次选中设备数量确认，防止误删。

### 审计与恢复

- 系统日志记录登录、资产变更、策略变更、通知投递、备份恢复、手动探测等事件。
- 批量删除前自动生成 CSV 快照，保存至 `data/audit/`。
- 单个及批量删除会把设备配置写入 7 天回收站；可在删除提示中立即撤销，也可在控制台“回收站”恢复。历史探测指标和告警事件不会随配置恢复。
- 删除快照内容同时写入日志详情，可按设备名/IP 检索。
- 控制台提供“删除快照”管理页，支持按时间、操作者、设备名/IP 搜索。
- 支持从删除快照中选择部分设备恢复，已存在相同 IP、探测类型、端口及路径组合的目标自动跳过。
- 控制台提供“操作审计”时间线，统一查看新增、编辑、暂停、停止、启用、恢复、删除等高风险动作。

### 数据与运维

- SQLite 单文件存储，适合轻量部署和快速迁移。
- 支持数据库备份下载和恢复上传，恢复前执行完整性与大小检查。
- 支持设备清单 CSV 导出。
- 批量导入支持 CSV 模板、逐行预览校验和错误行 CSV 下载，确认后仅导入有效行。
- 每周报告任务会生成稳定性摘要，支持在控制台自定义推送星期、时间、时区和启停状态。历史数据每日独立清理，停用周报不影响清理。
- 当前状态、连续成功或失败次数持久化到设备记录，重启或清理历史事件后仍能正确判定状态。
- 提供 `scripts/self_check.py` 自检脚本，覆盖核心 API、探测、告警、通知、审计、恢复链路。

## 技术栈

- 后端：FastAPI、SQLAlchemy、APScheduler
- 存储：SQLite
- 探测：ICMP Ping、TCP Socket、HTTP 请求
- 前端：Vue 3 单页应用
- 部署：Docker / Docker Compose

## 目录结构

```text
.
├── api/routers/             # 拆分后的 API 路由
├── services/                # 状态计算和数据库结构辅助服务
├── static/index.html        # 前端单页控制台
├── engine.py                # 探测引擎
├── alert.py                 # 告警与通知投递
├── models.py                # 数据模型
├── main.py                  # 应用入口
├── scripts/self_check.py    # 项目自检脚本
├── Dockerfile
└── docker-compose.yml
```

## 环境变量

最低必需配置：

```bash
ADMIN_USER=admin
ADMIN_PASS=change-me
```

推荐生产配置：

```bash
APP_SECRET=replace-with-long-random-secret
ADMIN_TOKEN_TTL_SECONDS=43200
LOGIN_WINDOW_SECONDS=300
LOGIN_MAX_FAILURES=5
MAX_RESTORE_BYTES=52428800
ALERT_RETENTION_DAYS=90
PROBE_METRIC_RETENTION_DAYS=14
SYSTEM_LOG_RETENTION_DAYS=30
NOTIFICATION_DELIVERY_RETENTION_DAYS=30
ALERT_OUTBOX_RETENTION_DAYS=90
BACKUP_RETENTION_COUNT=10
PROBE_CONCURRENCY=50
HTTP_VERIFY_TLS=true
HTTP_FOLLOW_REDIRECTS=false
HTTP_USER_AGENT=NetSentinel-Pro/1.0
ALERT_BATCH_INTERVAL_SECONDS=5
ALERT_STORM_THRESHOLD=3
```

环境变量由 `services/config.py` 在启动时统一校验，日志只输出非敏感的有效值。未列出的变量使用下表默认值；生产模式下 `APP_SECRET` 必填。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `APP_ENV` | `development` | 设置为 `production` 时强制要求 `APP_SECRET` |
| `ADMIN_USER` / `ADMIN_PASS` | 空 | 管理员账户 |
| `VIEWER_USER` / `VIEWER_PASS` | 空 | 可选只读账户 |
| `APP_SECRET` | 空 | 令牌签名和渠道密钥加密；更换前须迁移已有密文 |
| `ADMIN_TOKEN_TTL_SECONDS` | `43200` | 令牌有效期，60 至 604800 秒 |
| `LOGIN_WINDOW_SECONDS` / `LOGIN_MAX_FAILURES` / `LOGIN_MAX_BUCKETS` | `300` / `5` / `10000` | 登录限流窗口、次数、内存桶上限 |
| `MAX_RESTORE_BYTES` / `MAX_BULK_IMPORT_BYTES` | `52428800` / `1048576` | 恢复文件和批量导入请求上限 |
| `PROBE_CONCURRENCY` | `50` | 同时探测上限，1 至 1000 |
| `HTTP_VERIFY_TLS` / `HTTP_FOLLOW_REDIRECTS` | `true` / `false` | HTTP 探测 TLS 校验与跳转 |
| `HTTP_USER_AGENT` | `NetSentinel-Pro/1.0` | HTTP 探测请求标识 |
| `ALERT_BATCH_INTERVAL_SECONDS` / `ALERT_STORM_THRESHOLD` | `5` / `3` | 告警汇总窗口和风暴合并阈值 |
| `ALERT_RETENTION_DAYS` / `PROBE_METRIC_RETENTION_DAYS` | `90` / `14` | 告警事件和探测指标保留天数 |
| `SYSTEM_LOG_RETENTION_DAYS` / `NOTIFICATION_DELIVERY_RETENTION_DAYS` / `ALERT_OUTBOX_RETENTION_DAYS` | `30` / `30` / `90` | 日志、投递记录与告警队列保留天数 |
| `BACKUP_RETENTION_COUNT` | `10` | 各类升级前和恢复前备份分别保留最近 N 份 |
| `RECYCLE_BIN_RETENTION_DAYS` | `7` | 删除设备配置在回收站保留天数 |
| `ENABLE_DOCS` / `ENABLE_HSTS` | `false` / `false` | API 文档和 HSTS 响应头 |
| `ALLOW_PRIVATE_WEBHOOK` / `TRUSTED_PROXIES` | `false` / 空 | 内网 Webhook 例外、可信代理 IP 列表 |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM` / `SMTP_TLS` | 空 / `587` / 空 / 空 / 空 / `true` | 邮件渠道配置 |
| `FEISHU_WEBHOOK` | 空 | 未配置通知渠道时的兼容发送地址 |
| `PUBLIC_BASE_URL` | 空 | 通知中的控制台链接，例如 `https://monitor.example.com` |

邮件通知配置：

```bash
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=sender@example.com
SMTP_PASS=change-me
SMTP_FROM=sender@example.com
SMTP_TLS=true
```

## Docker 部署

创建 `.env`：

```bash
ADMIN_USER=admin
ADMIN_PASS=change-me
APP_SECRET=replace-with-long-random-secret
FEISHU_WEBHOOK=
```

启动服务：

```bash
docker compose up -d --build
```

访问地址：

```text
http://localhost:8000
```

数据默认挂载到本地：

```text
data/monitor.db
data/audit/
data/backups/
```

## 部署安全

生产环境必须设置随机生成的 `APP_SECRET`、强 `ADMIN_PASS`，并通过 HTTPS 反向代理访问。Compose 仅将 8000 端口绑定到宿主机回环地址。服务仅支持 **单 worker**；多个 worker 会重复执行探测和定时任务。只读 viewer 可选配置 `VIEWER_USER`、`VIEWER_PASS`。前端令牌保存在当前标签页的 `sessionStorage`，退出时服务端会吊销令牌。`APP_ENV=production` 未配置 `APP_SECRET` 时拒绝启动；`ENABLE_DOCS=true` 可在可信开发环境启用 API 文档。

Caddy 示例：

```caddyfile
monitor.example.com {
    reverse_proxy 127.0.0.1:8000 {
        header_up X-Forwarded-For {remote_host}
    }
}
```

Nginx 示例（需另行配置 TLS 证书）：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

仅在明确知道代理来源地址时设置 `TRUSTED_PROXIES`（逗号分隔 IP）。服务默认忽略客户端直接发送的 `X-Forwarded-For`。`ENABLE_HSTS=true` 可在已启用 HTTPS 的代理后添加 HSTS。Webhook 默认只接受 HTTPS 且不能解析为内网、回环或保留地址；确需内网通知时显式设置 `ALLOW_PRIVATE_WEBHOOK=true`。通知密钥在数据库中加密；生产环境应备份 `APP_SECRET`，丢失后无法解密既有通知配置。

镜像以 UID 10001 运行，挂载的 `data/` 必须对该 UID 可写。Compose 根文件系统只读，只给 `data/` 写权限和 ICMP 所需的 `NET_RAW` 能力。构建镜像前请检查 Docker 数据根目录的剩余空间与所在磁盘。

启动时会检查数据库迁移版本。需要升级旧库时，系统先在 `data/backups/` 创建通过完整性检查的在线备份，然后应用 Alembic 迁移。若迁移失败，应停止服务、保留原库与升级前备份，再从备份恢复。`GET /health/ready` 会检查后台任务、监控心跳与数据库连接；容器健康检查使用此接口。

P3 迁移将设备唯一标识改为 IP、探测类型、端口、路径的组合；同一个 IP 可添加不同 TCP 端口或 HTTP 路径。旧库升级前的备份按 `BACKUP_RETENTION_COUNT` 每类保留最近 N 份。升级失败时先停止服务，将最新的 `data/backups/monitor-pre-upgrade-*.db` 复制到 `data/monitor.db`，确认文件属主与权限，然后重新启动旧版程序。恢复操作失败时使用 `monitor-pre-restore-*.db` 同样回滚；操作前应另存当前数据库和 WAL 文件以便调查。SQLite 在线备份包含已提交的 WAL 数据。时间在 API 中使用带 `+00:00` 时区的 UTC 格式，浏览器按本地时区显示。

恢复上传时系统会先暂停后台监控与通知，备份当前库，验证上传库，再原子替换并迁移；恢复失败会用恢复前备份回滚。状态事件与待发送告警在同一事务提交；通知逐渠道记录成功结果，失败按指数退避最多尝试 5 次，超出后标记为 `DEAD` 以供排查。`ALERT_BATCH_INTERVAL_SECONDS` 控制告警汇总窗口，单组离线事件达到 `ALERT_STORM_THRESHOLD` 后发送合并通知。

1000 台模拟设备的 P1 压测结果见 [压测报告](docs/benchmarks/p1-1000-devices.md)。
P3 数据模型升级后的复测结果见 [P3 压测报告](docs/benchmarks/p3-1000-devices.md)。

## 构建 x86 Docker 镜像

```bash
docker build --platform linux/amd64 -t netsentinel-pro:latest .
```

如需标记 GHCR 镜像：

```bash
docker tag netsentinel-pro:latest ghcr.io/bighard-1/netsentinel-pro:latest
```

## 本地开发

需要 Python 3.12 或更新版本。虚拟环境和项目数据会保存在当前项目目录中。

```bash
python -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
ADMIN_USER=admin ADMIN_PASS=change-me uvicorn main:app --reload --no-proxy-headers
```

## 项目自检

自检会在项目目录内创建临时测试目录，并在结束时清理，不会使用系统默认临时目录。

本地 Python 环境：

```bash
python scripts/self_check.py
```

容器环境：

```bash
docker build --platform linux/amd64 -t netsentinel-pro:self-check .
docker run --rm --platform linux/amd64 netsentinel-pro:self-check python scripts/self_check.py
```

自检覆盖：

- 健康检查与安全响应头
- 管理员登录与限流
- 设备创建、编辑、批量导入、批量更新
- ICMP/TCP/HTTP 探测配置校验
- 探测指标、延迟、失败率、详情页
- 维护模式、静默窗口、分组策略
- 通知渠道、测试发送、投递记录
- 批量删除二次确认
- 删除快照生成、查询、下载、部分恢复
- 操作审计时间线

## API 摘要

### 认证与系统

- `POST /api/auth/login`：管理员登录。
- `GET /health`：健康检查。
- `GET /api/settings`：读取监控设置。
- `PUT /api/settings`：更新监控设置，包含周报推送时间、时区和开关配置。

### 看板与事件

- `GET /api/summary`：看板总览。
- `GET /api/risk-devices`：风险设备排行。
- `GET /api/alerts`：告警历史。
- `GET /api/logs`：系统日志。
- `GET /api/audit-timeline`：操作审计时间线。
- `POST /api/trigger-monitor`：手动触发探测。

### 设备资产

- `GET /api/devices`：设备列表。
- `POST /api/devices`：新增设备。
- `POST /api/devices/bulk`：批量导入设备。
- `PUT /api/devices/bulk-update`：批量更新设备。
- `POST /api/devices/bulk-delete`：受保护批量删除。
- `GET /api/devices/{device_id}`：设备信息。
- `GET /api/devices/{device_id}/detail`：设备详情。
- `PUT /api/devices/{device_id}`：更新设备。
- `DELETE /api/devices/{device_id}`：删除设备。
- `PUT /api/devices/{device_id}/maintenance`：设置或取消维护模式。
- `GET /api/devices/export.csv`：导出设备 CSV。

### 删除快照

- `GET /api/devices/delete-snapshots`：删除快照列表。
- `GET /api/devices/delete-snapshots/{log_id}`：读取删除快照设备清单。
- `POST /api/devices/delete-snapshots/{log_id}/restore`：从快照中选择恢复设备。

### 分组策略

- `GET /api/group-policies`：查看分组告警策略。
- `PUT /api/group-policies/{group_name}`：创建或更新策略。
- `DELETE /api/group-policies/{group_name}`：删除策略。

### 通知渠道

- `GET /api/notification-channels`：通知渠道列表。
- `POST /api/notification-channels`：创建通知渠道。
- `PUT /api/notification-channels/{channel_id}`：更新通知渠道。
- `DELETE /api/notification-channels/{channel_id}`：删除通知渠道。
- `POST /api/notification-channels/{channel_id}/test`：发送测试通知。
- `GET /api/notification-deliveries`：投递记录。
- `GET /api/group-notification-routes`：分组通知路由。
- `PUT /api/group-notification-routes/{group_name}`：更新分组通知渠道绑定。

### 备份恢复

- `GET /api/backup`：下载数据库备份。
- `POST /api/restore`：上传并恢复数据库。

## 生产建议

- 使用强密码和独立 `APP_SECRET`。
- 为 Docker 数据目录配置可靠备份。
- 将 `data/monitor.db` 和 `data/audit/` 纳入备份策略。
- 在反向代理层启用 HTTPS、访问控制和日志审计。
- 定期运行自检脚本验证升级后的关键链路。

## License

当前仓库未声明开源许可证。生产或二次分发前请先补充明确的许可证文件。
