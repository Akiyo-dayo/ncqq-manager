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
- 🐑 **BotShepherd** — 内置 OneBot v11 WS 代理，多框架连接管理、消息统计、跨框架黑名单
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
git clone https://github.com/your-repo/ncqq-manager.git
cd ncqq-manager
docker compose up -d
```

打开浏览器访问 `http://localhost:8000`，按引导完成初始化设置。

### 方式二：手动部署

**环境要求**：Python 3.10+、Node.js 16+、Docker

```bash
git clone https://github.com/your-repo/ncqq-manager.git
cd ncqq-manager

# 一键启动（自动安装依赖 + 构建前端 + 初始化 BotShepherd + 启动服务）
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
git clone https://github.com/your-repo/ncqq-manager.git
cd ncqq-manager

# 首次启动前建议先手动执行一次，完成依赖安装/前端构建/初始化
uv run python start.py

# 注册 systemd 开机自启动（仅注册主项目，BotShepherd 由主项目自动唤起）
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
│   ├── botshepherd.py      # BotShepherd 集成
│   ├── database.py         # SQLite 数据库
│   └── ...
├── routers/                # API 路由层
├── middleware/              # 中间件（认证/限速）
├── frontend/               # React 前端 SPA
├── botshepherd/            # BotShepherd 子项目（WS 代理）
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

