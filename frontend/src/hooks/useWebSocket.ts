/**
 * WebSocket Hook - 实时事件连接
 * 提供自动重连、心跳检测和状态管理
 */
import { useEffect, useRef, useState, useCallback } from 'react';

interface UseWSOptions {
    /** WebSocket URL path (e.g. /ws/events) */
    path: string;
    /** 自动重连间隔基数 (ms) */
    reconnectInterval?: number;
    /** 是否自动连接 */
    enabled?: boolean;
}

export type WSDisconnectReason =
    | 'unauthorized'
    | 'capacity_limited'
    | 'heartbeat_timeout'
    | 'network_error'
    | 'server_closed'
    | 'manual_close'
    | 'unknown';

const HEARTBEAT_TIMEOUT = 25000; // 25s 无消息则判定断线（后端容器多时推送间隔可达 5s）
const MAX_RECONNECT_INTERVAL = 30000;
const MAX_RECONNECT_JITTER = 1000;

function classifyClose(code: number): WSDisconnectReason {
    if (code === 4001) return 'unauthorized';
    if (code === 4429) return 'capacity_limited';
    if (code === 1000) return 'manual_close';
    if (code === 1006 || code === 1011 || code === 1012 || code === 1013) return 'server_closed';
    return 'unknown';
}

export function useWebSocket<T = unknown>(options: UseWSOptions) {
    const { path, reconnectInterval = 5000, enabled = true } = options;
    const [data, setData] = useState<T | null>(null);
    const [connected, setConnected] = useState(false);
    const [lastDisconnectReason, setLastDisconnectReason] = useState<WSDisconnectReason | null>(null);
    const [reconnectAttempt, setReconnectAttempt] = useState(0);
    const wsRef = useRef<WebSocket | null>(null);
    const timerRef = useRef<ReturnType<typeof setTimeout>>();
    const heartbeatRef = useRef<ReturnType<typeof setTimeout>>();
    const disposedRef = useRef(false);
    const reconnectAttemptRef = useRef(0);
    const closeReasonRef = useRef<WSDisconnectReason | null>(null);

    const optRef = useRef({ path, enabled, reconnectInterval });
    optRef.current = { path, enabled, reconnectInterval };

    const connect = useCallback(() => {
        const { path: p, enabled: en, reconnectInterval: ri } = optRef.current;
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
        const url = `${protocol}//${window.location.host}${p}`;
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
            const skipReconnect = reason === 'unauthorized' || (!optRef.current.enabled);
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
                setData(msg);
            } catch { /* ignore */ }
        };
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
    }, [path, enabled]);

    const send = useCallback((msg: unknown) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify(msg));
        }
    }, []);

    return { data, connected, send, reconnectAttempt, lastDisconnectReason };
}

