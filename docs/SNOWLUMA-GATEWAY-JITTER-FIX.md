# SnowLuma 网关「会话抖动」修复（2026-10-15）

> 现象：已登录账号的 OneBot 会话每约 8 秒被拆掉重建一次，ws 反复重连，
> BotShepherd 持旧 socket 发指令被拒（`rejected inbound action from stale socket`），
> 用户侧表现为「消息发不出去 / 响应异常」。
> 状态：**宁波生产已验证修复**（探针超时 102 → 0、单 tick 掉线 51 → ≤1、抖动归零）。

## 1. 这是哪个组件的问题

**不是 ncqq-manager，也不是 NapCat。** 是 **SnowLuma 网关**
（镜像 `motricseven7/snowluma:v1.14.22`，进程 `node /app/runtime/index.mjs`）。

ncqq-manager 只是面板；本仓的 `services/snowluma.py` 适配器负责把网关的账号投影成面板实例。
抖动发生在网关内部，所以修复打在**网关 bundle** 上。

## 2. 根因（运行时插桩取证，非推测）

在网关 bundle 里加临时插桩后抓到的决定性判据：

```
[DIAG] tickdrop prev=51 new=0 procs=51 known=51 rawLive=0 dropped=51 missEnum=0 missProbe=51 probeOk=51 probeTimeout=51 probeErr=104
[DIAG] loop-lag 707ms
[DIAG] loop-lag 806ms
```

逐字段：

| 字段 | 值 | 含义 |
|---|---|---|
| `prev` | 51 | 上一 tick 有 51 根活管道 |
| `new` | **0** | 本 tick 判定 **0 根活管道** |
| `procs`/`known` | 51/51 | **进程枚举正常**（`missEnum=0`）⇒ 排除枚举问题 |
| `rawLive` | **0** | **探针返回空集** |
| `missProbe` | **51** | 51 根全因「探针没返回」被丢 |
| `probeTimeout` | **102** | **102 次探针超时**（正是 250 ms 超时） |
| `loop-lag` | **707/806 ms** | **事件循环一次停顿 0.7–0.8 秒** |

### 机制

```js
var DEFAULT_PIPE_PROBE_TIMEOUT_MS = 250;   // ← 只有 250 ms

async function isConnectableUnixSocket(socketPath) {
    return new Promise((resolve) => {
        socket = net.createConnection(socketPath);                            // 真去 connect
        timer = setTimeout(() => finish(false), DEFAULT_PIPE_PROBE_TIMEOUT_MS); // ← 250ms 到点即判「死」
        socket.once("connect", () => finish(true));
    });
}

async function listLiveLinuxPipePids(runtimeDir, probe = isConnectableUnixSocket) {
    names = await promises.readdir(runtimeDir);
    await Promise.all(names.map(async (name) => {          // ← 103 根管道全并发探
        if (await probe(path.join(runtimeDir, name))) result.add(pid);
    }));
    return result;
}
```

`connect` 回调与 `setTimeout(250)` 在**同一个事件循环**上排队。
事件循环一旦停顿 700–800 ms，`connect` 回调等不到执行，`setTimeout` 先到期 ⇒ 探针报「死」。
**物理上管道好好的，只是回调没轮到。**

紧接着是**真正的逻辑缺陷**：

```js
const newLivePipes = new Set();
if (livePipes === null) {                       // 只有「返回 null」才走保护分支
    for (const pid of this.livePipes) if (newKnownPids.has(pid)) newLivePipes.add(pid);
} else for (const pid of livePipes) { ... }     // ← 空集也当权威 ⇒ newLivePipes 为空
for (const pid of this.livePipes) if (!newLivePipes.has(pid)) this.emit("pipe-down", pid);  // ← 51 个全 down
```

代码**已经**为 `livePipes === null`（枚举超时）做了保护，却**漏了「探针返回空集」**这一同样属于「本次采样失败」的情形。

### 为什么是「阵发」且会自愈

```
事件循环停顿 700–800ms
  → 250ms 探针整批超时 → livePipes=∅
  → 51 个 pipe-down → 51 个 OneBot 会话拆除
  → 下一 tick 恢复 → 51 个 pipe-up → 会话重建
      （HTTP/WS 监听重绑 ×51、friends/groups/member warmup ×51）
  → 重建本身再制造 700–800ms 停顿  ← 自我维持
```

这也解释了：为什么 51 个号**完全同步**抖、为什么**重启网关能立刻缓解**（重置事件循环）、
为什么**过一阵又复发**（两个缺陷都还在）。

停顿来源：网关单线程常驻 ~96% 单核；文件日志默认 `debug` 级，40 行/s 里 **64% 是
`[MsgPush.UnknownElement] wire element ignored`** 噪声；抖动期间还叠加 51 个会话重建。

## 3. 修复内容（4 处，全部可逆）

| # | 改动 | 理由 |
|---|---|---|
| **F1** | `DEFAULT_PIPE_PROBE_TIMEOUT_MS` **250 → 2000 ms** | 停顿实测 700–800 ms，留 2.5× 余量 ⇒ 停顿不再产生假阴性 |
| **F2** | `DEFAULT_INTERVAL_MS$1` **1500 → 3000 ms** | 探针频率减半，降低自身负载 |
| **F3** | 空探针结果不再当权威（`livePipes.size === 0 && this.livePipes.size > 0 && newKnownPids.size > 0` 时走保护分支） | 硬兜底：极端停顿也不会一次清空全部会话 |
| **F4/F5** | 保留 `[DIAG]` 埋点（`tickdrop` / `probe-empty-guard` / 探针计数 / `loop-lag`） | 常驻预警：复发会直接在日志留证 |

应用方式：`scripts/apply_snowluma_gateway_fix.js`（幂等，自动备份）。
参考 diff：`patches/snowluma-gateway-jitter-fix-20261015.patch`。

```bash
docker cp scripts/apply_snowluma_gateway_fix.js snowluma:/tmp/
docker exec snowluma node /tmp/apply_snowluma_gateway_fix.js
docker exec snowluma supervisorctl restart snowluma   # 只重启网关，QQ 客户端不动
docker exec snowluma md5sum /app/runtime/index.mjs    # 期望 567aa9d453ec13897e0fe7bc2da5a680
```

## 4. 校验值

| 文件 | md5 |
|---|---|
| 厂商原版 `index.mjs`（v1.14.22） | `6d699508706792d353887bb0aebdc3ab` |
| 修复后 `index.mjs` | `567aa9d453ec13897e0fe7bc2da5a680` |

## 5. 实测验证（宁波，2026-10-15 00:21–00:30）

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 探针超时（`probeTimeout`） | 102 → 204 → 212 | **0** |
| 单 tick 掉线管道（`dropped`） | **51** | **≤1** |
| 会话抖动（`session started`/分钟） | 105–297 | **0–2** |
| `pipe connected` | 155 次 / 30s | **0** |
| `rejected inbound action from stale socket` | 79 次 / 30s | 随抖动消失 |
| 事件循环停顿 | 707 / 806 ms | 仍存在（最高 664 ms）——但不再产生假阴性 |
| 真实消息流量 | — | **167 条 `[Event]` / 60s**（业务正常） |

## 6. ⚠️ 持久化：bundle 在镜像层，容器重建会丢

`/app/runtime/index.mjs` 属**镜像层**（非 bind mount），容器一重建补丁就没了。
因此生产上把修复固化进**看门狗 + FIX GUARD**，脚本已纳入本仓：

| 文件 | 作用 |
|---|---|
| `scripts/snowluma-gw-watchdog.sh` | 看门狗本体（Job 0 武装检查 / Job 1 FIX GUARD / Job 2 抖动看门狗） |
| `scripts/install_snowluma_gw_watchdog.sh` | 幂等安装器：装脚本 + **武装还原源** + 装 `*/5` root cron + 自检 |

```bash
scp scripts/snowluma-gw-watchdog.sh scripts/install_snowluma_gw_watchdog.sh root@<vm>:/tmp/
ssh root@<vm> 'bash /tmp/install_snowluma_gw_watchdog.sh'
```

FIX GUARD 逻辑：

```
若 /app/runtime/index.mjs 的 md5 == 厂商原版(6d699508…)
   → 从 <host /app/data>/_patches/index.mjs.fixed 还原修复版
   → node --check 校验
   → 重启「仅网关程序」（QQ 客户端不动）
   → 失败则回退厂商原版并告警
```

### 武装条件（务必记住）

看门狗**只是执行者**，还原源必须存在，否则 FIX GUARD 形同虚设：

| 节点 | 看门狗 | cron | `_patches/index.mjs.fixed` | 状态 |
|---|---|---|---|---|
| 宁波 `nb-snowluma` | ✅ | ✅ `*/5` | ✅ | 已武装，2026-10-14 16:30 实测还原成功 |
| 苏州 `suzhou-bot-vm` | ✅ | ✅ `*/5` | ✅（2026-10-15 14:09 补齐） | 已武装，14:10:01 实测还原成功（4 秒，37 个 QQ 客户端 PID 不变） |

> 2026-10-15 14:09 之前，苏州**只有看门狗、没有还原源**，FIX GUARD 实际处于失能状态：
> root cron 每 5 分钟照跑，但 `_patches/index.mjs.fixed` 不存在，容器一旦重建补丁就永久丢失。

### 2026-10-15 加固

原脚本把日志重定向直接挂在还原命令上：

```bash
docker exec "$CONTAINER" cp .../index.mjs.fixed /app/runtime/index.mjs 2>>"$LOG"
```

`$LOG` 不可写时 bash 打不开重定向 ⇒ **该命令根本不执行** ⇒ 还原被静默跳过。
现改为经 `log()` 包裹（写不进文件就退到 syslog），**任何日志问题都不能再阻断纠正动作**；
并新增 Job 0 武装检查（还原源缺失时明确告警，而不是安静地什么都不做）。

> 已复现：以非 root 身份手跑旧脚本，还原被跳过；同一时刻 root cron 仍成功还原。

（更彻底的方案是构建派生镜像 / 加 bind mount，见 §8。）

## 7. 回滚

```bash
# 回到厂商原版（抖动会复发，仅应急）
docker exec snowluma sh -c 'cp /app/runtime/index.mjs.bak-* /app/runtime/index.mjs'   # 用脚本留下的备份
docker exec snowluma supervisorctl restart snowluma
```

## 8. 遗留与建议

| # | 项 | 说明 |
|---|---|---|
| 1 | **上报上游** | 上游真实缺陷：①探针 250 ms 超时对事件循环停顿零容忍；②空探针结果被当权威。建议按 §2 证据提 issue（镜像 `v1.14.22`） |
| 2 | **降日志洪水** | 文件日志默认级别是 `debug`（`resolveFileMinLevel()` 默认 `"debug"`，在 `logger-Bm8oJxzs.js`），64% 是 `[MsgPush.UnknownElement]` 噪声。可用 env `SNOWLUMA_LOG_FILE_LEVEL=info` 关掉 —— 但 `/etc/supervisord.conf` 是**只读挂载**，需改 compose env（要重建容器）或补丁 logger 文件 |
| 3 | `SNOWLUMA_HOOK_AUTOLOAD` 矛盾 | env `=1` 覆盖 `runtime.json: hookAutoLoad=false`（env 优先），尚未处理 |
| 4 | `autostart=false` 单点 | 宁波 50 + 苏州 35 个 QQ program 全 false，容器/VM 重启后不自启 |
| 5 | 账号密度 | 51 号单容器、单线程 ~96% 单核。本次修复已把触发门槛大幅抬高，非必须；后续再扩号可考虑拆 2–3 容器 |

## 9. 触发源实测：内联 base64 大媒体（2026-10-15 补充）

§2 的机制需要一个「点火器」：把事件循环推到 700–800 ms 停顿的那一次负载。实测如下。

宁波网关日志全量（2026-10-14 ~ 10-15）：

| 项 | 值 |
|---|---|
| `[Highway.Stage] staged inline base64 source` | **335 次 / 2.96 GiB** |
| 体积分布 | <1MB 43、1–5MB 131、5–20MB 110、20–40MB 45、**≥40MB 6** |
| 最早出现 | **2026-10-10 16:02:52**（早于本次全部改动） |
| 最大单条 | 73.8 MB（base64 原文约 98 MB） |

**发送方**：昆明「视频解析」服务（`150.242.247.238:8080`，wsClient 名 `视频解析`；
`parser_use_base64=True`、`parser_max_size=60`、`API_TIMEOUT=60`）。
网关只对「客户端连上来」记名，不对入站 action 记名，因此按体积与内容反推：
40–70 MB 的载荷只可能是视频，而全部 wsClient 中只有「视频解析」具备取视频并内联回传的能力；
逐条时间对撞（±4 s）也把昆明侧失败与网关侧失败一一对上。

**点火时刻**：`2026-10-14 21:17:17` 与 `21:17:19` 各落盘一条 **45,910,972 B（43.8 MiB）**，
7 秒后 `21:17:24` 51 个会话同时被拆——正是 §2 描述的整批假阴性。

**是否发失败？**

| 失败形态 | 位置 | 次数 | 说明 |
|---|---|---|---|
| `WebSocket call api send_msg timeout` | 昆明侧 | **21** | 1.33 倍体积的 base64 传输 + 网关同步落盘 + 上传，超过 `API_TIMEOUT=60s` 即放弃 |
| `highway upload transport failed after 3 attempts (TCP connect timeout)` | 网关侧 | 164 | **148 次 <1MB、16 次 1–5MB、仅 2 次 ≥5MB**（最大 15.1 MB） |
| `result=120`（拒发） | 昆明侧 | 13 | |

结论：**大媒体本身很少在 highway 上失败**（164 次里只有 2 次 ≥5MB）；
它的失败形态是**昆明侧 60 秒 WS 调用超时**。网关侧的 highway 失败集中在**小媒体**，
且按账号聚集（宁波 `3537524514` 14 次、苏州 `864439782` 9 次…），与体积无关。

因此 P0 实际有两条，互相独立：

1. **让下游不要再用内联 base64 发大媒体**——改传 `http(s)://` URL 让网关流式落盘；
2. 昆明侧「60 秒同步等待大媒体发送结果」需要重新设计，超时必然发生。
