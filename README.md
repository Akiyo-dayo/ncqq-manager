# NapCat QQ Manager

<p align="center">
  <strong>NapCat 容器管理面板</strong><br>
  优雅地管理 NapCat QQ Bot Docker 容器生命周期
</p>

---

## ✨ 功能特性

- 🐳 **容器管理** — 一键创建、启动、停止、重启、删除 NapCat Docker 容器
- 📱 **扫码登录** — WebUI 内直接展示二维码，扫码即可登录 QQ Bot
- 🌐 **多节点集群** — 支持多台服务器的远程节点管理，统一面板操控
- 🔧 **配置管理** — 在线编辑 OneBot11 网络配置（HTTP/WS/SSE 服务端与客户端）
- 📁 **文件管理** — 在线浏览和编辑容器配置文件与插件
- 🐋 **镜像管理** — 列出、拉取、删除本地 Docker 镜像
- 👥 **用户系统** — 管理员/普通用户分权，普通用户仅可管理自己的实例
- 📊 **实时监控** — CPU/内存使用率实时图表，节点延迟检测
- 📝 **操作日志** — 完整的操作审计记录
- ⏰ **定时任务** — 支持定时重启等自动化运维
- 🔔 **告警系统** — 容器异常自动 Webhook 通知（支持实例离线检测）
- 💾 **备份恢复** — 数据库一键导出与上传恢复
- 🛡️ **安全防护** — CSRF/SSRF 防护、IP 封禁、bcrypt 密码加密、随机初始密码
- 📡 **Bot 雷达** — 登记并探测 Bot 框架（AstrBot/NoneBot/Koishi 等）的 OneBot 端点，一键注入到任意实例
- 🌙 **深色模式** — 自动适配系统主题
- 🌍 **国际化** — 中文 / English 双语支持

## 🛠️ 技术栈

| 层级 | 技术 |
|------|------|
| **后端** | Python 3.10+ · FastAPI · Uvicorn · aiodocker · aiohttp · orjson |
| **前端** | React 18 · TypeScript · Vite · Material UI (MUI) |
| **数据库** | SQLite WAL（零配置，自动迁移） |
| **容器化** | Docker · Docker Compose |

## 🚀 快速开始

### 方式一：Docker Compose（推荐）

```bash
git clone https://github.com/Akiyo-dayo/ncqq-manager.git
cd ncqq-manager
docker compose up -d
```

打开浏览器访问 `http://localhost:8000`，按引导完成初始化设置。

### 方式二：手动部署

**环境要求**：Python 3.10+、Node.js 16+、Docker

```bash
git clone https://github.com/Akiyo-dayo/ncqq-manager.git
cd ncqq-manager

# 一键启动（自动安装依赖 + 构建前端 + 启动服务）
python start.py
```

或手动分步执行：

```bash
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 方式三：Ubuntu systemd 自启动（uv）

```bash
git clone https://github.com/Akiyo-dayo/ncqq-manager.git
cd ncqq-manager

# 首次启动前建议先手动执行一次，完成依赖安装/前端构建/初始化
uv run python start.py

# 注册 systemd 开机自启动
sudo bash scripts/install_autostart_ubuntu.sh
```

卸载自启动：

```bash
sudo bash scripts/uninstall_autostart_ubuntu.sh
```

## 📁 项目结构

```
ncqq-manager/
├── main.py                 # FastAPI 应用入口
├── start.py                # 一键启动脚本
├── requirements.txt        # Python 依赖
├── Dockerfile              # Docker 构建文件
├── docker-compose.yml      # Docker Compose 编排
├── services/               # 业务服务层
│   ├── docker_manager.py   # Docker 容器操作
│   ├── docker_async.py     # aiodocker 纯异步 Docker API
│   ├── container_state.py  # 容器状态引擎（后台异步刷新）
│   ├── docker_events.py    # Docker 事件监听（事件驱动替代轮询）
│   ├── cluster_manager.py  # 集群节点管理
│   ├── user_manager.py     # 用户管理
│   ├── alert_manager.py    # 告警管理
│   ├── bot_radar.py        # Bot 雷达（端点探测/端点库/注入）
│   ├── database.py         # SQLite 数据库
│   └── ...
├── routers/                # API 路由层
├── middleware/              # 中间件（认证/限速）
├── frontend/               # React 前端 SPA
├── docs/                   # 使用手册
└── resource/               # 静态资源（壁纸等）
```

## ⚙️ 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `CORS_ORIGINS` | 允许的 CORS 源（逗号分隔） | 空（开发模式允许 localhost） |
| `COOKIE_SECURE` | 是否启用安全 Cookie（HTTPS） | `false` |

## 📋 API 文档

启动服务后访问 `http://localhost:8000/docs` 查看 Swagger API 文档。

详细使用手册见 [`docs/manual.html`](docs/manual.html)。

## 📄 License

GPLv3

---

**NapCat QQ Manager**



## 状态与在线判定说明

- **登录真值只有一个来源**：状态引擎（`services/container_state.py`）的级联检测。
  文件系统里的 `onebot11_<QQ号>.json` 只能说明"这个实例配置过谁"，
  既不是当前登录账号，也不是"上次成功登录"，因此只写入 `configured_uin`。
- **在线 ≠ WS 连着**：`bot_online` 以 OneBot 心跳新鲜度为准。NapCat 进程活着、
  反向 WS 也连着，但 QQ 已退登在等扫码时，`bot_online` 为 false。
  对外接口 `/api/bots` 同时给出 `ws_connected` 与 `bot_online` 两个字段。
- **节点失联的数据会被标记**：远程节点断开后其容器仍保留在列表里（避免闪烁），
  但会带上 `stale: true`，且 `status` 降级为 `unknown`、`bot_online` 归 false，
  不会继续显示成鲜绿的 running。

## 集群密钥

每个面板启动时会生成一把集群密钥。**其它面板要把这台机器加为节点时，需要填这把密钥。**
在「集群设置 → 本机集群密钥」里查看与复制，也可以重置（重置后所有把本机加为节点的
面板都必须更新，否则会显示离线）。

添加节点时会先做握手探测，失败会明确告诉你是密钥不匹配、端口不通、DNS 解析失败
还是证书问题，而不是笼统的"离线"。删除节点是软删除：重新添加同一地址会复用原节点 ID，
用户对该节点实例的授权不会失效。

