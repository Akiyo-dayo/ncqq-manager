# SnowLuma 登录环境修复

适用：实例确认待登录，刷新二维码仍返回 `snowluma_qr_unavailable`。

在基础信息的错误提示下选择“修复登录环境”，阅读并确认“备份并修复”。恢复能力在节点执行，HQ 仅转发；不会对共享容器执行 stop/restart。

- 已在线、登错号、未知状态均拒绝重置。
- 修复前复取一次二维码；已恢复可用就跳过重置。
- 仅停指定 supervisor program；若该 HOME 下仍有 QQ 进程，拒绝移动数据。
- 旧配置原子移动至持久卷 `/app/data/login-repair/<唯一事务>/original`。
- 新环境取码/一键登录可用或登录成功后才提交事务；失败保留现场并回滚。
- 不删除旧备份，不复制其他账号配置，不修改绑定、OneBot 或网络设置。
- 重建后通常需要重新扫码。这是可复用的恢复手段，不代表已定位/修复 QQ 上游根因。

## 诊断与回滚

`GET /api/containers/{name}/repair-login?node_id=...` 查询该账号最近任务，核对 `phase`、`backup`、`error`。任务仅保存在面板进程内，面板重启后需检查持久事务 manifest。不要在修复期间更新面板。

每个事务记录 program、uin、原 profile 路径、操作前 supervisor 状态和事务阶段。尚未提交的事务可在同一版本助手下使用 `rollback <program> <uin> <transaction>` 恢复；已提交事务必须人工明确确认后定向停止该账号、保留现 profile、还原 original，再启动并验收。不得操作共享容器，不得清空整个 HOME。

锁在单个面板进程内生效；部署保持当前单 uvicorn worker。外部手工操作、用户扫码不受此锁控制，修复前会重复检查在线状态，操作期间仍应避免并发运维和扫码。整个容器的截图/点击串行，避免共享 scratch 文件相互覆盖。

## 发布

同一镜像一处构建后分发 HQ、宁波、苏州；保留每节点 config/extractor/账号映射与 compose。本功能的事务助手随 services 打包，不依赖覆盖节点 config。不要批量重置健康实例来验收。
