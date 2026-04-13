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
import type { Container } from '../services/api';

interface QRItem {
    status: string;
    url?: string;
    type?: string;
    uin?: string;
}

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
            closeReasonRef.current = 'manual_close';
            try { wsRef.current.close(); } catch { /* ignore */ }
            wsRef.current = null;
        }

        const scheduleReconnect = (skip: boolean) => {
            if (skip || disposedRef.current || !en) return;
            const nextAttempt = reconnectAttemptRef.current + 1;
            reconnectAttemptRef.current = nextAttempt;
            setReconnectAttempt(nextAttempt);
            const backoff = Math.min(ri * (2 ** (nextAttempt - 1)), MAX_RECONNECT_INTERVAL);
            const jitter = Math.floor(Math.random() * MAX_RECONNECT_JITTER);
            timerRef.current = setTimeout(connect, backoff + jitter);
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
            setConnected(false);
            clearTimeout(heartbeatRef.current);
            const reason = closeReasonRef.current || classifyClose(event.code);
            closeReasonRef.current = null;
            setLastDisconnectReason(reason);
            const skipReconnect = !optRef.current.enabled || reason === 'capacity_limited';
            scheduleReconnect(skipReconnect);
        };
        ws.onerror = () => {
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

    const send = useCallback((msg: unknown) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify(msg));
        }
    }, []);

    return { containers, qrStates, connected, send, reconnectAttempt, lastDisconnectReason };
}

