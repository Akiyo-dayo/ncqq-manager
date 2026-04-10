import React, { useState, useEffect, useLayoutEffect, useRef } from 'react';
import {
    Box,
    Paper,
    Typography,
    FormControl,
    TextField,
    Select,
    MenuItem,
    Button,
    List,
    ListItem,
    ListItemIcon,
    ListItemText,
    Chip,
    CircularProgress,
    Pagination,
} from '@mui/material';
import { Refresh as RefreshIcon, FiberManualRecord as DotIcon, Download as DownloadIcon } from '@mui/icons-material';
import { operationLogsApi, type OperationLog, type OperationLogsQuery } from '../services/api';
import { useToast } from '../components/Toast';
import { useTranslate } from '../i18n';

const OperationLogs: React.FC = () => {
    const [logs, setLogs] = useState<OperationLog[]>([]);
    const [loading, setLoading] = useState(false);
    const [limit, setLimit] = useState(50);
    const [page, setPage] = useState(1);
    const [totalPages, setTotalPages] = useState(0);
    const [operator, setOperator] = useState('');
    const [type, setType] = useState('');
    const [level, setLevel] = useState<'info' | 'warning' | 'error' | ''>('');
    const [pendingNewCount, setPendingNewCount] = useState(0);
    const [highlightedLogIds, setHighlightedLogIds] = useState<string[]>([]);
    const t = useTranslate();

    const toast = useToast();
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
        const topThreshold = 24;
        preserveScrollRef.current = list.scrollTop > topThreshold;
        if (preserveScrollRef.current) {
            scrollOffsetRef.current = list.scrollHeight - list.scrollTop;
        }
    };

    const scrollToTop = () => {
        const list = listRef.current;
        if (!list) {
            return;
        }
        list.scrollTo({ top: 0, behavior: 'smooth' });
        preserveScrollRef.current = false;
        setPendingNewCount(0);
    };


    const buildQuery = (): OperationLogsQuery => ({
        limit,
        page,
        operator: operator.trim(),
        type: type.trim(),
        level,
    });

    const fetchLogs = async (mode: 'auto' | 'manual' | 'initial' = 'manual') => {
        captureScrollState();
        setLoading(true);
        try {
            const data = await operationLogsApi.list(buildQuery());
            const nextLogs = data.logs || [];
            const previousTopId = latestLogIdRef.current;
            const nextTopId = nextLogs[0]?.id || null;
            const previousTopIndex = previousTopId
                ? nextLogs.findIndex((log) => log.id === previousTopId)
                : -1;
            const newLogs = previousTopIndex > 0 ? nextLogs.slice(0, previousTopIndex) : [];

            if (preserveScrollRef.current && mode === 'auto' && newLogs.length > 0) {
                setPendingNewCount((count) => count + newLogs.length);
            } else if (!preserveScrollRef.current || mode !== 'auto') {
                setPendingNewCount(0);
            }

            if (newLogs.length > 0) {
                const ids = newLogs.map((log) => log.id).filter(Boolean) as string[];
                setHighlightedLogIds(ids);
                if (highlightTimerRef.current) {
                    clearTimeout(highlightTimerRef.current);
                }
                highlightTimerRef.current = setTimeout(() => {
                    setHighlightedLogIds([]);
                }, 3000);
            }

            latestLogIdRef.current = nextTopId;
            setLogs(nextLogs);
            setTotalPages(data.pagination?.pages || 0);
        } catch (error) {
            toast.error(t('opLogs.fetchFailed'));
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        latestLogIdRef.current = null;
        fetchLogs('initial');
    }, [limit, page, operator, type, level]);

    useLayoutEffect(() => {
        const list = listRef.current;
        if (!list || !preserveScrollRef.current) {
            return;
        }
        list.scrollTop = Math.max(0, list.scrollHeight - scrollOffsetRef.current);
    }, [logs]);

    useEffect(() => {
        const list = listRef.current;
        if (!list) {
            return;
        }
        const handleScroll = () => {
            if (list.scrollTop <= 24) {
                setPendingNewCount(0);
                preserveScrollRef.current = false;
            }
        };
        list.addEventListener('scroll', handleScroll);
        return () => list.removeEventListener('scroll', handleScroll);
    }, [logs.length]);
    useEffect(() => {
        return () => {
            if (highlightTimerRef.current) {
                clearTimeout(highlightTimerRef.current);
            }
        };
    }, []);

    // 15s 自动刷新 + 可见性感知
    useEffect(() => {
        let interval: ReturnType<typeof setInterval>;
        const start = () => {
            interval = setInterval(() => {
                fetchLogs('auto');
            }, 15000);
        };
        const stop = () => {
            clearInterval(interval);
        };
        const onVis = () => {
            document.hidden ? stop() : start();
        };
        if (!document.hidden) {
            start();
        }
        document.addEventListener('visibilitychange', onVis);
        return () => {
            stop();
            document.removeEventListener('visibilitychange', onVis);
        };
    }, [limit, page, operator, type, level]);

    const getLevelColor = (level: string): 'info' | 'warning' | 'error' | 'default' => {
        switch (level) {
            case 'info':
                return 'info';
            case 'warning':
                return 'warning';
            case 'error':
                return 'error';
            default:
                return 'default';
        }
    };

    const formatLogText = (log: OperationLog): string => {
        const operator = log.operator || log.id;
        const target = log.target || '';

        switch (log.type) {
            case 'container_start':
                return t('opLogs.containerStart').replace('{operator}', operator).replace('{target}', target);
            case 'container_stop':
                return t('opLogs.containerStop').replace('{operator}', operator).replace('{target}', target);
            case 'container_restart':
                return t('opLogs.containerRestart').replace('{operator}', operator).replace('{target}', target);
            case 'container_create':
                return t('opLogs.containerCreate').replace('{operator}', operator).replace('{target}', target);
            case 'container_delete':
                return t('opLogs.containerDelete').replace('{operator}', operator).replace('{target}', target);
            case 'user_login':
                return t('opLogs.userLogin').replace('{operator}', operator).replace('{ip}', log.operator_ip || 'unknown');
            case 'user_create':
                return t('opLogs.userCreate').replace('{operator}', operator).replace('{target}', target);
            case 'user_delete':
                return t('opLogs.userDelete').replace('{operator}', operator).replace('{target}', target);
            case 'config_change':
                return t('opLogs.configChange').replace('{operator}', operator);
            case 'node_create':
                return t('opLogs.nodeCreate').replace('{operator}', operator).replace('{target}', target);
            case 'node_delete':
                return t('opLogs.nodeDelete').replace('{operator}', operator).replace('{target}', target);
            default:
                return t('opLogs.unknownAction').replace('{operator}', operator).replace('{type}', log.type);
        }
    };

    return (
        <Box sx={{ p: 3 }}>
            <Paper sx={{ p: 3 }}>
                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
                    <Typography variant="h5">{t('opLogs.title')}</Typography>
                    <Box sx={{ display: 'flex', gap: 2, alignItems: 'center' }}>
                        <FormControl size="small" sx={{ minWidth: 120 }}>
                            <Select
                                value={limit}
                                onChange={(e) => {
                                    setLimit(Number(e.target.value));
                                    setPage(1);
                                }}
                            >
                                <MenuItem value={20}>20 {t('opLogs.records')}</MenuItem>
                                <MenuItem value={50}>50 {t('opLogs.records')}</MenuItem>
                                <MenuItem value={100}>100 {t('opLogs.records')}</MenuItem>
                                <MenuItem value={200}>200 {t('opLogs.records')}</MenuItem>
                            </Select>
                        </FormControl>
                        <TextField
                            size="small"
                            label={t('opLogs.operator')}
                            value={operator}
                            onChange={(e) => {
                                setOperator(e.target.value);
                                setPage(1);
                            }}
                        />
                        <TextField
                            size="small"
                            label={t('opLogs.type')}
                            value={type}
                            onChange={(e) => {
                                setType(e.target.value);
                                setPage(1);
                            }}
                        />
                        <FormControl size="small" sx={{ minWidth: 120 }}>
                            <Select
                                displayEmpty
                                value={level}
                                onChange={(e) => {
                                    setLevel(e.target.value as 'info' | 'warning' | 'error' | '');
                                    setPage(1);
                                }}
                            >
                                <MenuItem value="">{t('opLogs.allLevels')}</MenuItem>
                                <MenuItem value="info">info</MenuItem>
                                <MenuItem value="warning">warning</MenuItem>
                                <MenuItem value="error">error</MenuItem>
                            </Select>
                        </FormControl>
                        <Button
                            variant="outlined"
                            startIcon={<RefreshIcon />}
                            onClick={() => fetchLogs('manual')}
                            disabled={loading}
                        >
                            {t('admin.refresh')}
                        </Button>
                        <Button
                            variant="outlined"
                            startIcon={<DownloadIcon />}
                            onClick={() => {
                                const params = new URLSearchParams();
                                params.set('limit', String(limit));
                                params.set('page', String(page));
                                if (operator.trim()) params.set('operator', operator.trim());
                                if (type.trim()) params.set('type', type.trim());
                                if (level) params.set('level', level);
                                window.open(`/api/operation_logs/download?${params.toString()}`, '_blank');
                            }}
                        >
                            {t('config.exportLogs')}
                        </Button>
                 </Box>
                </Box>

                {pendingNewCount > 0 && (
                    <Box
                        sx={{
                            position: 'sticky',
                            top: 0,
                            zIndex: 1,
                            display: 'flex',
                            justifyContent: 'center',
                            py: 1,
                            mb: 2,
                            backgroundColor: 'background.paper',
                        }}
                    >
                        <Button
                            size="small"
                            variant="contained"
                            onClick={scrollToTop}
                            sx={{ textTransform: 'none' }}
                        >
                            {`有 ${pendingNewCount} 条新日志，点击回到顶部`}
                        </Button>
                    </Box>
                )}

                {loading ? (
                    <Box sx={{ display: 'flex', justifyContent: 'center', py: 5 }}>
                        <CircularProgress />
                    </Box>
                ) : logs.length === 0 ? (
                    <Box sx={{ textAlign: 'center', py: 5, color: 'text.secondary' }}>
                        <Typography variant="body1">{t('opLogs.noLogs')}</Typography>
                        <Typography variant="body2" sx={{ mt: 1 }}>
                            {t('opLogs.noLogsHint')}
                        </Typography>
                    </Box>
                ) : (
                    <List
                        ref={listRef}
                        dense
                        sx={{ maxHeight: '70vh', overflowY: 'auto' }}
                    >
                        {logs.map((log) => {
                            const isHighlighted = highlightedLogIds.includes(log.id);
                            return (
                                <ListItem
                                    key={log.id || log.timestamp}
                                    sx={{
                                        borderBottom: '1px solid',
                                        borderColor: 'divider',
                                        backgroundColor: isHighlighted ? 'action.hover' : 'transparent',
                                        transition: 'background-color 0.6s ease',
                                    }}
                                >
                                    <ListItemIcon sx={{ minWidth: 32 }}>
                                        <DotIcon sx={{ fontSize: 12, color: getLevelColor(log.level) === 'info' ? 'info.main' : getLevelColor(log.level) === 'warning' ? 'warning.main' : getLevelColor(log.level) === 'error' ? 'error.main' : 'grey.500' }} />
                                    </ListItemIcon>
                                    <ListItemText
                                        primary={formatLogText(log)}
                                        secondary={log.time}
                                    />
                                    <Chip label={log.level} size="small" color={getLevelColor(log.level)} variant="outlined" />
                                </ListItem>
                            );
                        })}
                    </List>
                )}

                {totalPages > 1 && (
                    <Box sx={{ display: 'flex', justifyContent: 'center', mt: 3 }}>
                        <Pagination
                            color="primary"
                            count={totalPages}
                            page={page}
                            onChange={(_, value) => setPage(value)}
                        />
                    </Box>
                )}
            </Paper>
        </Box>
    );
};

export default OperationLogs;

