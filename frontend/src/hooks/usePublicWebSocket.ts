/**
 * 公开 WebSocket Hook — 用户面板专用
 *
 * 连接 /ws/public，接收容器列表 + QR 状态推送。
 * 替代 UserDashboard 中的 HTTP 轮询（fetchContainers + loadBatchQR）。
 *
 * 协议：
 *   服务端 → 客户端:
 *     {"type": "full",      "data": {"containers": [...], "qr": {...}}}
 *     {"type": "heartbeat"}
 *   客户端 → 服务端:
 *     {"type": "subscribe", "page": 1, "pageSize": 20}
 */
import { useEffect, useRef, useState, useCallback } from 'react';
import { publicApi, type Container, type QRResponse } from '../services/api';

type QRItem = QRResponse;

interface PublicWSData {
    containers: Container[];
    qr: Record<string, QRItem>;
}

interface UsePublicWSOptions {
    /** 是否启用 WS 连接 */
    enabled?: boolean;
    /** 自动重连间隔基数 (ms) */
    reconnectInterval?: number;
}

export type PublicWSDisconnectReason =
    | 'capacity_limited'
    | 'heartbeat_timeout'
    | 'network_error'
    | 'server_closed'
    | 'manual_close'
    | 'unknown';

const HEARTBEAT_TIMEOUT = 90000;
const MAX_RECONNECT_INTERVAL = 60000;
const MAX_RECONNECT_JITTER = 1000;
// 连接数打满不是错误而是排队，用 30~60s 的长退避一直重试（抖动分散开，避免所有人同时挤回来）
const CAPACITY_RECONNECT_BASE = 30000;
const CAPACITY_RECONNECT_JITTER = 30000;

function classifyClose(code: number): PublicWSDisconnectReason {
    if (code === 4429) return 'capacity_limited';
    if (code === 1000) return 'manual_close';
    if (code === 1006 || code === 1011 || code === 1012 || code === 1013) return 'server_closed';
    return 'unknown';
}

export function usePublicWebSocket(options: UsePublicWSOptions = {}) {
    const { enabled = true, reconnectInterval = 5000 } = options;
    const [containers, setContainers] = useState<Container[]>([]);
    const [qrStates, setQrStates] = useState<Record<string, QRItem>>({});
    const [connected, setConnected] = useState(false);
    const [lastDisconnectReason, setLastDisconnectReason] = useState<PublicWSDisconnectReason | null>(null);
    const [reconnectAttempt, setReconnectAttempt] = useState(0);
    const wsRef = useRef<WebSocket | null>(null);
    const timerRef = useRef<ReturnType<typeof setTimeout>>();
    const heartbeatRef = useRef<ReturnType<typeof setTimeout>>();
    const disposedRef = useRef(false);
    const reconnectAttemptRef = useRef(0);
    const closeReasonRef = useRef<PublicWSDisconnectReason | null>(null);
    const optRef = useRef({ enabled, reconnectInterval });
    optRef.current = { enabled, reconnectInterval };

    const connect = useCallback(() => {
        const { enabled: en, reconnectInterval: ri } = optRef.current;
        if (!en || disposedRef.current) return;

        if (wsRef.current) {
            // 先摘掉引用再关闭：onclose 是异步回调，只有靠 wsRef 比较旧 socket 才能认出自己已被取代
            const stale = wsRef.current;
            wsRef.current = null;
            try { stale.close(); } catch { /* ignore */ }
        }
        closeReasonRef.current = null;

        const scheduleReconnect = (reason: PublicWSDisconnectReason) => {
            if (disposedRef.current || !optRef.current.enabled) return;
            const nextAttempt = reconnectAttemptRef.current + 1;
            reconnectAttemptRef.current = nextAttempt;
            setReconnectAttempt(nextAttempt);
            const delay = reason === 'capacity_limited'
                ? CAPACITY_RECONNECT_BASE + Math.floor(Math.random() * CAPACITY_RECONNECT_JITTER)
                : Math.min(ri * (2 ** (nextAttempt - 1)), MAX_RECONNECT_INTERVAL) + Math.floor(Math.random() * MAX_RECONNECT_JITTER);
            timerRef.current = setTimeout(connect, delay);
        };

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${protocol}//${window.location.host}/ws/public`;

        const ws = new WebSocket(url);
        wsRef.current = ws;

        const resetHB = () => {
            clearTimeout(heartbeatRef.current);
            heartbeatRef.current = setTimeout(() => {
                closeReasonRef.current = 'heartbeat_timeout';
                wsRef.current?.close();
            }, HEARTBEAT_TIMEOUT);
        };

        ws.onopen = () => {
            if (disposedRef.current) { ws.close(); return; }
            reconnectAttemptRef.current = 0;
            setReconnectAttempt(0);
            setLastDisconnectReason(null);
            setConnected(true);
            resetHB();
        };
        ws.onclose = (event) => {
            // 旧 socket 的收尾晚于新连接建立时，既不能改状态（会把已连上的连接标成断开），也不能再排一次重连
            if (ws !== wsRef.current) return;
            wsRef.current = null;
            setConnected(false);
            clearTimeout(heartbeatRef.current);
            const reason = closeReasonRef.current || classifyClose(event.code);
            closeReasonRef.current = null;
            setLastDisconnectReason(reason);
            scheduleReconnect(reason);
        };
        ws.onerror = () => {
            // 已被取代的 socket 报错不能污染当前连接的断开原因
            if (ws !== wsRef.current) return;
            closeReasonRef.current = 'network_error';
            try { ws.close(); } catch { /* ignore */ }
        };
        ws.onmessage = (event) => {
            resetHB();
            try {
                const msg = JSON.parse(event.data);
                if (msg?.type === 'heartbeat') return;
                if (msg?.type === 'full' && msg.data) {
                    const d = msg.data as PublicWSData;
                    if (Array.isArray(d.containers)) {
                        setContainers(d.containers);
                    }
                    else if (d.containers && Array.isArray((d.containers as Record<string, unknown>).data)) {
                        setContainers((d.containers as Record<string, unknown>).data as Container[]);
                    }
                    if (d.qr) {
                        setQrStates(d.qr);
                    }
                }
            } catch { /* ignore */ }
        };
    }, []);

    useEffect(() => {
        disposedRef.current = false;
        connect();
        return () => {
            disposedRef.current = true;
            closeReasonRef.current = 'manual_close';
            clearTimeout(timerRef.current);
            clearTimeout(heartbeatRef.current);
            if (wsRef.current) {
                try { wsRef.current.close(); } catch { /* ignore */ }
                wsRef.current = null;
            }
        };
    }, [connect]);

    useEffect(() => {
        if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
            closeReasonRef.current = 'manual_close';
            wsRef.current.close();
        }
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [enabled]);

    // 手机切后台、锁屏、合盖期间定时器被系统节流，心跳检测形同虚设：
    // 回到前台必须以 readyState 为准重新判断，否则指示灯停在“已连接”而数据早就停更，
    // 还要干等最长 60s 的退避才恢复。HTTP 兜底拉一次，不必等重连握手完成。
    useEffect(() => {
        const handleResume = () => {
            if (document.visibilityState !== 'visible') return;
            if (disposedRef.current || !optRef.current.enabled) return;
            if (wsRef.current?.readyState === WebSocket.OPEN) return;
            setConnected(false);
            reconnectAttemptRef.current = 0;
            setReconnectAttempt(0);
            clearTimeout(timerRef.current);
            connect();
            void (async () => {
                try {
                    const [list, qr] = await Promise.all([publicApi.containers(), publicApi.batchQR()]);
                    if (disposedRef.current) return;
                    if (Array.isArray(list.containers)) setContainers(list.containers);
                    if (qr.items) setQrStates(qr.items);
                } catch { /* 拉不到就等 WS 重连补上 */ }
            })();
        };
        document.addEventListener('visibilitychange', handleResume);
        window.addEventListener('online', handleResume);
        return () => {
            document.removeEventListener('visibilitychange', handleResume);
            window.removeEventListener('online', handleResume);
        };
    }, [connect]);

    const send = useCallback((msg: unknown) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify(msg));
        }
    }, []);

    return { containers, qrStates, connected, send, reconnectAttempt, lastDisconnectReason };
}

