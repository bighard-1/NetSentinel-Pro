# NetSentinel Pro

NetSentinel Pro 是一个轻量级网络资产存活监控系统，适合在内网环境中监控服务器、网关、交换机管理地址、摄像头、门店设备等基础节点。项目使用 FastAPI、SQLite、异步 ICMP 探测和单页看板实现，强调部署简单、状态清晰、告警可追踪。

## 核心能力

- 设备资产管理：支持单个添加、批量导入、编辑、删除、分组和暂停监控。
- 实时监控看板：展示总设备数、在线数、离线数、未知数，并按故障优先排序。
- 明确状态判定：在线、离线、未知三种状态均有固定判定规则和页面说明。
- 手动探测：管理员可主动触发一轮探测，触发逻辑与服务停止信号分离。
- 飞书告警：设备状态变化后推送飞书卡片，多设备波动会合并发送。
- 系统日志：记录登录、资产变更、配置变更、备份恢复、监控状态变化、告警发送和周报清理。
- 数据备份恢复：支持下载 SQLite 数据库备份，上传恢复前会执行完整性检查。
- 每周报告：每周五 17:00 生成稳定性周报，并清理 30 天前告警记录。

## 三种设备状态的判定方式

### 在线

设备最近一次被系统确认可达。

- 新增设备首次探测成功后，会立即进入在线状态。
- 已离线设备恢复时，需要连续成功次数达到“恢复阈值”后才会重新进入在线状态。
- 这样既避免“刚添加且能 ping 通却长期未知”的困惑，也保留了离线恢复防抖能力。

### 离线

设备连续探测失败次数达到“掉线阈值”后，系统确认设备离线。

- 默认掉线阈值为 3 次。
- 失败次数未达到阈值前不会立即判定离线，用于过滤短暂网络抖动。

### 未知

未知不是“离线”，也不是“在线”。它只表示系统暂时还没有足够证据确认状态。

常见场景：

- 设备刚添加，后台还没完成首次探测。
- 首次探测失败，但失败次数尚未达到掉线阈值。
- 设备被暂停监控前没有产生过确认状态。

## 管理员认证

管理员账号和密码通过环境变量配置：

```bash
ADMIN_USER=admin
ADMIN_PASS=change-me
```

前端登录后由后端签发带过期时间的管理令牌。默认有效期为 12 小时，可通过以下变量调整：

```bash
ADMIN_TOKEN_TTL_SECONDS=43200
```

建议生产环境同时配置 `APP_SECRET`，用于增强令牌签名密钥：

```bash
APP_SECRET=replace-with-long-random-secret
```

## Docker 部署

创建 `.env`：

```bash
ADMIN_USER=admin
ADMIN_PASS=change-me
APP_SECRET=replace-with-long-random-secret
FEISHU_WEBHOOK=
```

启动：

```bash
docker compose up -d --build
```

访问：

```text
http://localhost:8000
```

数据文件默认挂载在本地 `data/monitor.db`。

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
ADMIN_USER=admin ADMIN_PASS=change-me uvicorn main:app --reload
```

## 项目自检

项目提供轻量级自检脚本，覆盖健康检查、登录、设备创建、非法地址拦截和日志查询：

```bash
python scripts/self_check.py
```

## API 摘要

- `POST /api/auth/login`：管理员登录。
- `GET /api/devices`：查看设备列表。
- `POST /api/devices`：新增设备。
- `POST /api/devices/bulk`：批量导入设备。
- `PUT /api/devices/{device_id}`：更新设备。
- `DELETE /api/devices/{device_id}`：删除设备。
- `POST /api/trigger-monitor`：手动触发探测。
- `GET /api/settings`：读取监控设置。
- `PUT /api/settings`：更新监控设置。
- `GET /api/logs`：查看系统日志。
- `GET /api/backup`：下载数据库备份。
- `POST /api/restore`：上传并恢复数据库。

## 后续建议

- 引入 Alembic 数据库迁移，支撑长期版本升级。
- 将前端依赖本地化打包，减少内网部署对公网 CDN 的依赖。
- 增加 TCP/HTTP 探测、延迟统计、丢包率统计和设备详情页。
- 增加多通知渠道，例如企业微信、钉钉、邮件和通用 Webhook。
