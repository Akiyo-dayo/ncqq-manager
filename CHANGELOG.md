# Changelog

## 2026-07-30 - 生命周期状态机 / 集群通讯重构，移除 BotShepherd

一次围绕"状态显示与实际不符"的系统性排查。四个互相独立的 P0 缺陷叠加，
造成了重启卡死、节点集体离线、换号后显示旧 QQ、公开接口误报在线四类现象。

### 🐛 Bug 修复

#### 1. 点击重启后永久卡在「重启中」，而容器早已正常运行

- **根因（两处，必须同时修）**：
  1. `action_jobs.monitor` 以 `inspect_fn(name, node_id)` 调用，而
     `cluster_manager.inspect_container_state_async` 的签名是 `(node_id, name)`。
     参数颠倒后，本地重启被当成"查询名为 `<容器名>` 的远程节点"，
     `_get_node()` 找不到该节点 → 恒定返回 `HTTP 404` / `status=unknown` →
     成功条件永远不成立 → 120s 后落到 `stuck`，而容器其实一直是 running。
  2. 成功分支里的 `self._notify_callback` 属性根本不存在（实际字段是 `_notify`）。
     只修第 1 条会立刻触发 `AttributeError`，被 `run()` 的兜底 `except` 吞成
     `fail()` —— **bug 会从"卡死"变成"重启失败"**。
- **修复**：统一按 `(node_id, name)` 调用；删除那四行冗余且写错的通知代码
  （`succeed()` 内部本来就会触发通知）。
- **附带**：`asyncio.create_task` 的返回值改为强引用持有。事件循环只持弱引用，
  任务可能在 await 点被 GC 静默取消，job 就会永远停在 `running`。

#### 2. 保存一次「集群设置」，整个集群立刻全部离线

- **根因**：`GET /api/cluster/config` 把集群密钥遮蔽成字面量 `"***"` 返回，
  前端 `ClusterSettings` 将其存入 state 后又用 `{...config}` 整体 POST 回来，
  而 POST 的 `allowed_keys` 恰好包含 `api_key` → 真密钥被覆写成 `"***"` →
  所有把本机当节点的面板全部 401 → 界面上表现为"节点莫名其妙全离线"，
  且每次保存设置都会复发。
- **修复**：GET 不再下发该字段（改为 `has_api_key` 布尔）；POST 显式丢弃；
  启动时检测到密钥已被写成掩码值则自动重新生成并告警。
- **配套**：新增 `GET/POST /api/cluster/key`，本机集群密钥终于可以在界面上
  查看、复制、重置 —— 此前 UI 上无处可见，运维只能去 `config/app.db` 里
  手动 `SELECT value FROM settings WHERE key='api_key'`。

#### 3. 换号后一直显示旧 QQ 号，甚至把 NapCat 的自动登录账号改回旧号

- **根因（三层）**：
  1. `container_state` 里 `from services.bot_heartbeat import bot_heartbeat_service`
     —— 这个名字不存在（模块导出的是 `bot_heartbeat`）。`ImportError` 被裸
     `except Exception: pass` 完全吞掉，注释中"仅信任心跳服务"的在线校准
     **从未执行过**，`bot_online` 一旦置 True 就再也不会因静默掉线复位。
  2. 叠加上一条，`if inst.bot_online:` 的短路分支每个 tick 都会命中并 `continue`，
     跳过 API 复核与二维码刷新，uin 退化为"按 `onebot11_*.json` 文件名推断"。
  3. 换号并不会删除旧账号的 `onebot11_<旧号>.json`，于是旧号被当成当前账号，
     进而触发用旧号改写 `webui.json` 的 `autoLoginAccount` 并自动重启容器 ——
     不只是显示陈旧，是**主动把状态改错**，且会自锁死（注入会刷新旧文件 mtime）。
- **修复**：
  - 修正导入名，异常改为 `logger.warning`，不再静默。
  - 去掉 `bot_online` 短路：它只作为"别急着判离线"的软信号，不能当登录真值。
  - 文件名推断出的 QQ 号只写入 `configured_uin`，不再污染 `last_uin`。
  - `docker_login` 中"NapCat 存活 + 二维码超时 + 有配置文件 ⇒ 已登录"的
    文件系统兜底彻底移除；`get_stats` 不再触发这条独立判定链去和状态引擎打架。
  - 登录账号变更时主动清理旧号的心跳缓存、二维码与 `configured_uin`。
  - 已登录实例的登录复核间隔 240s → 60s（退出登录不产生任何 Docker 事件，
    这个值直接决定"换号后多久才显示新号"的上限）。

#### 4. 公开接口把「WS 连着」当成「Bot 在线」

- **根因**：`/api/bots` 的 `connected` 取自 WS 注册表的 `is_alive()`，
  只表示反向 WS 链路是否存活。NapCat 进程在、WS 连着、但 QQ 已退登正在等
  扫码时，该字段仍为 `true`，外部插件据此调用 OneBot API 只会拿到失败。
  `get_entry_snapshot` 也缺少 `get_login_result` 里那段"连接死亡后清 uin"的
  逻辑，会一直吐旧 QQ 号。
- **修复**：拆成 `ws_connected`（链路）与 `bot_online`（确认登录 + 心跳新鲜）
  两个字段，`connected` 保留为兼容别名但语义改为真实在线；补上快照清理。

#### 5. 容器日志实时流永远是空的

- **根因**：`ws_router` 调用 `cluster_manager.get_logs` —— 该方法不存在
  （只有 `get_logs_async`）。`AttributeError` 被 `except (TimeoutError, Exception)`
  吞掉，本地和远程节点都推送空字符串且不报错。
- **修复**：改用 `get_logs_async`，异常按类型记日志。

#### 6. 远程节点的配置读写与文件删除实际操作的是本机

- **根因**：`container_config_router` 的四个端点接受 `node_id` 却一律读写
  本机 `get_data_dir()`。远程容器读不到配置、保存会在主控机凭空建目录，
  **删除更是会误删本机同名路径**。
- **修复**：远程请求透明转发到目标节点，由对方以 `node_id=local` 在自己磁盘执行。
- 同时补上 `delete_data` 的转发 —— 此前 UI 勾选"同时删除数据"对远程实例静默无效。

#### 7. 其他

- `cluster_manager.proxy_to_node_async` 被定义了两次，第一份调用的
  `self._proxy_to_node_async` 并不存在，靠后定义覆盖侥幸能跑。已删除。
- `_normalize_address` 用 `startswith("http")` 判协议，主机名以 `http` 开头
  （如 `httpnode.lan`）会被误判为已带协议，随后抛 `InvalidURL` 被吞成"离线"。
  改用 `://` 判断并补地址合法性校验。
- `Dockerfile` 从未 `COPY resource/`，Docker 部署下登录页背景与壁纸全部 404。
- 未匹配的 `/api/*` 会落到 SPA catch-all 返回整页 HTML，调用方 JSON 解析失败后
  报出的错误与真正原因无关。现返回 JSON 404。
- 本地节点卡片显示的实例数是**全集群总和**（`get_instance_status` 不区分节点）。
- 前端 401 后派发的 `auth:unauthorized` 事件全仓无人监听，页面数据冻结、
  每隔 10~120 秒弹一次红色错误，却始终不跳登录页。已接上，并区分游客访问
  公开面板时的 401（不误踢）。

### ♻️ 重构

#### 生命周期状态机（`services/action_jobs.py`）

- 监控与动作**并发**执行，由 `action_done` 事件把关。此前串行执行，监控启动时
  容器早已重新 running，`seen_not_running` 永远观察不到迁移，只能靠固定 15 秒
  空转凑数；并发后能真实观察 stop → start，并以 `State.StartedAt` 变化作为
  独立佐证（远程节点只是接收请求，必须自己证明重启确实发生过）。
- 重启的 stop 宽限期 60s → 10s，与 stop 一致。NapCat 不响应 SIGTERM，
  60s 意味着每次重启都要干等一分钟才真正开始。
- 非终态 job 超过 180s 不再遮盖真实 Docker 状态；GC 改为定时执行并回收被遗弃的
  job（此前只在新建 job 时触发，之后没人操作就永不回收）。
- 被取代的 job 显式转入 `superseded` 终态，不再静默泄漏在 `running`。
- 同一动作连点复用原 operation；**换动作则新建并取代旧 job** —— 否则调用方会
  拿到一个不相干的 operation_id，眼看着"重启成功"而自己的 stop 从未执行。
- stop 的判定全部以"动作已执行"为前提：排在重启后面的 stop 会看到重启自身的
  停机阶段，否则会在容器最终 running 的情况下报成功。
- 新增终态 `unknown`：监控自身崩溃说明不了容器的任何情况，不该报成"操作失败"。

#### 节点通讯层（`services/cluster_manager.py`）

- 新增统一调用入口 `NodeClient.call`，取代散落各处的硬编码超时：
  健康检查 5s / 列表 8s / 读取 10s / 生命周期 20s / 建容器 180s。
  此前健康检查与列表都是 `total=2, connect=1`，而代码注释自己写着
  "Japan panel → Suzhou node" —— 跨境链路必然反复横跳；
  远程建容器则是 5s，拉镜像必超时，返回"节点不可达"但对方其实正在创建成功。
- 瞬时连接错误重试一次；DNS / 拒绝 / TLS / 未授权等明确失败不重试。
- 失败归类为 `dns` / `refused` / `timeout` / `tls` / `unauthorized` /
  `invalid_address` / `not_a_node` 等并写回节点记录，界面因此能显示
  "集群密钥不匹配，请核对两端密钥"，而不是笼统的"离线"。
- 连续 3 次失败进入 degraded：健康检查降频，其余请求快速失败，
  避免一个挂掉的节点把每个请求都拖满超时。
- 返回结构化的 `NodeCallResult` 取代 `(code, body, ct)` 三元组 ——
  `if code == 200 and body` 这种写法正是错误被吞掉的根源。
- 本地分支不再在 async 函数里直接调同步 docker-py：打开任意容器详情页会阻塞
  事件循环最长约 7 秒，期间所有远程节点的健康检查都在超时窗口里排队，
  这是"多节点忽好忽坏"的隐蔽放大器。

#### 节点数据一致性

- `nodes` 表落一条真实的 `local` 记录。此前它只是内存里的虚拟对象，
  所有针对本地节点的 `UPDATE` 影响 0 行却返回 `ok`（改名字永远不生效），
  而同一请求里的密钥修改却是生效的。
- 新增 `created_at` / `last_ok_ts` / `last_status` / `last_error` /
  `last_error_kind` / `enabled` / `insecure_tls` 列（数据库迁移 v4 → v5）。
- 新增 `POST /api/nodes/probe` 握手探测，添加节点前强制验证，
  失败返回 422 + 分类原因（可 `force=true` 强行保存）。
  此前添加节点**零校验**：无论成败都返回绿色的 `{"status":"ok"}`。
- 节点删除改为**软删除**：重新添加同一地址复用原 `node_id`，
  普通用户对该节点实例的授权不再失效 —— 而"删了重加"恰恰是排查问题时的
  第一反应，此前会静默摧毁所有授权且管理员看不出异常。
- 删除时级联清理内存实例。此前已删除节点的容器因为不再被查询，
  会以绿色 `running` 永久滞留在面板上直到进程重启。
- 地址重复检测；新增节点级离线 / 恢复告警。

#### 状态刷新链路

- 状态引擎每 tick 对每个运行中容器**串行**执行 3~4 次 `docker exec`
  （二维码新鲜度探测 + uin 探测），10 个实例就能把一次 tick 拖到几十秒，
  期间 `_signal_push` 不会被调用，所有 WS 连接只能收心跳。
  改为 `gather` + 信号量并发，并按登录态加 TTL 缓存。
- `notify_change` 改用 `call_soon_threadsafe`：Docker 事件监听跑在独立线程，
  而 `asyncio.Event.set()` 不是线程安全的，唤醒可能丢失，
  刷新退化到最长 240s 的兜底间隔。
- 心跳上线 / 掉线时主动唤醒引擎（这类变化不产生任何 Docker 事件）。
- WS 去重快照补齐 `action_phase` / `display_status` / `login_stage` / `stale`：
  此前只比对 `status/uin/node_id/bot_online`，重启前后容器都是 running、
  uin 不变 → 快照相同 → 只发心跳，**第二个管理员永远看不到「重启中」，
  卡住的状态也永远不会自我纠正**。
- 公开 WS 的版本号剔除 `age_seconds` / `expires_in` 这类每 tick 必变的
  时间衍生字段，去重不再形同虚设。
- 修复 `_signal_push` 的丢唤醒竞态：慢客户端在 `send` 期间错过唤醒要白等一轮
  （240s 档下就是多等 4 分钟）。改为版本号比对。
- 管理端 `/ws/events` 与公开端 `/ws/public` 的连接名额分开计数，
  管理员的每个标签页不再挤占匿名用户的配额。
- 远程节点失联的实例标记 `stale`，`status` 降级为 `unknown`、`bot_online` 归零。
  此前节点宕机半小时，面板上它的容器仍是鲜绿的 `running`。

### 🗑️ 移除 BotShepherd

- 删除 submodule、`services/botshepherd.py`、`services/bs_activation_service.py`、
  `routers/botshepherd_router.py`、前端页面、i18n 词条、配置项
  （`init_bs_*` / `manager_host` / `manager_port`）、`Dockerfile` / `start.py` /
  文档中的相关引用。
- **Bot 雷达迁移并保留**：端点探测、端点库、注入到实例移到独立的
  `services/bot_radar.py` + `routers/bot_radar_router.py`（前缀 `/api/bot-radar`），
  并补充完整的使用引导（用途说明、三步工作流、探测结果解读、别名的自动化用法）。
  仅移除"注入到 BS"与"从 BS 自动收集"两个功能。
- 登录后钩子（WS 客户端注入、`autoLoginAccount` 同步、自动加群通知）与
  Bot 心跳服务均非 BS 专属，完整保留。注入标记目录仍沿用 `.bs_injected`
  —— 改名会让所有存量实例被判成"未注入"，从而重新注入并重启一遍容器。

### 🎨 用户体验

- WS 达到连接上限（4429）后**永久不重连**，用户只能手动刷新页面。改为长退避重连。
- 旧 socket 的 `onclose` 异步触发时新连接可能已 open，会把新连接状态打成断开
  并触发多余重连（自我踩踏）。加实例校验。
- 两个 WS hook 新增 `visibilitychange` 恢复：手机切后台、锁屏、笔记本合盖回来后
  立即重连并拉一次 HTTP 兜底，不再等最长 60s 退避（期间界面是旧数据，
  指示灯却仍显示"已连接"）。
- 用户面板重启后二维码永久卡在「刷新中」：effect 依赖 `qrCodes`，每次 WS 推送都会
  清掉 6 个轮询定时器，而 key 已被标记所以不再重排也永不删除。已重构为
  `AbortController` + 单循环。
- 前端不识别后端的 `need_auth` 状态（有码但需登录才能看），落到兜底分支一直转圈；
  WS 推送还会无条件覆盖用户手动刷出的二维码造成回弹。均已修复。
- 批量操作使用全局节点选择（默认 `'all'`）**必定失败**，且只存容器名导致
  跨节点同名容器互相串。改为逐项携带各自的 `node_id`。
- `pause` / `unpause` / `kill` / `delete` 点击后无 loading 无禁用，可重复点击；
  删除确认按钮同样没有防重入。已统一处理。
- 实例详情页自己轮询容器状态后**无条件**报成功，与列表页结论可能相反。
  改为以 operation 为唯一事实源，按最终 phase 区分成功 / 失败 / 卡住 / 未知。
- `Toast` 用 `Date.now()` 作 key，同毫秒会撞车导致提示互相吞掉；
  分页在列表缩短后停在空白页且分页控件被隐藏，只能刷新页面。
- 错误提示：FastAPI 返回的是 `{detail}` 而非 `{message}`，此前所有具体原因
  （"该地址已经添加为节点…"、"操作过于频繁…"）都被丢成裸的 `HTTP 400`。

### ✅ 验证

在测试服务器（Ubuntu 22.04 / Docker 29.6）部署双面板节点实测：

| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| 本地重启 | 120s 后 `stuck`，`error: HTTP 404` | 13s `succeeded` |
| 本地停止 | 立即"成功"但容器仍在运行 | 12s `succeeded`，容器 `exited` |
| 远程重启 | 无法判定成功 | 15s `succeeded` |
| 远程建容器 | 5s 超时报"节点不可达" | 正常返回 |
| 保存集群设置 | 密钥变成 `"***"`，节点全离线 | 密钥不变 |
| 错误密钥加节点 | 静默入库，显示"离线" | 422 + "集群密钥不匹配" |
| 节点失联 | 容器仍显示绿色 `running` | `status=unknown`, `stale=true` |
| 删除节点 | 容器永久滞留，用户授权失效 | 级联清理，重加复用原 ID |
| 容器日志流 | 永远为空 | 正常输出 |
| 重启中插入停止 | 停止报成功但容器在运行 | 重启转 `superseded`，停止在 `exited` 后才成功 |

| 涉及文件 | 变更 |
|----------|------|
| `services/action_jobs.py` | 生命周期状态机重写：并发监控、`StartedAt` 佐证、超时兜底、GC |
| `services/cluster_manager.py` | 统一 `NodeClient` 调用层、错误分类、熔断、软删除、握手探测 |
| `services/container_state.py` | 并发容器探测、修正心跳导入、去除 uin 短路、线程安全唤醒 |
| `services/container_instance.py` | `stale` 标记、`configured_uin` 语义、换号清理、状态矛盾修复 |
| `services/bot_heartbeat.py` | 跨节点心跳回写、`forget()`、主动唤醒引擎 |
| `services/bot_radar.py` | 新增 — 从 BotShepherd 迁出的端点探测 / 端点库 / 注入 |
| `services/docker_async.py` | 新增 `inspect_state`、重启宽限期、移除 BS 检测层 |
| `services/docker_login.py` | 移除会主动改错状态的文件系统兜底 |
| `services/docker_lifecycle.py` | 登录后钩子去 BS 化，保留 WS 注入与自动加群 |
| `services/database.py` | 迁移 v5：`nodes` 表健康字段 + 真实 local 记录 |
| `routers/node_router.py` | 密钥端点、握手探测、软删除、拒绝写入 `api_key` |
| `routers/container_runtime_router.py` | job 强引用、`/state` 端点、去重、重建清理 |
| `routers/container_config_router.py` | 远程节点透明转发（此前会误删本机路径） |
| `routers/bot_api_router.py` | `ws_connected` / `bot_online` 拆分，跨节点去重 |
| `routers/bot_radar_router.py` | 新增 — `/api/bot-radar/*` |
| `routers/ws_router.py` | 快照字段补齐、版本号去时间化、日志流修复 |
| `frontend/src/hooks/*.ts` | 重连策略、`visibilitychange`、陈旧 socket 守卫 |
| `frontend/src/pages/*.tsx` | 节点探测 UI、本机密钥卡片、雷达引导、批量操作、stale 展示 |
| `frontend/src/services/api.ts` | 错误 `detail` 透传、集群/雷达接口、`stale` 字段 |

## 2026-06-10 - QQ 号隐私显示与头像 / API 原始 UIN 修复

### 🐛 Bug 修复

#### 1. 公共 API / WS 返回 QQ 号被脱敏
- **问题**：公开容器列表和公开 WebSocket 会把 `uin` 打码，导致外部服务无法拿到完整 QQ 号，也影响后续基于 QQ 的查询能力。
- **修复**：后端 API / WS 不再对 QQ 号做脱敏处理，返回原始 `uin` / `last_uin`。
- `GET /api/public/containers` 新增透传 `configured_uin` 字段，方便外部服务读取配置态 QQ。

#### 2. 未登录页面仍需保护 QQ 号展示
- **问题**：后端脱敏移除后，公开页面未登录访问时不应直接展示完整 QQ 号。
- **修复**：脱敏逻辑移动到前端 `UserDashboard` 展示层：
  - 未登录浏览器：仅文本显示打码 QQ。
  - 已登录管理端：显示完整 QQ。
  - 搜索、头像 URL、API 数据和 WS 数据始终使用完整 QQ，不受展示层脱敏影响。

#### 3. NapCat 内部 `uid` 被误判为 QQ 号
- **问题**：NapCat WebUI 登录接口可能返回内部短 `uid`（例如 `77` / `9264`），旧逻辑会把它当 QQ 号，导致页面显示短号且头像请求错误。
- **修复**：登录检测不再把 `uid` 当 QQ UIN；仅接受 `user_id` / `self_id` / `uin` / `qq` / `account` 等真实账号字段。
- UIN 归一化新增短号过滤，避免内部短 ID 污染状态引擎。

### ✅ 验证
- 苏州入口 `http://jp.akiyo.fun:18080/api/public/containers` 返回完整 QQ 号，头像 `/api/resource/avatar/<QQ>` 返回 `image/jpeg 200`。
- 日本面板入口 `http://api.akiyo.fun:8000/api/public/containers` 返回完整 QQ 号，头像 `/api/resource/avatar/<QQ>` 返回 `image/jpeg 200`。

| 涉及文件 | 变更 |
|----------|------|
| `routers/container_public_router.py` | 公共容器 API 返回原始 QQ，并增加 `configured_uin` |
| `routers/ws_router.py` | 公共 WS 保留原始 QQ，不再打码 payload |
| `frontend/src/pages/UserDashboard.tsx` | 未登录仅显示层打码；已登录显示完整 QQ；搜索/头像继续使用原始值 |
| `services/docker_async.py` | 登录检测忽略 NapCat 内部 `uid`，过滤短 ID |
| `services/docker_login.py` | 同步登录检测路径增加短 ID 过滤 |

## 2026-04-13 - 状态刷新体系重构 & 远程重启 / last_uin 修复

### 🐛 Bug 修复

#### 1. 远程实例重启提示失败但实际成功
- **根因**：`cluster_manager.proxy_to_node_async` 默认超时 5s，Docker restart 先 graceful stop（最多 10s）再 start，总耗时远超 5s → 超时返回 502 → 前端显示 ✗
- **修复**：`action_container_async` 对 restart/stop 传 `timeout=30s`，start 等操作 `10s`

#### 2. last_uin 每次 tick 被清空 — 掉线后无法显示最后登录 QQ 号
- **根因**：`_tick_once` 用 Docker API 返回的空数据（`list_local_containers` 仅含 `{id,name,status,image,created}`）调用 `upsert(uin="", last_uin="", ...)`，覆盖了 `update_login()` 写入的值
- **修复**：upsert 仅传 Docker 实际提供的基础字段；登录 / 心跳字段改为 `if "field" in c` 条件传入，本地容器这些字段由 `update_login` / `update_bot_heartbeat` 独立管理

#### 3. 登录检测失败时误显示"待登录"
- **根因**：登录检测 5 级联全部异常时，`login_stage` 保持 `"waiting"` 与正常待登录混淆
- **修复**：新增 `login_stage = "unknown"` 状态；前端用橙色标签显示"状态未知"，与蓝色"待登录"区分

#### 4. 登录页缺少返回按钮 & 标签误导
- 登录页新增返回首页按钮
- 公共面板未登录容器标签从"待登录"改为"未登录"，避免与管理员面板措辞混淆

### ⚡ 性能优化

#### 状态引擎降频 — 解决高频刷新导致数据不完整
- **问题**：3-10s 的 tick 间隔 + 3s WS 推送，网络延迟/丢包时实例状态尚未获取完就进入下一轮
- **修复**：
  - 后端 tick 间隔：3-10s → **10-240s**（乘法退避 ×1.5）
  - 已登录实例 TTL：60s → **240s**
  - 新增 `POST /api/containers/refresh` 手动刷新 API（`speed_limit(2.0)`），唤醒引擎立即 tick

#### WS 推送改为事件驱动
- **问题**：WS 端点 `asyncio.sleep(30)` 硬等待，用户操作后最多 30s 才更新
- **修复**：
  - state_engine 新增 `_push_event` 广播机制 + `wait_push()` 方法
  - tick 完成后 `_signal_push()` 唤醒所有 WS 循环（Event 替换模式，多消费者安全）
  - WS 循环改用 `await state_engine.wait_push(timeout=30)` — 有变化毫秒级推送，无变化 30s 心跳兜底

#### 前端配套优化
- WS 心跳超时：25s → **90s**（匹配新推送间隔）
- WS 最大重连间隔：30s → **60s**
- HTTP 回退轮询：30-240s → **10-120s**（指数退避 ×1.5）
- 刷新按钮调用 `forceRefresh()` 唤醒后端引擎 + HTTP 拉取
- Dashboard 离线实例使用 `last_uin` 显示灰度半透明头像 fallback

### 🎯 效果
- 远程实例 restart/stop 不再假失败
- Bot 掉线后管理面板正确展示最后登录 QQ 号 + 灰度头像
- 用户操作后状态更新从最慢 30s 降至毫秒级
- 空闲时自动降频至 4 分钟，大幅减少 Docker API / 网络开销
- 实例详情页（BasicInfo）保持原有 15s/60s 高频刷新不变

| 涉及文件 | 变更 |
|----------|------|
| `services/container_state.py` | tick 10-240s 退避 + `_push_event` 广播 + `wait_push` / `_signal_push` + upsert 条件传入登录字段 |
| `services/cluster_manager.py` | `action_container_async` restart/stop timeout=30s |
| `routers/container_crud_router.py` | +`POST /api/containers/refresh` 手动刷新端点 |
| `routers/ws_router.py` | WS 循环改用 `state_engine.wait_push()` 事件驱动 |
| `services/docker_async.py` | 登录检测失败时 `login_stage="unknown"` |
| `frontend/src/layouts/AdminLayout.tsx` | `forceRefresh()` + HTTP 回退 10-120s |
| `frontend/src/services/api.ts` | +`containerApi.forceRefresh()` |
| `frontend/src/hooks/useWebSocket.ts` | 心跳 90s / 重连 60s |
| `frontend/src/hooks/usePublicWebSocket.ts` | 同上 |
| `frontend/src/pages/Dashboard.tsx` | last_uin fallback + 灰度头像 + 状态未知标签 |
| `frontend/src/pages/UserDashboard.tsx` | "未登录"标签 + 状态未知橙色标签 |
| `frontend/src/pages/Login.tsx` | +返回首页按钮 |
| `frontend/src/i18n.ts` | +statusUnknown / notLoggedIn 中英翻译 |


## 2026-04-12 - 容器识别可配置 & 远程节点闪烁修复

### 🐛 Bug 修复

#### 1. 容器识别关键词不可自定义 — `napcar3` 无法被识别
- **根因**：过滤逻辑硬编码 `"napcat"` 子串匹配，容器名 `napcar3` 不含 `napcat` 被忽略
- **修复**：新增 `container_keywords` 运行时配置项（默认 `["napcat"]`），容器名或镜像名匹配任一关键词即纳入管理
- 前端集群设置页新增「容器识别关键词」输入框
- 后端 API GET/POST `/api/cluster/config` 均已同步支持

#### 2. 远程节点超时导致容器列表闪烁 / 丢失
- **根因**：远程节点获取超时时 `remote_containers=[]`，`cleanup()` 清除该节点全部容器 → WS 推送缺失 → 下次 tick 又恢复
- **修复**：
  - `cluster_manager.list_remote_containers_async()` 返回值改为 `(containers, responded_node_ids)`
  - `cleanup()` 新增 `cleanable_nodes` 参数，仅清理成功响应节点的容器
  - 本地 Docker 连续失败 ≥5 次才强制清理（避免僵尸容器堆积）

### ⚡ 优化
- 容器过滤中的 `get_container_keywords()` 调用移至循环外，减少 N 次冗余调用

| 涉及文件 | 变更 |
|----------|------|
| `services/config.py` | +`container_keywords` 运行时配置 + `get_container_keywords()` 工具函数 |
| `services/docker_manager.py` | 同步容器列表过滤改用可配置关键词 |
| `services/docker_async.py` | 异步容器列表过滤改用可配置关键词 |
| `services/cluster_manager.py` | `list_remote_containers_async()` 返回响应节点集合 |
| `services/container_state.py` | 节点级 cleanup + 本地失败计数器 |
| `services/instance_subsystem.py` | `cleanup()` 支持 `cleanable_nodes` 参数 |
| `routers/node_router.py` | GET/POST cluster config 增加 `container_keywords` |
| `frontend/src/pages/ClusterSettings.tsx` | 新增容器识别关键词输入 |
| `frontend/src/i18n.ts` | 中英文翻译 |


## 2026-04-12 - 多节点同名容器修复 & 前端缓存 / 状态持久化优化

### 🐛 Bug 修复

#### 1. 默认节点选择错误
- Dashboard 默认 `selectedNode` 从 `'local'` 改为 `'all'`（所有节点）
- URL 持久化条件同步调整

#### 2. 多节点同名容器覆盖消失
- **后端**：`instance_subsystem` 字典键从 `name` 改为 `node_id:name` 复合键
  - `get()` / `exists()` / `upsert()` / `remove()` 均增加 `node_id` 参数（默认 `"local"`）
  - `cleanup()` 改为接收 `set[tuple(node_id, name)]`
- `container_state.py`、`ws_router.py` 调用方同步传递 `node_id`
- **前端**：Dashboard 容器卡片在"所有节点"视图下显示节点名称标签

#### 3. 前端同步延迟 / 浏览器缓存残留
- **后端**：新增 `NoCacheAPIMiddleware`，所有 `/api/` 响应设置 `Cache-Control: no-store`
- **前端**：`api.ts` 的 `request()` 函数添加 `cache: 'no-store'`
- 自适应刷新间隔从 3-30s（步进 3）缩短为 3-10s（步进 2）

#### 4. 节点选择状态导航丢失
- Dashboard → ConfigEditor 导航时携带 `?node=` 参数
- ConfigEditor 返回按钮 / BasicInfo 删除后导航均保留 `?node=` 参数

### ✅ 验证
- 普通用户删除权限验证：前端 `isAdmin` 门控 + 后端 `_USER_ALLOWED_ACTIONS` 白名单，已正确无需修改

| 涉及文件 | 变更 |
|----------|------|
| `services/instance_subsystem.py` | 复合键重构，所有方法增加 `node_id` 参数 |
| `services/container_state.py` | upsert/cleanup 传递 `node_id`，缩短自适应间隔 |
| `routers/ws_router.py` | public WS 分页模式传递 `node_id` |
| `main.py` | 新增 `NoCacheAPIMiddleware` |
| `frontend/src/pages/Dashboard.tsx` | 默认节点 `'all'` + 节点标签 + URL 参数传递 |
| `frontend/src/pages/ConfigEditor.tsx` | 返回按钮保留 `?node=` 参数 |
| `frontend/src/components/BasicInfo.tsx` | 删除后导航保留 `?node=` 参数 |
| `frontend/src/services/api.ts` | fetch `cache: 'no-store'` |


## 2026-04-12 - 节点下拉状态显示优化（quick + full）

### ✅ 改进
- `Dashboard` 节点下拉改为两阶段加载：
  1. 先请求 `GET /api/nodes?quick=true` 秒开
  2. 再异步请求 `GET /api/nodes` 覆盖真实在线状态
- 将 `unknown` 节点状态从红点调整为灰点，避免被误判为离线

### 🎯 效果
- 首屏速度保持不变
- 节点状态会在短时间内自动从占位态更新为真实在线态
- 减少“远程节点都红点但实际可用”的误导

| 涉及文件 | 变更 |
|----------|------|
| `frontend/src/pages/Dashboard.tsx` | quick->full 二次刷新 + unknown 灰点 |


## 2026-04-12 - 集群跨节点登录态同步修复

### 🐛 问题
- 日本面板通过集群接入苏州节点时 列表页长期显示"待登录/等待生成"
- 进入实例详情又能看到正确登录信息 造成页面状态不一致

### ✅ 修复
- **修复远程 QR 代理路径**
  - `services/cluster_manager.py`
  - `get_qr_status_async()` 从错误的 `/api/qr/{name}` 改为
    `/api/containers/{name}/qrcode?node_id=local`
- **修复远程容器字段同步缺失**
  - `services/container_state.py`
  - 状态引擎 upsert 远程容器时 同步写入：
    - `uin`
    - `last_uin`
    - `bot_online`
    - `bot_heartbeat_ts`
    - `login_stage`
    - `login_method`
  - 同时推导 `logged_in`：
    - `login_stage == "logged_in"` 或 `bot_online == true`

### 🎯 效果
- 集群列表页与实例详情页登录态一致
- 同一份 ncqq-manager 项目可在不同服务器部署并稳定互联

| 涉及文件 | 变更 |
|----------|------|
| `services/cluster_manager.py` | 远程二维码状态代理路径修正 |
| `services/container_state.py` | 远程实例登录字段同步 + logged_in 推导 |


## 2026-04-11 - 安全加固 & 注册审核系统 & 离线QQ号展示

### 🔒 公开页面 QR 码安全加固

未登录用户通过公开端点（HTTP / WebSocket）不再能获取二维码图片数据。

- **新增** `to_qr_dict_public()` — 公开 QR 字典，QR URL 替换为 `status: "need_auth"`
- **新增** `get_qr_states_public()` 在 `InstanceSubsystem` 和 `ContainerStateEngine` 中委托
- **修改** `/api/public/qr/batch` 和 `/ws/public` 使用公开 QR 接口
- **前端** `UserDashboard` 处理 `need_auth` 状态，提示用户登录后扫码

| 涉及文件 | 变更 |
|----------|------|
| `services/container_instance.py` | +`to_qr_dict_public()`, `to_public_dict()` 增加 last_uin |
| `services/instance_subsystem.py` | +`get_qr_states_public()` |
| `services/container_state.py` | +`get_qr_states_public()` 委托 |
| `routers/container_public_router.py` | 公开批量 QR 换用 public 方法 |
| `routers/ws_router.py` | `/ws/public` 推送换用 public 方法 |
| `frontend/src/pages/UserDashboard.tsx` | 处理 `need_auth`，显示登录提示 |

### 📱 离线 QQ 号展示（last_uin）

NapCat 掉线后仍保留并展示最近登录的 QQ 号，方便识别实例。

- **新增** `last_uin` 字段 — `update_login()` 在清除 uin 前保存，`clear_runtime()` 不清除
- **前端** 离线时用 `last_uin` 显示 QQ 号 + 灰度头像 + "(离线)" 标签
- 该字段不参与在线状态判断

| 涉及文件 | 变更 |
|----------|------|
| `services/container_instance.py` | +`last_uin` 字段，持久化逻辑 |
| `frontend/src/services/api.ts` | Container 接口 +`last_uin` |
| `frontend/src/pages/UserDashboard.tsx` | 离线 QQ 号 + 灰度头像渲染 |

### 📝 注册审核系统

新增完整的用户注册申请 + 管理员审核工作流。

**后端：**
- **新增** `routers/registration_router.py` — 7 个端点（公开注册 + 管理端）
  - `POST /api/register` — 提交申请（0.2s 限流），支持被拒后重新申请
  - `GET /api/register/status` — 查询注册开关
  - `GET/POST/DELETE /api/registration-requests/*` — 管理员分页查询、通过、拒绝、删除
- **新增** `registration_requests` 数据表 + v4 迁移（含 status/requested_at 索引）
- 通过的用户自动创建为 USER 权限，不分配实例

**前端：**
- `Login.tsx` — 登录/注册模式切换，含密码确认、长度校验
- **新增** `RegistrationReview.tsx` — 管理员审核页面（分页、状态过滤、通过/拒绝弹窗/删除）
- `AdminLayout.tsx` — 侧边栏新增"注册审核"入口 + 待审核红色徽章（30s 轮询）
- `App.tsx` — 路由 `/admin/registration-review`
- `api.ts` — `publicApi.register/registerStatus` + `registrationApi` 全套管理接口
- `i18n.ts` — 中英文翻译完整覆盖

| 涉及文件 | 变更 |
|----------|------|
| `routers/registration_router.py` | **新文件** 注册路由 |
| `services/database.py` | +注册表 schema + v4 迁移 |
| `main.py` | 挂载 registration_router |
| `frontend/src/services/api.ts` | +publicApi 注册 + registrationApi |
| `frontend/src/pages/Login.tsx` | 登录/注册切换 |
| `frontend/src/pages/RegistrationReview.tsx` | **新文件** 管理审核页 |
| `frontend/src/App.tsx` | +路由 |
| `frontend/src/layouts/AdminLayout.tsx` | +侧边栏入口 + Badge |
| `frontend/src/i18n.ts` | +中英文翻译 |


## 2026-04-11 - ws/public sync stability fix
- Fix: resolve ws/public payload version calculation order bug (payload used before assignment), which caused websocket to close immediately.
- Improve: trigger state refresh on /internal/login-event to reduce front-end state lag after scan/login transitions.
- Improve: public ws version now based on payload content instead of tick-only gating.
- Improve: set no-cache headers for index route to reduce stale frontend bundle/cache issues after deploy.


## 2026-04-11 - login-state truth fix
- Fix: demote sdk_ws and filesystem as non-authoritative login sources to prevent false logged-in state
- Fix: dropped stale login cache short-circuit in QR route
- Improve: offline+qr-refresh containers now consistently show waiting and expose qr url


## [Hotfix-3] - 2026-04-11

### 登录检测架构升级 — 五级级联 + 容器内 exec 强确认

#### 🐛 回归修复

Hotfix-2 的 `qr_from_this_session` 作为硬否决条件导致新回归：用户扫码登录成功后 `qrcode.png` 仍存在且 mtime > 容器启动时间，该标记永远为 True → Level 4 filesystem 永远返回 `logged_in: False` → 已登录的 bot 显示未登录。

#### ✨ 改进

- **新增 Level 3.5：容器内 OneBot 检测** — `check_login_via_container_exec()` 通过 `docker exec` 在目标容器内直接请求 `127.0.0.1:3000/get_login_info`，绕过端口映射（`http_port=0`）和宿主机网络（WS 403）限制，是最可靠的登录确认手段
- **Level 4 filesystem 降级为辅助信号** — `qr_from_this_session` 不再作为硬否决条件。产生过 QR 时返回 `stage: ambiguous` + `reason: filesystem_ambiguous`，交由 Level 3.5 做强确认；仅在无本次 QR + WebUI 活跃 + 有 uin 时作为弱信号判定已登录（token 自动登录场景）
- **`_qr_stale_via_container_fs` 防御性改进** — 未知状态（无文件/无输出）默认值从 `True`(stale) 改为 `False`(not stale)，不再倒向"已登录"方向

#### 🔄 五级级联检测流程

| 级别 | 方法 | 适用场景 | 开销 |
|------|------|----------|------|
| 1 | SDK WS 直连 | WS 已连接 | 零 |
| 2 | BS 账号 API | BS 运行时 | 10s 缓存 |
| 3 | OneBot HTTP (宿主机端口) | http_port > 0 | 2s timeout |
| 3.5 | **容器内 exec** (127.0.0.1:3000) | http_port=0 / WS 403 | 4s timeout |
| 4 | 文件系统辅助 | 仅无本次 QR 时 | docker exec |

#### 📁 变更文件

| 文件 | 变更 |
|------|------|
| `services/docker_async.py` | +Level 3.5 容器内exec, filesystem降级为辅助, stale默认值修复 |

---

## [Hotfix-2] - 2026-04-11

### 移动端适配 + 登录检测误判修复

#### 🐛 Bug 修复

- **移动端侧边栏自适应** — `AdminLayout` 侧边栏在移动端（`<md`）自动隐藏，改为 `temporary` 抽屉模式，点击左上角汉堡按钮展开，选择菜单项或点击遮罩层后自动收起，不再霸占屏幕空间
- **文件系统兜底登录误判** — 重写第4级 filesystem fallback 检测逻辑：不再以「qrcode.png 超过 60s 未刷新」作为已登录依据。新逻辑比对 qrcode.png mtime 与容器 Docker `StartedAt` 时间戳，区分「本次会话产生的二维码（token 失效进入扫码态，QR 过期）」和「上次会话残留 / 无二维码（token 自动登录成功）」，消除容器重启后误判假在线
  - 新增 `_get_container_started_at()` — 通过 Docker API 获取容器启动时间
  - 新增 `_qr_from_this_session_via_container_fs()` — 容器内部对比 QR mtime 与 `/proc/uptime`
  - Docker API 失败时自动回退到容器内部判断，避免遗漏

#### 📁 变更文件

| 文件 | 变更 |
|------|------|
| `frontend/src/layouts/AdminLayout.tsx` | +移动端 temporary 抽屉 + 汉堡按钮 |
| `services/docker_async.py` | 重写 filesystem fallback 检测 + 2 个新方法 |

---

## [Hotfix] - 2026-04-11

### 安全加固回归修复

#### 🐛 Bug 修复

- **公开端点头像恢复** — 撤销公开端点（`/public/containers`、`/public/qr/batch`、`/public/containers/page`、`/ws/public`）的 QQ 号脱敏，恢复返回完整 uin，修复前端因脱敏后 uin 无效而无法加载头像的问题
- **登录后权限立即生效** — `Login.tsx` 登录成功后调用 `AuthContext.refresh()` 同步用户权限，解决管理员登录后需手动刷新才能看到完整菜单的问题

#### 📁 变更文件

| 文件 | 变更 |
|------|------|
| `routers/container_public_router.py` | 撤销 3 个公开端点的 uin 脱敏 |
| `routers/ws_router.py` | 撤销 `/ws/public` 分页+全量模式的 uin 脱敏 |
| `frontend/src/pages/Login.tsx` | +登录后 `await refresh()` 同步 AuthContext |

---

## [Security] - 2026-04-10

### 用户权限体系安全加固

本次更新全面修复了用户权限管理漏洞，确保普通用户仅能访问分配给自己的实例，无法越权查看或操作管理员功能。

---

### 🔴 P0 — 越权访问修复

#### 后端

- **container_config_router** — 所有 4 个配置读写/文件管理端点增加 `check_instance_permission` 实例权限校验
- **container_crud_router** — `GET /api/containers` 普通用户仅返回绑定的实例（管理员返回全量）；`inject-ws-client` 增加实例权限检查
- **node_router** — 5 个敏感端点（集群配置、集群状态、节点列表、节点日志、代理转发）全部升级为 `require_admin`
- **container_runtime_router** — QR 二维码、刷新登录状态两个端点从无认证改为需登录 + 实例权限检查
- **container_runtime_router** — 容器操作增加白名单，普通用户仅允许 `start` / `stop` / `restart`，禁止 `pause` / `unpause` / `kill` / `delete`

#### WebSocket

- **ws_router `/ws/events`** — 普通用户仅推送其绑定的实例状态（管理员推送全量）
- **ws_router `/ws/logs/{name}`** — 增加 `check_instance_permission` 实例权限检查

### 🟡 P1 — 敏感信息泄露修复

- **公开端点 uin 脱敏** — `container_public_router` 三个公开端点、`ws_router /ws/public` 的 QQ 号全部脱敏处理（如 `385***633`），支持全长度 QQ 号安全脱敏
- **短号兜底** — `_mask_uin` 函数处理全部边界情况（空串 → `""`、1位 → `*`、3位 → `1*3`、6位 → `1****6`）

### 🟢 前端权限控制

- **新增 `AuthContext`** — 全局认证上下文，通过 `/auth/status` 获取当前用户身份和权限等级
- **路由守卫** — `RequireAdmin` / `RequireAuth` 组件保护管理员专属路由（集群设置、节点管理、用户管理、镜像、告警、备份、定时任务、BotShepherd、Bot 雷达、操作日志）
- **AdminLayout 侧边栏** — 普通用户仅显示「托管实例」菜单，管理员专属菜单项自动隐藏
- **ConfigEditor** — 普通用户仅可见「基本信息」和「NapCat 日志」Tab，隐藏「网络配置」和「文件管理」
- **Dashboard** — 普通用户隐藏 `pause` / `unpause` / `kill` / `delete` 操作按钮、批量操作和新建实例按钮；节点选择器仅管理员可见

### 📁 变更文件

| 文件 | 变更 |
|------|------|
| `routers/container_config_router.py` | +权限检查 |
| `routers/container_crud_router.py` | +列表过滤 +权限检查 |
| `routers/node_router.py` | 5端点→require_admin |
| `routers/container_runtime_router.py` | +认证 +操作白名单 |
| `routers/container_public_router.py` | +uin脱敏 |
| `routers/ws_router.py` | +权限过滤 +uin脱敏 |
| `frontend/src/contexts/AuthContext.tsx` | 新增 |
| `frontend/src/App.tsx` | +路由守卫 |
| `frontend/src/layouts/AdminLayout.tsx` | +菜单权限过滤 |
| `frontend/src/pages/ConfigEditor.tsx` | +Tab权限显隐 |
| `frontend/src/pages/Dashboard.tsx` | +操作按钮权限 |
| `frontend/src/pages/Login.tsx` | +角色跳转 |