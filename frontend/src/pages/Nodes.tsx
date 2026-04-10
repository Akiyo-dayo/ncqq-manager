import { useState, useEffect, useRef, useCallback } from 'react';
import { Box, Typography, Button, TextField, Skeleton, IconButton, useTheme, Dialog, DialogTitle, DialogContent, DialogActions, CircularProgress, Chip } from '@mui/material';
import HubIcon from '@mui/icons-material/Hub';
import RefreshIcon from '@mui/icons-material/Refresh';
import DesktopWindowsIcon from '@mui/icons-material/DesktopWindows';
import AddIcon from '@mui/icons-material/Add';
import SettingsIcon from '@mui/icons-material/Settings';
import ContentCopyIcon from '@mui/icons-material/ContentCopy';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import TerminalIcon from '@mui/icons-material/Terminal';
import CloseIcon from '@mui/icons-material/Close';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import { nodeApi, type Node } from '../services/api';
import { useTranslate } from '../i18n';
import { useToast } from '../components/Toast';
import MiniChart from '../components/MiniChart';

export default function Nodes() {
    const theme = useTheme();
    const t = useTranslate();
    const toast = useToast();
    const [loading, setLoading] = useState(true);
    const [nodes, setNodes] = useState<Node[]>([]);
    const [remoteLoading, setRemoteLoading] = useState(true);  // 远程节点状态加载中
    const [openDialog, setOpenDialog] = useState(false);

    // form state
    const [editNodeId, setEditNodeId] = useState<string | null>(null);
    const [nodeName, setNodeName] = useState('');
    const [nodeAddress, setNodeAddress] = useState('');
    const [nodeApiKey, setNodeApiKey] = useState('');

    // console log dialog state
    const [logDialogOpen, setLogDialogOpen] = useState(false);
    const [logNodeId, setLogNodeId] = useState('');
    const [logNodeName, setLogNodeName] = useState('');
    const [logContent, setLogContent] = useState('');
    const [logLoading, setLogLoading] = useState(false);
    const [logLines, setLogLines] = useState(500);
    const [logAutoRefresh, setLogAutoRefresh] = useState(false);
    const logEndRef = useRef<HTMLDivElement>(null);
    const logIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

    const fetchNodes = async () => {
        setLoading(true);
        setRemoteLoading(true);
        try {
            // 第一阶段：quick 模式 — 本地节点完整，远程节点骨架（<50ms）
            const data = await nodeApi.list(true);
            setNodes(data.nodes || []);
        } catch (e) {
            console.error(e);
        } finally {
            setLoading(false);
        }
        // 第二阶段：异步获取远程节点完整状态（含健康检查）
        try {
            const full = await nodeApi.list(false);
            setNodes(full.nodes || []);
        } catch { /* 远程状态获取失败不影响页面 */ }
        setRemoteLoading(false);
    };

    useEffect(() => {
        fetchNodes();
    }, []);

    // ============ Console Log Handlers ============

    const fetchLogContent = useCallback(async (nodeId: string, lines: number) => {
        setLogLoading(true);
        try {
            const data = await nodeApi.getLogs(nodeId, lines);
            setLogContent(data.logs || '');
            setTimeout(() => logEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 100);
        } catch {
            setLogContent(t('nodePanel.logFetchError'));
        } finally {
            setLogLoading(false);
        }
    }, [t]);

    const handleOpenConsole = async (node: Node) => {
        setLogNodeId(node.id);
        setLogNodeName(node.name);
        setLogContent('');
        setLogDialogOpen(true);
        await fetchLogContent(node.id, logLines);
    };

    const handleCloseConsole = () => {
        setLogDialogOpen(false);
        setLogAutoRefresh(false);
        if (logIntervalRef.current) {
            clearInterval(logIntervalRef.current);
            logIntervalRef.current = null;
        }
    };

    const toggleAutoRefresh = () => {
        if (logAutoRefresh) {
            setLogAutoRefresh(false);
            if (logIntervalRef.current) {
                clearInterval(logIntervalRef.current);
                logIntervalRef.current = null;
            }
        } else {
            setLogAutoRefresh(true);
            logIntervalRef.current = setInterval(() => {
                fetchLogContent(logNodeId, logLines);
            }, 5000);
        }
    };

    useEffect(() => {
        return () => {
            if (logIntervalRef.current) clearInterval(logIntervalRef.current);
        };
    }, []);

    // count error/warning lines in logs
    const errorCount = (logContent.match(/error/gi) || []).length;
    const warnCount = (logContent.match(/warn/gi) || []).length;

    const handleOpenAdd = () => {
        setEditNodeId(null);
        setNodeName('');
        setNodeAddress('127.0.0.1:8000');
        setNodeApiKey('');
        setOpenDialog(true);
    };

    const handleOpenEdit = (node: Node) => {
        setEditNodeId(node.id);
        setNodeName(node.name);
        setNodeAddress(node.address);
        setNodeApiKey(''); // Don't show existing key
        setOpenDialog(true);
    };

    const handleDelete = async (id: string, name: string) => {
        if (!window.confirm(t('admin.confirmDelete').replace('{name}', name))) return;
        try {
            await nodeApi.delete(id);
            toast.success(`${name} ${t('admin.deleteText')} ✓`);
            fetchNodes();
        } catch (e) {
            toast.error(`${name} ${t('admin.deleteText')} ✗`);
        }
    };

    const handleSave = async () => {
        try {
            if (editNodeId) {
                await nodeApi.edit(editNodeId, nodeName, nodeAddress, nodeApiKey);
                toast.success(`${nodeName} updated ✓`);
            } else {
                await nodeApi.add(nodeName, nodeAddress, nodeApiKey);
                toast.success(`${nodeName} added ✓`);
            }
            setOpenDialog(false);
            fetchNodes();
        } catch (e) {
            toast.error(String(e));
        }
    };

    return (
        <Box sx={{ p: { xs: 3, md: 6 }, maxWidth: 1200, mx: 'auto' }}>
            <Box sx={{ mb: 4 }}>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>{t('nodePanel.breadcrumb')}</Typography>
                <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 2, justifyContent: 'space-between', alignItems: 'center' }}>
                    <Typography variant="h5" sx={{ fontWeight: 700, display: 'flex', alignItems: 'center', gap: 1 }}>
                        <HubIcon sx={{ color: '#3b82f6' }} /> {t('nodePanel.title')}
                    </Typography>

                    <Box sx={{ display: 'flex', gap: 2, alignItems: 'center' }}>
                        <Button variant="outlined" color="inherit" onClick={fetchNodes} startIcon={<RefreshIcon />} sx={{ borderRadius: 2, height: 38, borderColor: theme.palette.divider, bgcolor: theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.02)' : '#fff' }}>
                            {t('admin.refresh')}
                        </Button>
                        <Button variant="contained" onClick={handleOpenAdd} startIcon={<AddIcon />} sx={{ borderRadius: 2, background: '#2563eb', height: 38, px: 3, boxShadow: 'none', '&:hover': { background: '#1d4ed8', boxShadow: 'none' } }}>
                            {t('nodePanel.addNode')}
                        </Button>
                        <Button variant="outlined" color="inherit" onClick={() => window.open('/manual', '_blank')} sx={{ borderRadius: 2, height: 38, borderColor: theme.palette.divider, bgcolor: theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.02)' : '#fff' }}>
                            {t('nodePanel.manual')}
                        </Button>
                    </Box>
                </Box>
                <Typography variant="body2" color="text.secondary" sx={{ mt: 3, maxWidth: 600, lineHeight: 1.6 }}>
                    {t('nodePanel.description')}
                </Typography>
            </Box>

            <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(400px, 1fr))', gap: 3 }}>
                {loading ? (
                    [...Array(2)].map((_, i) => <Skeleton key={i} variant="rounded" height={300} sx={{ borderRadius: 3 }} />)
                ) : nodes.map(node => {
                    const isRemoteLoading = remoteLoading && node.id !== 'local' && node.status === 'unknown';
                    return (
                    <Box key={node.id} sx={{ position: 'relative', borderRadius: 3, background: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)', backdropFilter: 'blur(16px) saturate(1.2)', WebkitBackdropFilter: 'blur(16px) saturate(1.2)', border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`, overflow: 'hidden', transition: 'all 0.3s', '&:hover': { border: '1px solid rgba(59,130,246,0.5)', boxShadow: '0 8px 24px rgba(0,0,0,0.1)' } }}>
                        {/* 远程节点加载遮罩 */}
                        {isRemoteLoading && (
                            <Box sx={{
                                position: 'absolute', inset: 0, zIndex: 10,
                                display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 1.5,
                                bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,35,0.85)' : 'rgba(255,255,255,0.85)',
                                backdropFilter: 'blur(4px)', borderRadius: 3,
                            }}>
                                <CircularProgress size={28} sx={{ color: '#3b82f6' }} />
                                <Typography variant="caption" color="text.secondary">{t('nodePanel.connecting') || '正在连接...'}</Typography>
                            </Box>
                        )}
                        <Box sx={{ p: 3 }}>
                            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
                                <Typography variant="h6" sx={{ fontWeight: 700, display: 'flex', alignItems: 'center', gap: 1 }}>
                                    <DesktopWindowsIcon fontSize="small" color="action" /> {node.name}
                                    {node.id === 'local' && (
                                        <Box component="span" sx={{ fontSize: '0.65rem', bgcolor: '#3b82f6', color: '#fff', px: 0.8, py: 0.2, borderRadius: 1, ml: 1 }}>
                                            LOCAL
                                        </Box>
                                    )}
                                </Typography>
                                <Box sx={{ display: 'flex', gap: 0.5 }}>
                                    <IconButton size="small" title={t('nodePanel.console')} onClick={() => handleOpenConsole(node)}><TerminalIcon fontSize="small" /></IconButton>
                                    <IconButton size="small" title={t('nodePanel.nodeSettings')} onClick={() => handleOpenEdit(node)}><SettingsIcon fontSize="small" /></IconButton>
                                    {node.id !== 'local' && (
                                        <IconButton size="small" title={t('nodePanel.deleteNode')} onClick={() => handleDelete(node.id, node.name)} color="error"><DeleteOutlineIcon fontSize="small" /></IconButton>
                                    )}
                                </Box>
                            </Box>

                            <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 2, mb: 3 }}>
                                <Box>
                                    <Typography variant="caption" color="text.secondary" display="block">{t('nodePanel.address')}</Typography>
                                    <Typography variant="body2">{node.address}</Typography>
                                </Box>
                                {node.api_key && (
                                    <Box sx={{ gridColumn: 'span 2' }}>
                                        <Typography variant="caption" color="text.secondary" display="block">{t('nodePanel.apiKeyLabel')}</Typography>
                                        <Typography variant="body2" sx={{ filter: 'blur(3px)', transition: '0.3s', cursor: 'pointer', '&:hover': { filter: 'blur(0)' } }} title={t('nodePanel.hoverToView')}>{node.api_key}</Typography>
                                    </Box>
                                )}
                                <Box>
                                    <Typography variant="caption" color="text.secondary" display="block">{t('nodePanel.nodeStatus')}</Typography>
                                    <Typography variant="body2" sx={{ color: node.status === 'online' ? '#10b981' : '#f43f5e', display: 'flex', alignItems: 'center', gap: 0.5 }}>
                                        <Box sx={{ width: 6, height: 6, borderRadius: '50%', bgcolor: node.status === 'online' ? '#10b981' : '#f43f5e' }} />
                                        {node.status === 'online' ? t('admin.online') : t('admin.offline')}
                                    </Typography>
                                </Box>
                                <Box>
                                    <Typography variant="caption" color="text.secondary" display="block">{t('nodePanel.directConnect')}</Typography>
                                    <Typography variant="body2" sx={{ color: node.status === 'online' ? '#10b981' : '#f43f5e', display: 'flex', alignItems: 'center', gap: 0.5 }}>
                                        <Box sx={{ width: 6, height: 6, borderRadius: '50%', bgcolor: node.status === 'online' ? '#10b981' : '#f43f5e' }} />
                                        {node.status === 'online' ? t('nodePanel.reachable') : t('nodePanel.unreachable')}
                                    </Typography>
                                </Box>
                                <Box>
                                    <Typography variant="caption" color="text.secondary" display="block">{t('nodePanel.latency')}</Typography>
                                    <Typography variant="body2" sx={{ color: node.ping < 100 ? '#10b981' : (node.ping < 300 ? '#f59e0b' : '#f43f5e') }}>
                                        {node.status === 'online' ? `${node.ping}ms` : '-'}
                                    </Typography>
                                </Box>
                                <Box>
                                    <Typography variant="caption" color="text.secondary" display="block">{t('nodePanel.platform')}</Typography>
                                    <Typography variant="body2">{node.system?.platform || '-'} / {node.system?.python_version || '-'}</Typography>
                                </Box>
                                <Box>
                                    <Typography variant="caption" color="text.secondary" display="block">{t('nodePanel.loadCpuMem')}</Typography>
                                    <Typography variant="body2">{node.system?.cpu_percent?.toFixed(1) || 0}% / {node.system?.mem_percent?.toFixed(1) || 0}%</Typography>
                                </Box>
                                <Box>
                                    <Typography variant="caption" color="text.secondary" display="block">{t('nodePanel.instanceStatus')}</Typography>
                                    <Typography variant="body2">{node.instances?.running || 0} / {node.instances?.total || 0}</Typography>
                                </Box>
                                <Box>
                                    <Typography variant="caption" color="text.secondary" display="block">{t('nodePanel.coreVersion')}</Typography>
                                    <Typography variant="body2" sx={{ color: '#10b981' }}>{node.system?.app_version ? `v${node.system.app_version}` : '-'}</Typography>
                                </Box>
                                <Box sx={{ gridColumn: 'span 2' }}>
                                    <Typography variant="caption" color="text.secondary" display="block">Node ID</Typography>
                                    <Typography variant="body2" sx={{ display: 'flex', alignItems: 'center', gap: 1, color: '#3b82f6', cursor: 'pointer' }} onClick={() => { navigator.clipboard.writeText(node.id); toast.success(t('nodePanel.copied')); }}>
                                        {node.id} <ContentCopyIcon sx={{ fontSize: 12 }} />
                                    </Typography>
                                </Box>
                            </Box>

                            <Box sx={{ display: 'flex', gap: 2 }}>
                                <MiniChart
                                    data={node.chart?.cpu || []}
                                    label={t('nodePanel.cpuUsage')}
                                    color={node.system?.cpu_percent && node.system.cpu_percent > 80 ? '#f43f5e' : '#3b82f6'}
                                    height={64}
                                />
                                <MiniChart
                                    data={node.chart?.mem || []}
                                    label={t('nodePanel.memUsage')}
                                    color={node.system?.mem_percent && node.system.mem_percent > 80 ? '#f43f5e' : '#10b981'}
                                    height={64}
                                />
                            </Box>
                        </Box>
                    </Box>
                    );
                })}
            </Box>

            <Dialog open={openDialog} onClose={() => setOpenDialog(false)} maxWidth="sm" fullWidth PaperProps={{ sx: { borderRadius: 3, backgroundImage: 'none', bgcolor: theme.palette.mode === 'dark' ? '#1e1e1e' : '#fff' } }}>
                <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1, fontWeight: 700 }}>
                    <SettingsIcon color="primary" /> {editNodeId ? t('nodePanel.editNode') : t('nodePanel.addNodeConfig')}
                </DialogTitle>
                <DialogContent>
                    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3, mt: 1 }}>
                        <Box>
                            <Typography variant="subtitle2" sx={{ mb: 1 }}>{t('nodePanel.remarkInfo')}</Typography>
                            <TextField fullWidth size="small" placeholder={t('nodePanel.remarkPlaceholder')} value={nodeName} onChange={e => setNodeName(e.target.value)} />
                        </Box>
                        <Box>
                            <Typography variant="subtitle2" sx={{ mb: 1 }}>{t('nodePanel.remoteAddress')}</Typography>
                            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
                                {t('nodePanel.remoteAddressHelp')}
                            </Typography>
                            <TextField fullWidth size="small" placeholder={t('nodePanel.remoteAddressPlaceholder')} value={nodeAddress} onChange={e => setNodeAddress(e.target.value)} />
                        </Box>
                        <Box>
                            <Typography variant="subtitle2" sx={{ mb: 1 }}>{t('nodePanel.apiKeyLabel')}</Typography>
                            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
                                {t('nodePanel.apiKeyHelp')}
                            </Typography>
                            <TextField fullWidth size="small" type="password" placeholder={t('nodePanel.apiKeyPlaceholder')} value={nodeApiKey} onChange={e => setNodeApiKey(e.target.value)} />
                        </Box>
                    </Box>
                </DialogContent>
                <DialogActions sx={{ p: 3, pt: 0 }}>
                    <Button onClick={() => setOpenDialog(false)} color="inherit" sx={{ borderRadius: 2 }}>{t('nodePanel.cancel')}</Button>
                    <Button variant="contained" onClick={handleSave} disabled={!nodeName || !nodeAddress} sx={{ borderRadius: 2, boxShadow: 'none' }}>{t('nodePanel.saveNode')}</Button>
                </DialogActions>
            </Dialog>

            {/* ============ Console Log Dialog ============ */}
            <Dialog open={logDialogOpen} onClose={handleCloseConsole} maxWidth="lg" fullWidth PaperProps={{ sx: { borderRadius: 3, backgroundImage: 'none', bgcolor: theme.palette.mode === 'dark' ? '#1a1a1a' : '#fff', height: '80vh' } }}>
                <DialogTitle sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', pb: 1 }}>
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
                        <TerminalIcon sx={{ color: '#3b82f6' }} />
                        <Box>
                            <Typography variant="h6" sx={{ fontWeight: 700, lineHeight: 1.2 }}>{t('nodePanel.consoleTitle')}</Typography>
                            <Typography variant="caption" color="text.secondary">{logNodeName} ({logNodeId})</Typography>
                        </Box>
                    </Box>
                    <IconButton size="small" onClick={handleCloseConsole}><CloseIcon /></IconButton>
                </DialogTitle>
                <DialogContent sx={{ display: 'flex', flexDirection: 'column', p: 0, overflow: 'hidden' }}>
                    {/* Toolbar */}
                    <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center', px: 3, py: 1.5, borderBottom: `1px solid ${theme.palette.divider}`, flexWrap: 'wrap' }}>
                        <TextField
                            size="small"
                            type="number"
                            value={logLines}
                            onChange={(e) => setLogLines(Math.max(50, Math.min(5000, parseInt(e.target.value) || 500)))}
                            sx={{ width: 100, '& .MuiOutlinedInput-root': { borderRadius: 2 } }}
                            label={t('nodePanel.logLines')}
                        />
                        <Button
                            size="small"
                            variant="outlined"
                            onClick={() => fetchLogContent(logNodeId, logLines)}
                            startIcon={<RefreshIcon />}
                            sx={{ borderRadius: 2, textTransform: 'none' }}
                        >
                            {t('nodePanel.logRefresh')}
                        </Button>
                        <Button
                            size="small"
                            variant={logAutoRefresh ? 'contained' : 'outlined'}
                            onClick={toggleAutoRefresh}
                            sx={{ borderRadius: 2, textTransform: 'none', ...(logAutoRefresh ? { bgcolor: '#10b981', '&:hover': { bgcolor: '#059669' } } : {}) }}
                        >
                            {logAutoRefresh ? t('nodePanel.autoRefreshOn') : t('nodePanel.autoRefreshOff')}
                        </Button>
                        <Box sx={{ flex: 1 }} />
                        {errorCount > 0 && (
                            <Chip icon={<ErrorOutlineIcon />} label={`${errorCount} errors`} size="small" color="error" variant="outlined" />
                        )}
                        {warnCount > 0 && (
                            <Chip icon={<WarningAmberIcon />} label={`${warnCount} warns`} size="small" color="warning" variant="outlined" />
                        )}
                    </Box>
                    {/* Log Content */}
                    <Box sx={{ flex: 1, overflow: 'auto', px: 2, py: 1, bgcolor: theme.palette.mode === 'dark' ? '#0d1117' : '#f8f9fa' }}>
                        {logLoading && !logContent ? (
                            <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100%' }}>
                                <CircularProgress size={32} />
                            </Box>
                        ) : !logContent ? (
                            <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100%' }}>
                                <Typography color="text.secondary">{t('nodePanel.noLogs')}</Typography>
                            </Box>
                        ) : (
                            <Box component="pre" sx={{
                                m: 0, p: 2, fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", Consolas, monospace',
                                fontSize: '0.78rem', lineHeight: 1.7, whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                                color: theme.palette.mode === 'dark' ? '#c9d1d9' : '#24292f',
                                '& .log-error': { color: '#f85149', fontWeight: 600 },
                                '& .log-warn': { color: '#d29922', fontWeight: 600 },
                            }}>
                                {logContent.split('\n').map((line, i) => {
                                    const isError = /error/i.test(line);
                                    const isWarn = !isError && /warn/i.test(line);
                                    return (
                                        <Box key={i} component="span" sx={{
                                            display: 'block',
                                            ...(isError ? { color: '#f85149', bgcolor: 'rgba(248,81,73,0.1)' } : {}),
                                            ...(isWarn ? { color: '#d29922', bgcolor: 'rgba(210,153,34,0.08)' } : {}),
                                            px: 1, borderRadius: 0.5,
                                        }}>
                                            <Box component="span" sx={{ color: theme.palette.mode === 'dark' ? '#484f58' : '#8b949e', mr: 1, userSelect: 'none', fontSize: '0.7rem' }}>
                                                {String(i + 1).padStart(4, ' ')}
                                            </Box>
                                            {line}
                                        </Box>
                                    );
                                })}
                                <div ref={logEndRef} />
                            </Box>
                        )}
                    </Box>
                </DialogContent>
            </Dialog>
        </Box>
    );
}
