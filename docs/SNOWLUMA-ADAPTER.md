# SnowLuma 适配器（ncqq-manager 节点代理）

> 落档时间：2026-10-14 · 来源：德云线上容器实测（非推测）
> 状态：**已在德云 / 宁波生产验证**，但**尚未并入本仓 master**（见「移植须知」）

## 1. 它解决什么

ncqq-manager 原本是 **NapCat 容器管理器**：一个容器 = 一个实例。
SnowLuma 是另一种 QQ 客户端封装，**一个共享容器里用 supervisor 跑多个 QQ 账号**。

本适配器让节点代理把这些账号**投影成「虚拟实例」**，从而：

- 面板里一个账号仍然是一条实例（**沿用原 NapCat 容器名**，见 §4）；
- 共享的 `snowluma` 容器**永不进入受管实例列表**；
- 二维码 / 登录态 / 日志 / 生命周期动作都走 SnowLuma 专属通道。

## 2. 改动清单（7 个文件）

### 新增（本仓已直接落盘）

| 文件 | 作用 |
|---|---|
| `services/snowluma.py` | 适配器本体：读 `snowluma_accounts.json` 映射，`is_virtual()` / `describe()` / `fetch_qr()` |
| `services/snowluma_logs.py` | 按账号过滤共享容器日志（避免显示旧 NapCat 容器的历史） |

### 修改（见 `patches/snowluma-adapter-vs-deyun-base-20261014.patch`）

| 文件 | 改动 |
|---|---|
| `services/config.py` | `container_keywords`：`["napcat"]` → `["napcat", "snowluma"]` |
| `services/container_state.py` | ① 状态引擎 `local_containers.extend(describe())` 投影虚拟实例；② `running_local_names` 跳过虚拟实例 |
| `services/action_jobs.py` | 无进行中任务时补 `display_status` 默认值（否则认证版列表接口缺字段） |
| `routers/ws_router.py` | 新增 `_fetch_container_logs()`：`cluster_manager.get_logs` 不存在（只有 `get_logs_async`），原调用恒抛异常被吞 → `/ws/logs` 永远推空；顺带把虚拟账号路由到账号过滤日志 |
| `routers/container_runtime_router.py` | 5 处 `is_virtual()` 守卫：QR 状态、生命周期动作（虚拟实例返回 403）、`/stats`（改用状态引擎投影值，避免报死掉的旧 NapCat 容器）、容器日志、登录态探测（`snowluma_probe`） |

## 3. 关键设计

### 3.1 映射表 `config/snowluma_accounts.json`

```json
"<原 NapCat 容器名>": {"container": "snowluma", "program": "qq-<x>", "uin": "<uin>", "enabled": true}
```

- **键 = 原 NapCat 容器名** ⇒ 面板实例名不变。
- 绑定键是 `(node_id, container_name)`（`services/user_manager.py: has_instance`）⇒ **容器名不变 = 绑定零改动**。
- 每次调用重读该文件 → 改映射 **10–20 秒生效，无需重启节点代理**。

### 3.2 为什么要「同名投影」

`container_state.py` 先放真实容器、再 `extend(describe())`，按 name upsert ⇒ 虚拟条目**覆盖同名真实容器**，面板只显示一条。
这正是「复用原容器名」的设计意图，也是**保住 HQ 侧绑定**（`node_id` + `container_name` 二元组）的前提。

### 3.3 生命周期动作对虚拟实例一律 403

面板「启动/停止」对 SnowLuma 虚拟实例永远 403（设计如此，**不是面板坏了**）。
替代：在节点上 `docker exec snowluma supervisorctl {start,stop,restart} qq-<x>`。

## 4. ⚠️ 移植须知（重要）

本补丁是对 **德云部署基线** 的隔离 diff：

| | 值 |
|---|---|
| base 镜像 | `ncqq-manager_ncqq-manager:latest`（rollback 容器 `ncqq-manager-v2-rollback-20261011`） |
| patched | `ncqq-manager-snowluma:20261011`（运行容器 `ncqq-manager-v2`） |
| 采集方式 | `docker cp` 两个容器的 `/app` 后逐文件 `md5sum` + `diff -u` |

**本仓 `master` 与这个基线不是同一血脉**（master 停在 2026-07-30 的一次重构，已移除 BotShepherd，并新增 `bot_radar*`）。
因此**不要**把补丁里的文件整份覆盖到 master —— 会回退 master 的修复。

正确做法：在检出 base 的血脉上 `git apply`，或把 §2 的 5 处改动**人工移植**到 master 对应位置（每处都很小：1 行 / 3 行 / 13 行 / 22 行 / 60 行）。

## 5. 校验

线上运行态 md5（`ncqq-manager-snowluma:20261014`，与德云一致）：

| 文件 | md5 |
|---|---|
| `services/snowluma.py` | `9e1dad072fdd66719321077a69393db2` |
| `services/snowluma_logs.py` | `2e4b243d7c244c03cbc70e164752f3f7` |
| `services/action_jobs.py` | `696b9c6088f2864c30908e123b93d395` |
| `services/config.py` | `d393056e5cfa640309eb49473805c703` |
| `services/container_state.py` | `0017b9c895fa5e3fcbb637c65a6edb7c` |
| `routers/ws_router.py` | `8b0b1e1c6e4275ee9b53c7c8f96c7d21` |
| `routers/container_runtime_router.py` | `5178be637ee3335d5122d95f6d53c198` |

> 注意：本仓 checkout 若启用 `core.autocrlf=true`（Windows 默认），文件 md5 会因 CRLF 变化，校验前先 `tr -d '\r'`。
