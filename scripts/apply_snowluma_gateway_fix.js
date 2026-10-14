#!/usr/bin/env node
/**
 * apply_snowluma_gateway_fix.js — SnowLuma 网关「会话抖动」修复应用器
 *
 * 背景（根因见 docs/SNOWLUMA-GATEWAY-JITTER-FIX.md）：
 *   网关 Node 事件循环出现 700–800 ms 停顿，而管道存活探针超时只有 250 ms，
 *   停顿期间整批探针误判「管道已死」→ livePipes 返回空集 → 代码把空集当权威
 *   → 所有已登录账号的 OneBot 会话被同时拆建 → 重建又制造停顿（正反馈环）。
 *
 * 本脚本对 motricseven7/snowluma 镜像内的 /app/runtime/index.mjs 做 4 处修改：
 *   F1 DEFAULT_PIPE_PROBE_TIMEOUT_MS  250  -> 2000
 *   F2 DEFAULT_INTERVAL_MS$1         1500  -> 3000
 *   F3 空探针结果不再当权威（硬兜底）
 *   F4 保留 [DIAG] 埋点（tickdrop / probe-empty-guard / loop-probe 计数 / loop-lag）
 *
 * 幂等：已应用则直接退出 0。写入前自动备份到 <file>.bak-<ts>。
 * 用法（在 snowluma 容器内）：
 *     docker cp apply_snowluma_gateway_fix.js snowluma:/tmp/
 *     docker exec snowluma node /tmp/apply_snowluma_gateway_fix.js
 *     docker exec snowluma supervisorctl restart snowluma   # 只重启网关，QQ 客户端不动
 *
 * 校验（应用后 md5 应为 567aa9d453ec13897e0fe7bc2da5a680，对应 v1.14.22 原版 6d699508706792d353887bb0aebdc3ab）：
 *     docker exec snowluma md5sum /app/runtime/index.mjs
 */
'use strict';
const fs = require('fs');

const FILE = process.env.SNOWLUMA_BUNDLE || '/app/runtime/index.mjs';
const DRY = process.argv.includes('--dry-run');

const RULES = [
  {
    id: 'F1',
    what: 'probe timeout 250ms -> 2000ms',
    from: 'var DEFAULT_PIPE_PROBE_TIMEOUT_MS = 250;',
    to: 'var DEFAULT_PIPE_PROBE_TIMEOUT_MS = 2000;',
    done: 'var DEFAULT_PIPE_PROBE_TIMEOUT_MS = 2000;',
  },
  {
    id: 'F2',
    what: 'poll interval 1500ms -> 3000ms',
    from: 'var DEFAULT_INTERVAL_MS$1 = 1500;',
    to: 'var DEFAULT_INTERVAL_MS$1 = 3000;',
    done: 'var DEFAULT_INTERVAL_MS$1 = 3000;',
  },
  {
    id: 'F3',
    what: 'never treat an all-empty probe result as authoritative',
    from: 'if (livePipes === null) {',
    to: 'if (livePipes === null || (livePipes.size === 0 && this.livePipes.size > 0 && newKnownPids.size > 0)) { if (livePipes !== null) this.log.warn("[DIAG] probe-empty-guard engaged prev=%d procs=%d probeTimeout=%d", this.livePipes.size, newKnownPids.size, (globalThis.__ps || {}).to || 0);',
    done: 'probe-empty-guard',
  },
  {
    id: 'F4a',
    what: 'probe outcome counters (loop probe telemetry)',
    from: 'this.listProcesses = deps.listProcesses;',
    to: 'this.listProcesses = deps.listProcesses;\n\t\tif (!globalThis.__ps) { globalThis.__ps = { ok: 0, to: 0, err: 0 }; let __l = Date.now(); const __t = setInterval(() => { const __n = Date.now(); const __g = __n - __l - 1000; __l = __n; if (__g > 120) console.warn("[DIAG] loop-lag " + __g + "ms"); }, 1000); if (__t.unref) __t.unref(); }',
    done: 'globalThis.__ps = { ok: 0, to: 0, err: 0 }',
  },
  {
    id: 'F4b',
    what: 'count probe timeouts',
    from: 'timer = setTimeout(() => finish(false), DEFAULT_PIPE_PROBE_TIMEOUT_MS);',
    to: 'timer = setTimeout(() => { if (globalThis.__ps) globalThis.__ps.to++; finish(false); }, DEFAULT_PIPE_PROBE_TIMEOUT_MS);',
    done: 'globalThis.__ps.to++',
  },
  {
    id: 'F4c',
    what: 'count probe successes',
    from: 'socket.once("connect", () => finish(true));',
    to: 'socket.once("connect", () => { if (globalThis.__ps) globalThis.__ps.ok++; finish(true); });',
    done: 'globalThis.__ps.ok++',
  },
  {
    id: 'F4d',
    what: 'count probe refusals',
    from: 'if (isUnavailableSocketError(error)) finish(false);\n\t\t\telse {',
    to: 'if (isUnavailableSocketError(error)) { if (globalThis.__ps) globalThis.__ps.err++; finish(false); }\n\t\t\telse {',
    done: 'globalThis.__ps.err++',
  },
  {
    id: 'F5',
    what: 'tick drop tripwire',
    from: 'for (const pid of this.livePipes) if (!newLivePipes.has(pid)) this.emit("pipe-down", pid);',
    to: '{ const __d = []; for (const pid of this.livePipes) if (!newLivePipes.has(pid)) __d.push(pid); if (__d.length) { const __me = __d.filter((p) => !newKnownPids.has(p)).length; const __mp = (livePipes === null ? -1 : __d.filter((p) => !livePipes.has(p)).length); const __s = globalThis.__ps || { ok: 0, to: 0, err: 0 }; this.log.warn("[DIAG] tickdrop prev=%d new=%d procs=%d known=%d rawLive=%d dropped=%d missEnum=%d missProbe=%d probeOk=%d probeTimeout=%d probeErr=%d", this.livePipes.size, newLivePipes.size, processes.length, newKnownPids.size, (livePipes === null ? -1 : livePipes.size), __d.length, __me, __mp, __s.ok, __s.to, __s.err); } }\n\t\t\tfor (const pid of this.livePipes) if (!newLivePipes.has(pid)) this.emit("pipe-down", pid);',
    done: 'tickdrop prev=%d',
  },
];

function main() {
  if (!fs.existsSync(FILE)) {
    console.error(`[fix] bundle not found: ${FILE}`);
    process.exit(2);
  }
  let src = fs.readFileSync(FILE, 'utf8');

  const already = RULES.filter((r) => src.includes(r.done)).length;
  if (already === RULES.length) {
    console.log('[fix] already applied — nothing to do');
    process.exit(0);
  }

  let out = src;
  const applied = [];
  for (const r of RULES) {
    if (out.includes(r.done)) { console.log(`[fix] ${r.id} already present`); continue; }
    const n = out.split(r.from).length - 1;
    if (n !== 1) {
      console.error(`[fix] ${r.id} anchor matched ${n} times (expected 1) — ABORT, bundle untouched`);
      process.exit(3);
    }
    out = out.replace(r.from, r.to);
    applied.push(r.id);
    console.log(`[fix] ${r.id} applied: ${r.what}`);
  }

  if (DRY) {
    console.log('[fix] --dry-run: not writing');
    return;
  }
  const backup = `${FILE}.bak-${Date.now()}`;
  fs.copyFileSync(FILE, backup);
  fs.writeFileSync(FILE, out);
  console.log(`[fix] wrote ${FILE} (${out.length} chars); backup: ${backup}`);
  console.log('[fix] now run: supervisorctl restart snowluma   # gateway only, QQ clients untouched');
}

main();
