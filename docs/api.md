# HTTP API

当前正式 API 路径以 `/api/v1` 开头，旧版 `/api` 路径保留为兼容别名；下表中的 `/api/...` 路径均可替换为 `/api/v1/...`。`/health` 与 `/health/ready` 为公开探针。除登录接口外，API 请求须带 `Authorization: Bearer <token>`。可选 viewer 账户只读，写请求返回 403。默认关闭交互式 OpenAPI；在可信开发环境设置 `ENABLE_DOCS=true` 后可访问 `/docs` 和 `/openapi.json`，文档仅列出 `/api/v1` 路径。

## 认证与系统（Auth / System）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | 用户名和密码换取 Bearer token |
| POST | `/api/auth/logout` | 吊销当前 token |
| GET | `/api/security-status` | 弱口令提示状态 |
| GET | `/api/version` | 应用版本、当前与目标迁移版本、迁移状态；需认证 |
| GET | `/health` | 进程存活，`{"status":"ok"}` |
| GET | `/health/ready` | 后台任务、探测心跳和数据库就绪；失败返回 503 |

```http
POST /api/auth/login HTTP/1.1
Content-Type: application/json

{"username":"admin","password":"your-strong-password"}
```

成功响应含 `access_token`、`token_type: "bearer"` 和 `role`。令牌有效期由 `ADMIN_TOKEN_TTL_SECONDS` 控制；退出登录后服务端吊销。

## 资产与探测（Devices / Monitor）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET / POST | `/api/devices` | 列表 / 创建单个目标 |
| GET / PUT / DELETE | `/api/devices/{device_id}` | 查询 / 更新 / 删除目标 |
| GET | `/api/devices/{device_id}/detail` | 最近事件、趋势、在线率和维护信息 |
| POST | `/api/devices/{device_id}/probe` | 排队立即探测单个目标，5 秒冷却 |
| PUT | `/api/devices/{device_id}/maintenance` | 设置或取消维护期 |
| POST | `/api/devices/bulk-preview` | 预览 CSV 文本并逐行校验 |
| POST | `/api/devices/bulk` | 批量创建，单次最多 1000 条 |
| PUT | `/api/devices/bulk-update` | 批量改状态、分组或周期 |
| POST | `/api/devices/bulk-delete` | 输入选中数量确认后移入回收站 |
| GET | `/api/devices/export.csv` | 下载防公式注入的 CSV |
| GET | `/api/devices/recycle-bin` | 未过期删除项 |
| POST | `/api/devices/recycle-bin/{entry_id}/restore` | 从回收站恢复配置 |
| GET | `/api/devices/delete-snapshots` | 旧版删除快照摘要 |
| GET | `/api/devices/delete-snapshots/{log_id}` | 快照设备清单 |
| GET | `/api/devices/delete-snapshots/{log_id}/download` | 下载快照 CSV |
| POST | `/api/devices/delete-snapshots/{log_id}/restore` | 选择行号恢复 |
| POST | `/api/trigger-monitor` | 全局手动触发探测 |

```http
POST /api/devices HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json

{"name":"Core switch","ip_address":"10.0.0.1","group_name":"network","probe_type":"ICMP","probe_interval_seconds":30}
```

同一地址可以用不同探测类型、TCP 端口或 HTTP 路径创建多个目标；完全相同的组合会被判为重复。TCP 目标需要 `probe_port`，HTTP 目标可设置 `probe_path` 和 `expected_status`。探测周期最少 5 秒。设备列表可用 `group_name`、`status=ONLINE|OFFLINE|UNKNOWN` 筛选，并用 `limit`（最多 1000）及 `offset` 分页；不传 `limit` 时保留完整数组响应，供旧前端使用。重复批量创建会跳过已有目标，重复批量更新不会重置未变化设备的状态，重复批量删除会返回 `deleted: 0`。

## 看板、事件与审计（Dashboard / Events）

| 方法 | 路径 | 常用参数 |
| --- | --- | --- |
| GET | `/api/summary` | 无 |
| GET | `/api/risk-devices` | `limit` |
| GET | `/api/alerts` | `device_id`、`event_type=ONLINE\|OFFLINE`、`limit`、`offset` |
| GET | `/api/logs` | `level`、`category`、`q`、`limit`、`offset` |
| GET | `/api/audit-timeline` | `q`、`action`、`device_id`、`limit`、`offset` |

事件、日志、删除快照与投递列表使用 `limit`、`offset`；默认通常为 200、上限 1000，具体端点以 OpenAPI 参数为准。时间为带时区的 UTC ISO 8601 字符串。成功的已认证写请求会追加审计记录，包含操作人、来源 IP、目标路径和操作前后状态摘要；请求正文及明文密钥不写入该记录。

## 策略、通知与设置（Groups / Notifications / Settings）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET / PUT | `/api/settings` | 读取 / 更新全局监控与周报设置 |
| PUT | `/api/groups/rename` | 批量改组名 |
| GET | `/api/group-policies` | 分组阈值与静默策略列表 |
| PUT / DELETE | `/api/group-policies/{group_name}` | 保存 / 删除分组策略 |
| GET / POST | `/api/notification-channels` | 渠道列表 / 创建 |
| PUT / DELETE | `/api/notification-channels/{channel_id}` | 更新 / 删除渠道 |
| POST | `/api/notification-channels/{channel_id}/test` | 测试发送并写投递记录 |
| GET | `/api/notification-deliveries` | 投递结果 |
| GET | `/api/group-notification-routes` | 分组路由列表 |
| PUT | `/api/group-notification-routes/{group_name}` | 保存分组路由 |

渠道地址与扩展配置在 API 中脱敏；编辑时留空表示保留现有密钥。通知目的地默认仅允许 HTTPS 且不得指向回环、内网或元数据地址。

## 备份恢复（Backup）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/backup` | 下载一致性的 SQLite 数据库快照 |
| POST | `/api/restore` | `multipart/form-data` 上传 `.db` 并恢复，超限返回 413 |

恢复会暂停后台写入、先备份当前库、校验上传库，再原子替换。失败时尝试自动回滚；运维步骤见 [运维手册](operations.md)。

## 错误响应

API 错误统一返回 `{"code":"VALIDATION_ERROR","message":"请求参数无效","detail":[...]}` 一类结构，`detail` 保留旧版兼容字段，字段校验错误不回显输入内容。常见状态码：400 参数或文件无效、401 未认证或令牌失效、403 viewer 写操作、404 对象不存在、409 状态冲突、411 导入请求缺少 Content-Length、413 文件过大、422 字段校验失败、429 限流、500 恢复失败、503 后台未就绪。已认证 API 默认按令牌和来源 IP 各限制每分钟 600 次，超限返回 `Retry-After`；登录失败有独立限流策略。
