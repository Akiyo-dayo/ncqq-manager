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
    /** 页面重新可见且连接已断时回调 —— 用来补一次 HTTP 兜底拉取，不必干等重连握手 */
    onResume?: () => void;
}

export type WSDisconnectReason =
    | 'unauthorized'
    | 'capacity_limited'
    | 'heartbeat_timeout'
    | 'network_error'
    | 'server_closed'
    | 'manual_close'
    | 'unknown';

const HEARTBEAT_TIMEOUT = 90000; // 90s 无消息则判定断线（后端 WS 推送间隔 30s）
const MAX_RECONNECT_INTERVAL = 60000;
const MAX_RECONNECT_JITTER = 1000;

function classifyClose(code: number): WSDisconnectReason {
    if (code === 4001) return 'unauthorized';
    if (code === 4429) return 'capacity_limited';
    if (code === 1000) return 'manual_close';
    if (code === 1006 || code === 1011 || code === 1012 || code === 1013) return 'server_closed';
    return 'unknown';
}

export function useWebSocket<T = unknown>(options: UseWSOptions) {
    const { path, reconnectInterval = 5000, enabled = true, onResume } = options;
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
    const onResumeRef = useRef(onResume);
    onResumeRef.current = onResume;

    const connect = useCallback(() => {
        const { path: p, enabled: en, reconnectInterval: ri } = optRef.current;
        if (!en || disposedRef.current) return;

        if (wsRef.current) {
            // 先摘掉引用再关闭：onclose 是异步回调，只有靠 wsRef 比较旧 socket 才能认出自己已被取代
            const stale = wsRef.current;
            wsRef.current = null;
            try { stale.close(); } catch { /* ignore */ }
        }
        closeReasonRef.current = null;

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
            // 旧 socket 的收尾晚于新连接建立时，既不能改状态（会把已连上的连接标成断开），也不能再排一次重连
            if (ws !== wsRef.current) return;
            wsRef.current = null;
            setConnected(false);
            clearTimeout(heartbeatRef.current);
            const reason = closeReasonRef.current || classifyClose(event.code);
            closeReasonRef.current = null;
            setLastDisconnectReason(reason);
            if (reason === 'unauthorized') {
                // 会话已失效，光断开还不够 —— 得让 AuthContext 清掉本地 user 并把人送回登录页
                window.dispatchEvent(new CustomEvent('auth:unauthorized'));
            }
            const skipReconnect = reason === 'unauthorized' || (!optRef.current.enabled);
            scheduleReconnect(skipReconnect);
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

    // 手机切后台、锁屏、合盖期间定时器被系统节流，心跳检测形同虚设：
    // 回到前台必须以 readyState 为准重新判断，否则指示灯停在“已连接”而数据早就停更，
    // 还要干等最长 60s 的退避才恢复。
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
            onResumeRef.current?.();
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

    return { data, connected, send, reconnectAttempt, lastDisconnectReason };
}

