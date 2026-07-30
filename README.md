# NetSentinel Pro

NetSentinel Pro 是一套面向内网资产的轻量级 NOC 监控系统，适合长期值守服务器、交换机管理地址、防火墙、网关、摄像头、门店网络设备和 HTTP/TCP 服务端点。项目采用 FastAPI、SQLite、异步探测引擎和单页大屏控制台实现，重点关注部署简单、状态直观、告警可追踪、误操作可审计可恢复。

## 功能总览

### 监控看板

- 科技感大屏首页，按卡片墙展示所有监控目标。
- 离线设备自动置顶并以红色高亮，便于第一时间定位故障资产。
- 支持分组标签栏、状态筛选、设备名/IP 搜索。
- 每张卡片显示独立探测倒计时，支持不同设备使用不同探测周期。
- 暂停和停止的设备仍保留在监控墙中，不会误以为资产被删除。

### 资产与探测

- 单个添加、批量导入、编辑、删除设备。
- 支持 ICMP、TCP 端口、HTTP 状态码三类探测。
- 支持单设备探测周期配置。
- 记录探测延迟、HTTP 状态码、失败原因、最近趋势和 24 小时失败率。
- 设备详情页展示最近事件、探测趋势、维护模式、在线率和告警统计。

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
- 删除快照内容同时写入日志详情，可按设备名/IP 检索。
- 控制台提供“删除快照”管理页，支持按时间、操作者、设备名/IP 搜索。
- 支持从删除快照中选择部分设备恢复，已存在相同 IP 的设备自动跳过。
- 控制台提供“操作审计”时间线，统一查看新增、编辑、暂停、停止、启用、恢复、删除等高风险动作。

### 数据与运维

- SQLite 单文件存储，适合轻量部署和快速迁移。
- 支持数据库备份下载和恢复上传，恢复前执行完整性与大小检查。
- 支持设备清单 CSV 导出。
- 每周报告任务会生成稳定性摘要并清理过期告警记录。
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
```

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
```

## 构建 x86 Docker 镜像

```bash
docker build --platform linux/amd64 -t netsentinel-pro:latest .
```

如需标记 GHCR 镜像：

```bash
docker tag netsentinel-pro:latest ghcr.io/bighard-1/netsentinel-pro:latest
```

## 本地开发

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
ADMIN_USER=admin ADMIN_PASS=change-me uvicorn main:app --reload
```

## 项目自检

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
- `PUT /api/settings`：更新监控设置。

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
