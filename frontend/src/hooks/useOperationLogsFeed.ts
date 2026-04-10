import { useEffect, useLayoutEffect, useRef, useState, type MutableRefObject } from 'react';

import { type OperationLog, type OperationLogsQuery, type OperationLogsResponse, operationLogsApi } from '../services/operationLogs';

export interface OperationLogsFilters {
    limit: number;
    page: number;
    operator: string;
    type: string;
    level: 'info' | 'warning' | 'error' | '';
}

export interface UseOperationLogsFeedResult {
    logs: OperationLog[];
    loading: boolean;
    totalPages: number;
    pendingNewCount: number;
    highlightedLogIds: string[];
    listRef: MutableRefObject<HTMLUListElement | null>;
    fetchLogs: (mode?: 'auto' | 'manual' | 'initial') => Promise<void>;
    scrollToTop: () => void;
}

export function buildOperationLogsQuery(filters: OperationLogsFilters): OperationLogsQuery {
    return {
        limit: filters.limit,
        page: filters.page,
        operator: filters.operator.trim(),
        type: filters.type.trim(),
        level: filters.level,
    };
}

export function useOperationLogsFeed(
    filters: OperationLogsFilters,
    onError: (message: string) => void,
): UseOperationLogsFeedResult {
    const [logs, setLogs] = useState<OperationLog[]>([]);
    const [loading, setLoading] = useState(false);
    const [totalPages, setTotalPages] = useState(0);
    const [pendingNewCount, setPendingNewCount] = useState(0);
    const [highlightedLogIds, setHighlightedLogIds] = useState<string[]>([]);
    const listRef = useRef<HTMLUListElement | null>(null);
    const preserveScrollRef = useRef(false);
    const scrollOffsetRef = useRef(0);
    const latestLogIdRef = useRef<string | null>(null);
    const highlightTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const captureScrollState = () => {
        const list = listRef.current;
        if (!list) {
            preserveScrollRef.current = false;
            return;
        }
        preserveScrollRef.current = list.scrollTop > 24;
        if (preserveScrollRef.current) {
            scrollOffsetRef.current = list.scrollHeight - list.scrollTop;
        }
    };

    const scrollToTop = () => {
        const list = listRef.current;
        if (!list) return;
        list.scrollTo({ top: 0, behavior: 'smooth' });
        preserveScrollRef.current = false;
        setPendingNewCount(0);
    };

    const applyResponse = (data: OperationLogsResponse, mode: 'auto' | 'manual' | 'initial') => {
        const nextLogs = data.logs || [];
        const previousTopId = latestLogIdRef.current;
        const nextTopId = nextLogs[0]?.id || null;
        const previousTopIndex = previousTopId ? nextLogs.findIndex((log) => log.id === previousTopId) : -1;
        const newLogs = previousTopIndex > 0 ? nextLogs.slice(0, previousTopIndex) : [];

        if (preserveScrollRef.current && mode === 'auto' && newLogs.length > 0) {
            setPendingNewCount((count) => count + newLogs.length);
        } else if (!preserveScrollRef.current || mode !== 'auto') {
            setPendingNewCount(0);
        }

        if (newLogs.length > 0) {
            const ids = newLogs.map((log) => log.id).filter(Boolean) as string[];
            setHighlightedLogIds(ids);
            if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current);
            highlightTimerRef.current = setTimeout(() => setHighlightedLogIds([]), 3000);
        }

        latestLogIdRef.current = nextTopId;
        setLogs(nextLogs);
        setTotalPages(data.pagination?.pages || 0);
    };

    const fetchLogs = async (mode: 'auto' | 'manual' | 'initial' = 'manual') => {
        captureScrollState();
        setLoading(true);
        try {
            const data = await operationLogsApi.list(buildOperationLogsQuery(filters));
            applyResponse(data, mode);
        } catch {
            onError('获取操作日志失败');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        latestLogIdRef.current = null;
        fetchLogs('initial');
    }, [filters.limit, filters.page, filters.operator, filters.type, filters.level]);

    useLayoutEffect(() => {
        const list = listRef.current;
        if (!list || !preserveScrollRef.current) return;
        list.scrollTop = Math.max(0, list.scrollHeight - scrollOffsetRef.current);
    }, [logs]);

    useEffect(() => {
        const list = listRef.current;
        if (!list) return;
        const handleScroll = () => {
            if (list.scrollTop <= 24) {
                setPendingNewCount(0);
                preserveScrollRef.current = false;
            }
        };
        list.addEventListener('scroll', handleScroll);
        return () => list.removeEventListener('scroll', handleScroll);
    }, [logs.length]);

    useEffect(() => () => {
        if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current);
    }, []);

    useEffect(() => {
        let interval: ReturnType<typeof setInterval>;
        const start = () => {
            interval = setInterval(() => { void fetchLogs('auto'); }, 15000);
        };
        const stop = () => clearInterval(interval);
        const onVisibilityChange = () => { document.hidden ? stop() : start(); };
        if (!document.hidden) start();
        document.addEventListener('visibilitychange', onVisibilityChange);
        return () => {
            stop();
            document.removeEventListener('visibilitychange', onVisibilityChange);
        };
    }, [filters.limit, filters.page, filters.operator, filters.type, filters.level]);

    return { logs, loading, totalPages, pendingNewCount, highlightedLogIds, listRef, fetchLogs, scrollToTop };
}

