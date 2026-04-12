# Changelog

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