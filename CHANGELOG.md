# Changelog

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