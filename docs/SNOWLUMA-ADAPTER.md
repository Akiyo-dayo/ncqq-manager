# SnowLuma 适配器（ncqq-manager 节点代理）

> 落档：2026-10-14（来源：德云线上容器实测，非推测） · 并入 master：2026-10-15
> 状态：**已在德云 / 宁波 / 苏州生产验证**，并已**移植进本仓 master**。

## 1. 它解决什么

ncqq-manager 原本是 **NapCat 容器管理器**：一个容器 = 一个实例。
SnowLuma 是另一种 QQ 客户端封装，**一个共享容器里用 supervisor 跑多个 QQ 账号**。

本适配器让节点代理把这些账号**投影成「虚拟实例」**，从而：

- 面板里一个账号仍然是一条实例（**沿用原 NapCat 容器名**，见 §3.2）；
- 共享的 `snowluma` 容器**永不进入受管实例列表**；
- 二维码 / 登录态 / 日志 / 生命周期动作都走 SnowLuma 专属通道。

## 2. 改动清单

### 新增

| 文件 | 作用 |
|---|---|
| `services/snowluma.py` | 适配器本体：读 `snowluma_accounts.json` 映射；`is_virtual()` / `is_shared_container()` / `describe()` / `fetch_qr()` / `account_action()` / `oneclick_login()` / `program_states()` |
| `services/snowluma_logs.py` | 按账号过滤共享容器日志（避免显示同名旧 NapCat 容器的历史） |
| `scripts/apply_snowluma_gateway_fix.js` | 网关「会话抖动」修复应用器（幂等，见 `docs/SNOWLUMA-GATEWAY-JITTER-FIX.md`） |

### 修改

| 文件 | 改动 |
|---|---|
| `services/config.py` | `container_keywords`：`["napcat"]` → `["napcat", "snowluma"]` |
| `services/container_state.py` | ① 状态引擎 `local_containers.extend(describe())` 投影虚拟实例；② `running_local_names` 跳过虚拟实例 |
| `routers/ws_router.py` | 新增 `_fetch_container_logs()`：虚拟账号走账号过滤日志；其余走 `async_docker_manager.get_logs` |
| `routers/container_runtime_router.py` | ① `ContainerAction` 新增 `LOGOUT`；② `_get_local_qr_status` 虚拟账号走 `fetch_qr`；③ 清数据 / 重建 路由加 `is_shared_container \| is_virtual` 守卫；④ `action` 路由按账号粒度走 `account_action`（start/stop/restart/logout），共享容器本体一律 403；⑤ `/stats` 改用状态引擎投影值；⑥ 新增 `_resolve_container_logs()` 供两个日志端点使用；⑦ 新增 `POST /containers/{name}/oneclick-login`；⑧ `refresh-login` 虚拟账号走 `snowluma_probe` |

> `services/action_jobs.py` 的 `display_status` 默认值改动**master 已自带**，无需再打。

## 3. 关键设计

### 3.1 映射表 `config/snowluma_accounts.json`

```json
"<原 NapCat 容器名>": {"container": "snowluma", "program": "qq-<x>", "uin": "<uin>", "enabled": true}
```

- **键 = 原 NapCat 容器名** ⇒ 面板实例名不变。
- 绑定键是 `(node_id, container_name)` ⇒ **容器名不变 = 绑定零改动**。
- 每次调用重读该文件 → 改映射 **10–20 秒生效，无需重启节点代理**。

### 3.2 为什么要「同名投影」

`container_state.py` 先放真实容器、再 `extend(describe())`，按 name upsert ⇒ 虚拟条目**覆盖同名真实容器**，面板只显示一条。
这正是「复用原容器名」的设计意图，也是**保住 HQ 侧绑定**（`node_id` + `container_name` 二元组）的前提。

### 3.3 生命周期动作

- 共享容器本体 `snowluma`：一律 403（停它 = 全部账号掉线）。
- 虚拟账号：映射到它自己的 supervisor 程序，支持 `start/stop/restart/logout`，按账号粒度操作。

## 4. 与旧基线的血脉差异（重要）

德云/宁波线上跑的 `ncqq-manager-snowluma:20261011/20261014b` 是从**德云部署基线**
（`ncqq-manager_ncqq-manager:latest`，**仍含 BotShepherd**）打补丁得到的。

本仓 `master` 是**更新的血脉**（2026-07-30 重构：移除 BotShepherd、新增 `bot_radar*`、
`container_state` 引入容器内探测缓存 / 线程安全唤醒、`action_jobs` 引入 supersede 与
abandoned-job 上限、`ws_router` 快照改为 `(node_id, name)` 并剔除易变字段）。

**因此本次是把 SnowLuma 改动「人工移植」到 master**，而不是整份覆盖文件：

- 只插入 SnowLuma 专属块（§2 的 8 处）；
- **没有**把旧基线的 BotShepherd 相关代码（`_trigger_bs_inject`、`_schedule_bs_cleanup`、
  `init_bs_*` 配置）带回来。

## 5. 校验

本仓 checkout 若启用 `core.autocrlf=true`（Windows 默认），文件 md5 会因 CRLF 变化，
校验前先 `tr -d '\r'`。

| 文件 | md5（LF） |
|---|---|
| `services/snowluma.py` | 见 `git hash-object` / CI 校验 |
| `services/snowluma_logs.py` | 同上 |
| `routers/container_runtime_router.py` | 同上 |

线上运行态（`ncqq-manager-snowluma:20261014b`，宁波＝苏州逐字节一致）参考值：

| 文件 | md5 |
|---|---|
| `services/snowluma.py` | `2013458791397c653e4a0bf4a4bddc0f` |
| `routers/container_runtime_router.py` | `6255492db39ca48284c503aa83efbced` |
| `frontend/dist/index.html` | `81d84525c1e766ad7665e16c85bf2583` |

> 注意：master 移植版与线上 20261014b **不是逐字节相同**（master 还带 2026-07-30 之后的修复），
> 上面只是「确认 SnowLuma 部分行为一致」的参考。
