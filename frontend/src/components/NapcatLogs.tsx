/**
 * NapcatLogs 组件 - 容器日志查看器
 * 从 ConfigEditor.tsx 提取，保留原有 UI 风格
 */
import { useState, useEffect, useRef } from 'react';
import {
    Box, Typography, Button, TextField, Paper, CircularProgress, Card, CardContent, useTheme
} from '@mui/material';
import RefreshIcon from '@mui/icons-material/Refresh';
import TerminalIcon from '@mui/icons-material/Terminal';
import DownloadIcon from '@mui/icons-material/Download';
import { useTranslate } from '../i18n';
import { containerApi } from '../services/api';

interface NapcatLogsProps {
    name: string;
    node_id: string;
}

export const NapcatLogs = ({ name, node_id }: NapcatLogsProps) => {
    const [logs, setLogs] = useState('');
    const [lines, setLines] = useState(200);
    const [loading, setLoading] = useState(false);
    const [autoRefresh, setAutoRefresh] = useState(true);
    const logsEndRef = useRef<HTMLDivElement>(null);
    const t = useTranslate();
    const theme = useTheme();

    const fetchLogs = async () => {
        if (!autoRefresh && logs) return; // if not auto refreshing, only fetch when triggered manually or logs are empty
        setLoading(true);
        try {
            const data = await containerApi.getLogs(name, lines, node_id);
            setLogs(data.logs || '');
            // 自动滚动到底部
            setTimeout(() => {
                logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
            }, 100);
        } catch (error) {
            console.error('Failed to fetch logs:', error);
        } finally {
            setLoading(false);
        }
    };

    const handleManualRefresh = () => {
        setLoading(true);
        containerApi.getLogs(name, lines, node_id)
            .then(data => {
                setLogs(data.logs || '');
                setTimeout(() => logsEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 100);
            })
            .catch(console.error)
            .finally(() => setLoading(false));
    };

    useEffect(() => {
        fetchLogs();
        let interval: NodeJS.Timeout;
        const start = () => { if (autoRefresh) interval = setInterval(fetchLogs, 3000); };
        const stop = () => { if (interval) clearInterval(interval); };
        const onVis = () => { document.hidden ? stop() : start(); };
        if (!document.hidden) start();
        document.addEventListener('visibilitychange', onVis);
        return () => { stop(); document.removeEventListener('visibilitychange', onVis); };
    }, [name, node_id, lines, autoRefresh]);

    return (
        <Box sx={{ mt: 1, height: '100%', display: 'flex', flexDirection: 'column' }}>
            <Box sx={{
                display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3,
                p: 2.5, borderRadius: 3, flexWrap: 'wrap', gap: 2,
                background: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                backdropFilter: 'blur(16px) saturate(1.2)',
                WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
                boxShadow: theme.palette.mode === 'dark' ? 'none' : '0 2px 12px rgba(0,0,0,0.06)'
            }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                    <Box sx={{ p: 1, borderRadius: 2, bgcolor: 'rgba(236,72,153,0.1)', display: 'flex' }}>
                        <TerminalIcon sx={{ fontSize: 24, color: '#ec4899' }} />
                    </Box>
                    <Box>
                        <Typography variant="h6" sx={{ fontWeight: 700, lineHeight: 1.2 }}>{t('config.containerLogs')}</Typography>
                        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                            {t('config.logsSubtitle')}
                        </Typography>
                    </Box>
                </Box>
                <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center', flexWrap: 'wrap' }}>
                    <TextField
                        size="small"
                        type="number"
                        label={t('config.fetchLines')}
                        value={lines}
                        onChange={(e) => setLines(parseInt(e.target.value) || 200)}
                        sx={{ width: 100, '& .MuiOutlinedInput-root': { borderRadius: 2 } }}
                    />
                    <Button
                        variant={autoRefresh ? "contained" : "outlined"}
                        color={autoRefresh ? "success" : "inherit"}
                        size="medium"
                        onClick={() => setAutoRefresh(!autoRefresh)}
                        sx={{ borderRadius: 2, textTransform: 'none', px: 2, fontWeight: 600, height: 40, boxShadow: 'none' }}
                    >
                        {autoRefresh ? t('config.autoRefreshOn') : t('config.autoRefreshOff')}
                    </Button>
                    <Button
                        startIcon={loading ? <CircularProgress size={16} color="inherit" /> : <RefreshIcon />}
                        onClick={handleManualRefresh}
                        disabled={loading}
                        size="medium"
                        variant="contained"
                        sx={{ borderRadius: 2, textTransform: 'none', px: 2, fontWeight: 600, height: 40, bgcolor: '#3b82f6', '&:hover': { bgcolor: '#2563eb' }, boxShadow: '0 4px 14px rgba(59,130,246,0.3)' }}
                    >
                        {t('config.manualRefresh')}
                    </Button>
                    <Button
                        startIcon={<DownloadIcon />}
                        size="medium"
                        variant="outlined"
                        onClick={() => {
                            window.open(`/api/containers/${name}/logs/download?lines=${lines}&node_id=${node_id}`, '_blank');
                        }}
                        sx={{ borderRadius: 2, textTransform: 'none', px: 2, fontWeight: 600, height: 40 }}
                    >
                        {t('config.exportLogs')}
                    </Button>
                </Box>
            </Box>

            <Paper
                elevation={0}
                sx={{
                    p: 2,
                    bgcolor: theme.palette.mode === 'dark' ? '#0f172a' : '#1e293b',
                    color: '#e2e8f0',
                    fontFamily: '"Fira Code", Consolas, Monaco, monospace',
                    fontSize: '0.85rem',
                    lineHeight: 1.6,
                    maxHeight: 'calc(100vh - 280px)',
                    minHeight: '400px',
                    overflow: 'auto',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-all',
                    borderRadius: 3,
                    border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.1)' : 'transparent'}`,
                    boxShadow: 'inset 0 2px 10px rgba(0,0,0,0.2)',
                    '&::-webkit-scrollbar': {
                        width: '8px',
                        height: '8px',
                    },
                    '&::-webkit-scrollbar-track': {
                        background: 'rgba(0,0,0,0.1)',
                        borderRadius: '4px',
                    },
                    '&::-webkit-scrollbar-thumb': {
                        background: 'rgba(255,255,255,0.2)',
                        borderRadius: '4px',
                        '&:hover': {
                            background: 'rgba(255,255,255,0.3)',
                        },
                    },
                }}
            >
                {logs ? (
                    <Box component="pre" sx={{ m: 0, fontFamily: 'inherit', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                        {logs}
                    </Box>
                ) : (
                    <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100%', minHeight: '300px', opacity: 0.5 }}>
                        <Typography sx={{ fontFamily: 'inherit' }}>{loading ? t('config.loadingLogs') : t('config.noLogs2')}</Typography>
                    </Box>
                )}
                <div ref={logsEndRef} />
            </Paper>
        </Box>
    );
};

