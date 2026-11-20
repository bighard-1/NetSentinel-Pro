# NetSentinel Pro 整改总结报告

日期：2026-11-19。工作分支：`codex/p6-observability`。项目、虚拟环境、测试库、浏览器工具、缓存和临时文件均位于外挂盘；未在本机运行会使用内置盘数据目录的 Docker。

## 变更清单

| 阶段 | 已完成的主要改动 | 关键位置 |
| --- | --- | --- |
| P0 | 设备当前状态持久化，历史清理不抹掉状态，恢复探测与就绪探针 | `models.py`、`engine.py`、`services/retention.py`、`main.py` |
| P1 | 探测并发与批次、SQLite 锁恢复、告警持久队列与重试、1000 目标模拟基线 | `engine.py`、`alert.py`、`docs/benchmarks/` |
| P2 | Bearer 认证与 viewer 权限、令牌吊销、通知密钥加密、SSRF 与导出防护、备份恢复及容器加固 | `auth.py`、`services/secrets.py`、`api/routers/backup.py`、`Dockerfile` |
| P3 | 联合探测目标身份、Alembic 迁移、UTC 时间、周报幂等与可用率计算 | `migrations/`、`services/migrations.py`、`weekly_report.py` |
| P4 | 回收站、CSV 预览、设备详情与手动探测、看板状态和基础双语、可访问性与离线前端 | `api/routers/devices.py`、`static/`、`docs/qa/p4-ui-accessibility.md` |
| P5 | Apache-2.0、版本与治理文件、CI/发布流水线、文档、`/api/v1`、统一错误、限流、写审计与升级前备份 | `LICENSE`、`.github/workflows/ci.yml`、`main.py`、`docs/` |
| P6 | JSON 日志、`/metrics`、自监控与外部心跳、每日在线备份、容量统计与隔离恢复演练 | `logging_service.py`、`services/metrics.py`、`services/self_monitor.py`、`services/operations.py`、`scripts/restore_drill.py` |

当前代码版本为 `0.6.0`；`version.py`、`pyproject.toml`、`CHANGELOG.md` 与 CI 的 `v<version>` 标签检查一致。旧 `/api` 路径仍可用，正式文档列出 `/api/v1`。

## 验证与性能

- 最终本地回归：`python -m pytest -q --cov --cov-report=term --cov-report=json:coverage.json`；核心覆盖率门槛由 `scripts/check_core_coverage.py` 校验。`scripts/self_check.py` 验证登录、设备、探测、备份恢复和主要 API。
- 隔离 60 天加速场景每周执行历史清理，缩短保留窗口，模拟在线、离线、恢复；设备状态始终存在，最终为在线。
- 安全场景遍历旧版和新版 `/api/*` 路由的未授权访问，验证 401；旧版数据库升级场景验证状态与数据保留。另有锁库恢复、探测失败重试、自监控引擎停止注入、告警渠道模拟和恢复演练。
- 1000 目标模拟探测：完成 1000 次、确认在线 1000 台，单轮墙钟 0.851 秒、引擎耗时 0.760 秒、SQLite 主库和 WAL 合计 5,234,480 字节、峰值 RSS 126,369,792 字节。详见 [P6 压测](benchmarks/p6-1000-devices.md)。这不是实际网络或持续 60 天的负载测试。
- P4 浏览器检查：登录入口 Lighthouse 可访问性 100/100，已登录主要视图 axe-core 零 WCAG 违规；详见 [界面验收](qa/p4-ui-accessibility.md)。

## 安全检查

| 检查项 | 状态 | 证据或限制 |
| --- | --- | --- |
| 受保护 API 认证、只读角色、令牌吊销与登录限流 | 已验证 | P2 安全场景、全路由未授权遍历 |
| 统一错误不回显输入、写操作审计、通用 API 限流 | 已验证 | P5 API 合同测试；限流桶位于单进程内存 |
| 通知密钥加密与返回脱敏、出站地址校验 | 已验证 | 旧库迁移与 Webhook 测试；需妥善保存 `APP_SECRET` |
| CSV 公式注入与恢复文件校验 | 已验证 | P2 安全场景、自检与恢复演练 |
| 依赖漏洞、静态安全与代码质量 | 本地通过 | `pip-audit`、Bandit、Ruff、mypy、actionlint；远端 CI 尚未运行 |
| 非 root 容器、只读根文件系统与镜像扫描 | 配置已提交 | 本机未运行 Docker，也未取得远端镜像扫描结果 |
| Prometheus 鉴权、外部心跳 HTTPS 校验 | 已验证 | P6 隔离测试；真实 Prometheus 与外部心跳服务尚未联调 |

## 升级与回滚

1. 停止旧服务前安全保存整个 `data/`、环境配置和 `APP_SECRET`；确认数据库所在盘有容纳主库、WAL 和备份的空间。
2. 停止旧服务，部署 `0.6.0` 代码并保持原有 `APP_SECRET`。启动流程比较应用版本与 Alembic 版本，必要时先生成 `data/backups/monitor-pre-upgrade-*.db`，再迁移；成功后更新 `data/.app-version`。
3. 检查 `/health/ready`、认证后的 `/api/version`、设备状态、渠道投递与 `/metrics`。运行 `python scripts/self_check.py` 和 `python scripts/restore_drill.py --source data/monitor.db`；恢复演练仅在仓库所在盘创建临时副本。
4. 如需回滚，停止新服务并保留故障现场；用升级前备份恢复 `data/monitor.db`，同时恢复旧代码及对应 `data/.app-version`，保持原 `APP_SECRET`。复制数据库前必须停止服务，不能直接删除运行中的 WAL。详见 [运维手册](operations.md#备份升级与回滚)。

## 风险与遗留

- 本地验证通过；分支尚未推送，远端 GitHub Actions、amd64/arm64 镜像构建与 Trivy 扫描、GHCR 发布及干净机器五分钟部署尚无运行结果。
- P4 双语覆盖主要监控界面；系统设置和部分反馈仍为中文。已登录页面没有 Lighthouse 评分，人工键盘与屏幕阅读器验收仍需完成。
- 1000 目标测试使用立即成功的 mock。真实网络延迟、长时间高负载、通知服务故障与备份盘容量需要在实际部署环境持续观察。
- 自监控故障注入验证了调度和发送调用；真实通知渠道到达性、外部死人开关的宕机超时告警需用生产配置演练。没有通知渠道时，自监控只能写日志。
- API 限流及探测计数器为单进程内存状态。部署仍需单 worker；系统日志按当前保留策略清理，若审计需长期留存，应配置额外归档。

下一步是审核本分支差异，在外挂盘或远端 CI 环境完成部署与镜像验收，再按发布流程推送分支及 `v0.6.0` 标签。
