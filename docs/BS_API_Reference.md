# BS 中间件 API 参考手册

> 基于 `BotShepherd/app/web_api/web_server.py` 源码整理，共 **36 个 API 端点**。
>
> **基础 URL**: `http://localhost:{web_port}`（默认 `5100`，可在 `global_config.json` 的 `web_port` 修改）
>
> **认证方式**: Flask Session — 需先 `POST /login` 获取 cookie，后续请求自动携带

---

## 📑 API 总览

| 分类 | 端点 | 方法 | 用途 | NCQQ可集成 |
|------|------|------|------|:---:|
| **认证** | `/login` | POST | 登录获取 Session | ✅ 已对接 |
| | `/logout` | GET | 登出 | - |
| **系统信息** | `/api/version` | GET | 获取 BS 版本号 | ⭐ |
| | `/api/github-version` | GET | 获取 GitHub 最新版本 | ⭐ |
| | `/api/status` | GET | 运行状态+活跃连接数 | ⭐ |
| | `/api/system-resources` | GET | CPU/内存/磁盘/运行时长 | ⭐ |
| | `/api/database-status` | GET | 数据库大小/消息数/保留天数 | ⭐ |
| | `/api/dashboard-content` | GET | 仪表盘 Markdown 内容 | - |
| **连接管理** | `/api/connections` | GET | 所有连接配置+运行时状态 | ✅ 已对接 |
| | `/api/connections/{id}` | PUT | 更新连接配置 | ⭐ |
| | `/api/connections/{id}/copy` | POST | 复制连接配置 | ⭐ |
| | `/api/connections/{id}` | DELETE | 删除连接配置 | ⭐ |
| **账号管理** | `/api/accounts` | GET | 所有账号配置(别名/时间) | ⭐⭐ |
| | `/api/accounts/{id}` | PUT | 更新账号配置 | ⭐ |
| | `/api/accounts/{id}` | DELETE | 删除账号配置 | - |
| | `/api/accounts/{id}/online-status` | GET | 检查账号在线状态 | ⭐⭐ |
| | `/api/recently-active-accounts` | GET | 24h 活跃账号列表 | ⭐⭐ |
| **群组管理** | `/api/groups` | GET | 所有群组配置 | ⭐ |
| | `/api/groups/{id}` | PUT | 更新群组配置 | ⭐ |
| | `/api/groups/{id}` | DELETE | 删除群组配置 | - |
| | `/api/recently-active-groups` | GET | 24h 活跃群组列表 | ⭐ |
| **全局配置** | `/api/global-config` | GET | 全局配置(超级用户/前缀/黑名单等) | ⭐ |
| | `/api/global-config` | PUT | 更新全局配置 | ⭐ |
| | `/api/config/flush` | POST | 强制内存脏数据写盘 | - |
| **黑名单** | `/api/blacklist` | GET | 获取用户/群组黑名单 | ⭐ |
| | `/api/blacklist` | POST | 添加黑名单 | ⭐ |
| | `/api/blacklist` | DELETE | 移除黑名单 | ⭐ |
| **统计分析** | `/api/statistics` | GET | 消息统计(趋势/Top群) | ⭐⭐ |
| | `/api/statistics/database` | GET | 数据库维度统计 | ⭐ |
| **消息查询** | `/api/query_messages` | GET | 消息记录查询(支持私聊) | ⭐ |
| **日志** | `/api/logs` | GET | 日志文件列表 | ⭐ |
| | `/api/logs/{filename}` | GET | 日志文件内容 | ⭐ |
| **系统控制** | `/api/update` | POST | 执行 git pull 更新 | - |
| | `/api/system/restart` | POST | 重启 BS 系统 | ⭐ |
| **备份管理** | `/api/backups` | GET | 备份文件列表 | ⭐ |
| | `/api/backups` | POST | 创建新备份 | ⭐ |
| | `/api/backups/{filename}` | GET | 下载备份文件 | - |
| | `/api/backups/{filename}` | DELETE | 删除备份文件 | ⭐ |

> **图例**: ✅ = 已在 NCQQ 中对接 | ⭐⭐ = 高价值可集成 | ⭐ = 可集成 | `-` = 低优先级

---

## 🔐 1. 认证

### POST `/login`
登录获取 Session Cookie。
```
Content-Type: application/x-www-form-urlencoded
Body: username=admin&password=admin
```
成功后设置 `session` cookie，后续请求自动携带。

### GET `/logout`
清除 Session，重定向到登录页。

---

## 📊 2. 系统信息

### GET `/api/version`
```json
{ "version": "1.0.2", "author": "Loping151", "description": "..." }
```

### GET `/api/github-version`
从 GitHub raw 拉取最新版本号，用于检测更新。响应格式同上。

### GET `/api/status`
```json
{ "status": "running", "active_connections": 4, "timestamp": 1736000000.0 }
```

### GET `/api/system-resources`
```json
{
  "app_cpu": 2.1,        "total_cpu": 15.8,
  "app_memory": 85.3,    "total_memory_gb": 16.0,
  "used_memory_gb": 8.5, "available_memory_gb": 7.5,
  "memory_percent": 53,  "disk_total_gb": 500, "disk_used_gb": 250,
  "disk_free_gb": 250,   "disk_percent": 50,
  "cpu_cores": 8,        "system_info": "Windows 10",
  "python_version": "3.10.11",
  "system_uptime_days": 5, "system_uptime_hours": 12,
  "app_uptime_hours": 2,   "app_uptime_minutes": 30
}
```

### GET `/api/database-status`
```json
{
  "db_size_mb": 125.8, "message_count": 50000,
  "retention_days": 3,  "storage_path": "./data"
}
```

### GET `/api/dashboard-content`
返回 `templates/dashboard.md` 的 Markdown 内容。
```json
{ "content": "# Welcome\n..." }
```

---

## 🔗 3. 连接管理

### GET `/api/connections`
获取所有连接配置 + 运行时状态。
```json
{
  "connection_1": {
    "name": "NapCat→Yunzai",
    "description": "主力连接",
    "enabled": true,
    "client_endpoint": "ws://0.0.0.0:6100/OneBotv11",
    "target_endpoints": ["ws://192.168.1.100:2536/OneBotv11"],
    "group": "prod",
    "status": {
      "enabled": true,
      "client_status": "connected",
      "client_endpoint": "ws://0.0.0.0:6100/OneBotv11",
      "target_statuses": {},
      "error": null,
      "client_address": "127.0.0.1:52341",
      "self_id": 1234567890
    }
  }
}
```



**`client_status` 状态值**:
| 值 | 含义 |
|---|---|
| `disabled` | 连接已禁用 |
| `starting` | 正在启动 WS 服务器 |
| `listening` | WS 服务器监听中，等待客户端连接 |
| `connected` | 客户端已连接 |
| `error` | 启动或连接错误 |

### PUT `/api/connections/{connection_id}`
更新指定连接配置。变更 `enabled` 或 `target_endpoints` 会触发热重载。
```json
{
  "name": "新名称", "description": "描述",
  "enabled": true,
  "client_endpoint": "ws://0.0.0.0:6100/OneBotv11",
  "target_endpoints": ["ws://192.168.1.100:2536/OneBotv11"]
}
```
→ `{ "success": true }`

### POST `/api/connections/{connection_id}/copy`
复制连接配置到新 ID。
```json
{ "new_id": "connection_2", "new_name": "副本连接" }
```
→ `{ "success": true, "message": "连接配置已复制" }`

### DELETE `/api/connections/{connection_id}`
删除连接并停止相关 WS 任务。
→ `{ "success": true }`

---

## 👤 4. 账号管理

### GET `/api/accounts`
```json
{
  "123456": {
    "name": "主号", "description": "...", "enabled": true,
    "aliases": { "#": ["#", "yun"], "yz": ["yunzai"] },
    "last_receive_time": "2025-01-10T12:00:00",
    "last_send_time": "2025-01-10T12:05:00"
  }
}
```

### PUT `/api/accounts/{account_id}`
更新账号配置（名称/描述/启用/别名）。更新后立即写盘。
```json
{ "name": "新名称", "aliases": { "#": ["#", "yun"] } }
```

### DELETE `/api/accounts/{account_id}`
删除账号配置文件。

### GET `/api/accounts/{account_id}/online-status`
通过 WS 代理发送 OneBot `get_status` API 检测账号在线状态。超时 5s。
```json
{ "online": true }
```

### GET `/api/recently-active-accounts`
返回 24h 内有消息活动的账号列表。
```json
[{ "self_id": "123456", "nickname": "Bot1", "last_activity": 1736000000 }]
```

---

## 👥 5. 群组管理

### GET `/api/groups`
获取所有群组配置（名称/启用/过滤词/到期时间等）。

### PUT `/api/groups/{group_id}`
更新群组配置。更新后立即写盘。

### DELETE `/api/groups/{group_id}`
删除群组配置。

### GET `/api/recently-active-groups`
返回 24h 内有消息活动的群组列表。

---

## ⚙️ 6. 全局配置

### GET `/api/global-config`
```json
{
  "superusers": ["644572093"],
  "command_prefix": "bs",
  "web_auth": { "username": "admin", "password": "admin" },
  "web_port": 5111,
  "blacklist": { "users": [], "groups": [] },
  "database": { "data_path": "./data", "auto_expire_days": 3 },
  "logging": { "level": "INFO", "keep_days": 3 },
  "backup": { "enabled": true, "keep_days": 7 }
}
```

### PUT `/api/global-config`
部分更新全局配置。传入要修改的字段即可。
```json
{ "database": { "auto_expire_days": 7 } }
```

### POST `/api/config/flush`
立即将内存中的账号/群组脏数据写入磁盘。
→ `{ "success": true }`

---

## 🚫 7. 黑名单

### GET `/api/blacklist`
```json
{ "users": ["user1"], "groups": ["group1"] }
```

### POST `/api/blacklist`
添加黑名单条目。`type` = `"users"` | `"groups"`。
```json
{ "type": "users", "id": "123456" }
```

### DELETE `/api/blacklist`
移除黑名单条目。请求体格式同上。

---

## 📈 8. 统计分析

### GET `/api/statistics`
| 参数 | 说明 | 可选值 |
|------|------|--------|
| `range` | 时间范围 | `today` / `yesterday` / `week` / `month` / `custom` |
| `start_date` | 自定义开始 | ISO 格式 |
| `end_date` | 自定义结束 | ISO 格式 |
| `self_id` | 按账号过滤 | 账号 ID |
| `direction` | 消息方向 | `SEND` / `RECV` |

```json
{
  "total_messages": 1000, "active_users": 50, "active_groups": 10,
  "received_messages": 800,
  "messages_change": 100, "users_change": 5, "groups_change": 1,
  "hourly_trend": [{ "time": "2025-01-10 00:00", "count": 10 }],
  "top_groups": [{ "group_id": "789012", "message_count": 200, "active_users": 1 }]
}
```

### GET `/api/statistics/database`
按群/账号/用户维度统计消息数。参数: `self_id`, `start_time`, `end_time`。

---

## 🔍 9. 消息查询

### GET `/api/query_messages`
| 参数 | 说明 |
|------|------|
| `self_id` | 账号 ID |
| `user_id` | 发送者 ID |
| `group_id` | 群号（`__private__` = 仅私聊） |
| `start_time` / `end_time` | Unix 时间戳 |
| `keywords` | 关键词（可多个） |
| `keyword_type` | `and` / `or` |
| `prefix` | 前缀过滤 |
| `direction` | `SEND` / `RECV` |
| `limit` / `offset` | 分页 (默认 20/0) |

```json
{
  "messages": [{
    "id": 1, "message_id": "msg123", "self_id": "123456",
    "user_id": "user1", "group_id": "group1",
    "raw_message": "原始消息", "message_content": "处理后",
    "timestamp": 1736000000, "direction": "RECV",
    "connection_id": "conn1"
  }],
  "total_count": 1000, "offset": 0, "limit": 20
}
```

---

## 📋 10. 日志管理

### GET `/api/logs`
```json
{
  "files": [{
    "name": "main/app.log", "display_name": "app.log",
    "size": 1024000, "modified": 1736000000.0,
    "is_rotated": false, "log_type": "main"
  }]
}
```

### GET `/api/logs/{filename}`
参数: `lines`（默认 1000）。
```json
{ "content": "日志内容...", "total_lines": 1000, "file_size": 1024000 }
```

---

## 💾 11. 备份管理

### GET `/api/backups`
```json
{ "backups": [{ "filename": "config_backup_20260315.zip", "size": 51200, "created": 1736000000 }] }
```

### POST `/api/backups`
创建新备份（加密 ZIP，密码为 `web_auth.password`）。
```json
{ "success": true, "filename": "config_backup_xxx.zip", "size": 51200 }
```

### GET `/api/backups/{filename}`
直接下载备份文件（返回二进制流）。

### DELETE `/api/backups/{filename}`
删除指定备份文件。→ `{ "success": true }`

---

## 🔧 12. 系统控制

### POST `/api/update`
执行 `git pull` 更新 BS 源码。
```json
{ "success": true, "message": "更新成功，请重启系统以应用更新", "output": "Already up to date." }
```

### POST `/api/system/restart`
延迟 2 秒重启 BS 进程。
```json
{ "success": true, "message": "系统将在2秒后重启" }
```

---

## ⚠️ 错误响应格式

所有 API 错误统一返回：
```json
{ "error": "错误描述信息" }
```

| HTTP 状态码 | 含义 |
|-------------|------|
| `200` | 成功 |
| `400` | 参数错误 |
| `401` | 未认证（需先登录） |
| `404` | 资源不存在 |
| `500` | 服务器内部错误 |

---

## 📝 配置读写规则

| 操作 | 行为 |
|------|------|
| `GET /api/accounts` | 先写盘脏数据 → 从文件重载 → 覆盖内存 |
| `GET /api/groups` | 先写盘脏数据 → 从文件重载 → 覆盖内存 |
| `GET /api/connections` | 从文件重载连接配置 → 合并运行时状态 |
| `PUT /api/accounts/{id}` | 更新内存 → 立即写盘 |
| `PUT /api/groups/{id}` | 更新内存 → 立即写盘 |
| `PUT /api/connections/{id}` | 更新内存 → 立即写盘 → 按需热重载 |
| `PUT /api/global-config` | 更新内存 → 立即写盘 |
| `POST /api/config/flush` | 仅写盘，不重载 |

---

## 🔌 NCQQ 对接说明

NCQQ 通过 `services/botshepherd.py` 的 `_fetch_connections_via_api()` 方法对接 BS API：

1. **认证流程**: 读取 `BotShepherd/config/global_config.json` 中的 `web_auth` → POST `/login` 获取 Session cookie
2. **数据获取**: 带 cookie GET `/api/connections` → 获取含运行时状态的连接数据
3. **降级方案**: BS 未运行时直接读取 `BotShepherd/config/connections/*.json` 文件

对接新 API 只需在 `services/botshepherd.py` 添加类似代理方法，复用 `_read_bs_auth()` 和 Session 认证机制。