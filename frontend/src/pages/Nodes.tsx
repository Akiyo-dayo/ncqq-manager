import { useState, useEffect, useRef, useCallback } from 'react';
import { Box, Typography, Button, TextField, Skeleton, IconButton, useTheme, Dialog, DialogTitle, DialogContent, DialogActions, CircularProgress, Chip, Alert, AlertTitle, Switch, FormControlLabel } from '@mui/material';
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
import NetworkCheckIcon from '@mui/icons-material/NetworkCheck';
import { nodeApi, type Node, type NodeProbeResult } from '../services/api';
import { useTranslate } from '../i18n';
import { useToast } from '../components/Toast';
import MiniChart from '../components/MiniChart';

/**
 * 把后端分类过的 error_kind 翻译成「下一步该干什么」。
 *
 * 光说“连接失败”用户只会重试第二遍，真正卡住的是不知道该改地址、改密钥
 * 还是去开防火墙，所以每一类都必须给出可执行的动作。
 */
const PROBE_HINTS: Record<string, string> = {
    unauthorized: '集群密钥不匹配。请到对方面板的「集群设置 → 本机集群密钥」复制正确的密钥。',
    no_key: '没有填集群密钥。请到对方面板的「集群设置 → 本机集群密钥」复制后填到下面。',
    refused: '对方端口未监听或被防火墙拦截。确认对方面板已启动、端口正确、安全组/防火墙已放行。',
    dns: '域名解析失败，请检查地址拼写。',
    tls: '证书校验失败。自签证书请勾选下面的「跳过证书校验」。',
    timeout: '连接超时，检查网络连通性（跨公网时确认没有被云安全组或运营商拦截）。',
    not_a_node: '该地址有服务在响应，但不是 NapCat 面板。请确认填的是面板端口，而不是 NapCat 实例或反代的端口。',
    invalid_address: '地址格式不合法。请填「IP:端口」或「https://域名」。',
};

function probeHint(kind?: string): string {
    if (!kind) return '';
    if (PROBE_HINTS[kind]) return PROBE_HINTS[kind];
    if (kind.startsWith('http_')) {
        return `对方返回了 HTTP ${kind.slice(5)}，不是预期的握手响应。确认地址指向的是 NapCat 面板本身，而不是反向代理下的其它路径。`;
    }
    return '';
}

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
    const [nodeInsecureTls, setNodeInsecureTls] = useState(false);
    const [probing, setProbing] = useState(false);
    const [probeResult, setProbeResult] = useState<NodeProbeResult | null>(null);
    const [saving, setSaving] = useState(false);
    // 保存被后端探测拦下（422）后的原因，非空时展示「仍然保存」
    const [saveProbe, setSaveProbe] = useState<NodeProbeResult | null>(null);

    // 删除确认
    const [deleteTarget, setDeleteTarget] = useState<Node | null>(null);
    const [deleting, setDeleting] = useState(false);

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

    const isLocalEdit = editNodeId === 'local';
    const isHttps = /^https:\/\//i.test(nodeAddress.trim());

    /** force=true 走 refresh 参数强制跳过后端 10 秒缓存 —— 否则用户连点刷新看不到任何变化 */
    const fetchNodes = useCallback(async (force = false) => {
        setRemoteLoading(true);
        if (!force) {
            // 第一阶段：quick 模式 — 本地节点完整，远程节点骨架（<50ms）
            setLoading(true);
            try {
                const data = await nodeApi.list(true);
                setNodes(data.nodes || []);
            } catch (e) {
                console.error(e);
            } finally {
                setLoading(false);
            }
        }
        // 第二阶段：异步获取远程节点完整状态（含健康检查）
        try {
            const full = await nodeApi.list(false, force);
            setNodes(full.nodes || []);
        } catch { /* 远程状态获取失败不影响页面 */ }
        setRemoteLoading(false);
    }, []);

    useEffect(() => {
        fetchNodes();
    }, [fetchNodes]);

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

    const resetForm = () => {
        setProbeResult(null);
        setSaveProbe(null);
        setProbing(false);
    };

    const handleOpenAdd = () => {
        setEditNodeId(null);
        setNodeName('');
        setNodeAddress('127.0.0.1:8000');
        setNodeApiKey('');
        setNodeInsecureTls(false);
        resetForm();
        setOpenDialog(true);
    };

    const handleOpenEdit = (node: Node) => {
        setEditNodeId(node.id);
        setNodeName(node.name);
        setNodeAddress(node.address);
        setNodeApiKey(''); // 留空 = 不修改密钥
        setNodeInsecureTls(Boolean(node.insecure_tls));
        resetForm();
        setOpenDialog(true);
    };

    const handleConfirmDelete = async () => {
        if (!deleteTarget) return;
        setDeleting(true);
        try {
            const res = await nodeApi.delete(deleteTarget.id);
            const cleared = res.instances_cleared ?? 0;
            toast.success(`${deleteTarget.name} 已删除 ✓ ${cleared > 0 ? `（清理了 ${cleared} 个实例记录）` : ''}`);
            setDeleteTarget(null);
            fetchNodes(true);
        } catch (e) {
            toast.error(String(e instanceof Error ? e.message : e));
        } finally {
            setDeleting(false);
        }
    };

    const handleProbe = async () => {
        setProbing(true);
        setProbeResult(null);
        setSaveProbe(null);
        try {
            const res = await nodeApi.probe(nodeAddress.trim(), nodeApiKey, nodeInsecureTls);
            setProbeResult(res.probe);
        } catch (e) {
            setProbeResult({ ok: false, detail: String(e instanceof Error ? e.message : e) });
        } finally {
            setProbing(false);
        }
    };

    const handleSave = async () => {
        setSaving(true);
        setSaveProbe(null);
        try {
            if (editNodeId) {
                await nodeApi.edit(editNodeId, nodeName, nodeAddress.trim(), nodeApiKey, nodeInsecureTls);
                toast.success(`${nodeName} 已保存 ✓`);
            } else {
                const res = await nodeApi.add(nodeName, nodeAddress.trim(), nodeApiKey, { insecureTls: nodeInsecureTls });
                toast.success(res.revived ? `${nodeName} 已恢复（复用原节点 ID）✓` : `${nodeName} 已添加 ✓`);
            }
            setOpenDialog(false);
            fetchNodes(true);
        } catch (e) {
            const msg = String(e instanceof Error ? e.message : e);
            // 422 的 body 里带着 probe，但通用请求封装只把它压成一句 "HTTP 422"。
            // 重新探测一次拿回分类原因，用户才知道该改地址还是改密钥。
            if (!editNodeId) {
                try {
                    const res = await nodeApi.probe(nodeAddress.trim(), nodeApiKey, nodeInsecureTls);
                    if (!res.probe.ok) {
                        setSaveProbe({ ...res.probe, detail: res.probe.detail || msg });
                        return;
                    }
                } catch { /* 探测本身也挂了就退回原始错误 */ }
            }
            toast.error(msg);
        } finally {
            setSaving(false);
        }
    };

    const handleForceSave = async () => {
        setSaving(true);
        try {
            await nodeApi.add(nodeName, nodeAddress.trim(), nodeApiKey, { insecureTls: nodeInsecureTls, force: true });
            toast.warning(`${nodeName} 已保存，但当前不可达，恢复连通后会自动上线`);
            setOpenDialog(false);
            setSaveProbe(null);
            fetchNodes(true);
        } catch (e) {
            toast.error(String(e instanceof Error ? e.message : e));
        } finally {
            setSaving(false);
        }
    };

    /** 探测失败面板 —— 添加对话框内的探测结果和 422 拦截复用同一块展示 */
    const renderProbeFailure = (probe: NodeProbeResult) => {
        const hint = probeHint(probe.error_kind);
        return (
            <Alert severity="error" sx={{ borderRadius: 2 }}>
                <AlertTitle sx={{ fontWeight: 700 }}>连接失败</AlertTitle>
                {probe.detail && (
                    <Typography variant="body2" sx={{ mb: hint ? 1 : 0 }}>{probe.detail}</Typography>
                )}
                {hint && <Typography variant="body2" sx={{ fontWeight: 600 }}>{hint}</Typography>}
            </Alert>
        );
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
                        <Button variant="outlined" color="inherit" onClick={() => fetchNodes(true)} disabled={remoteLoading} startIcon={remoteLoading ? <CircularProgress size={16} color="inherit" /> : <RefreshIcon />} sx={{ borderRadius: 2, height: 38, borderColor: theme.palette.divider, bgcolor: theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.02)' : '#fff' }}>
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
                    const isOnline = node.status === 'online';
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
                                <Typography variant="h6" sx={{ fontWeight: 700, display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                                    <DesktopWindowsIcon fontSize="small" color="action" /> {node.name}
                                    {node.id === 'local' && (
                                        <Box component="span" sx={{ fontSize: '0.65rem', bgcolor: '#3b82f6', color: '#fff', px: 0.8, py: 0.2, borderRadius: 1, ml: 1 }}>
                                            LOCAL
                                        </Box>
                                    )}
                                    {node.degraded && (
                                        <Chip size="small" color="warning" variant="outlined" icon={<WarningAmberIcon />}
                                            label="连续失败已降频重试" sx={{ fontSize: '0.65rem', height: 22 }} />
                                    )}
                                </Typography>
                                <Box sx={{ display: 'flex', gap: 0.5 }}>
                                    <IconButton size="small" title={t('nodePanel.console')} onClick={() => handleOpenConsole(node)}><TerminalIcon fontSize="small" /></IconButton>
                                    <IconButton size="small" title={t('nodePanel.nodeSettings')} onClick={() => handleOpenEdit(node)}><SettingsIcon fontSize="small" /></IconButton>
                                    {node.id !== 'local' && (
                                        <IconButton size="small" title={t('nodePanel.deleteNode')} onClick={() => setDeleteTarget(node)} color="error"><DeleteOutlineIcon fontSize="small" /></IconButton>
                                    )}
                                </Box>
                            </Box>

                            {/* 离线原因：后端已经翻成可读中文，直接显示，省得用户去翻日志 */}
                            {!isOnline && !isRemoteLoading && node.last_error && (
                                <Alert severity="error" icon={<ErrorOutlineIcon fontSize="small" />} sx={{ borderRadius: 2, mb: 2, py: 0.5, fontSize: '0.8rem' }}>
                                    <Box>{node.last_error}</Box>
                                    {probeHint(node.last_error_kind) && (
                                        <Box sx={{ mt: 0.5, fontWeight: 600 }}>{probeHint(node.last_error_kind)}</Box>
                                    )}
                                </Alert>
                            )}

                            <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 2, mb: 3 }}>
                                <Box>
                                    <Typography variant="caption" color="text.secondary" display="block">{t('nodePanel.address')}</Typography>
                                    <Typography variant="body2">{node.address}</Typography>
                                </Box>
                                {node.id !== 'local' && (
                                    <Box>
                                        <Typography variant="caption" color="text.secondary" display="block">集群密钥</Typography>
                                        <Typography variant="body2" sx={{ color: node.has_key ? '#10b981' : '#f59e0b' }}>
                                            {node.has_key ? '已配置' : '未配置'}
                                        </Typography>
                                    </Box>
                                )}
                                <Box>
                                    <Typography variant="caption" color="text.secondary" display="block">{t('nodePanel.nodeStatus')}</Typography>
                                    <Typography variant="body2" sx={{ color: isOnline ? '#10b981' : '#f43f5e', display: 'flex', alignItems: 'center', gap: 0.5 }}>
                                        <Box sx={{ width: 6, height: 6, borderRadius: '50%', bgcolor: isOnline ? '#10b981' : '#f43f5e' }} />
                                        {isOnline ? t('admin.online') : t('admin.offline')}
                                    </Typography>
                                </Box>
                                <Box>
                                    <Typography variant="caption" color="text.secondary" display="block">{t('nodePanel.directConnect')}</Typography>
                                    <Typography variant="body2" sx={{ color: isOnline ? '#10b981' : '#f43f5e', display: 'flex', alignItems: 'center', gap: 0.5 }}>
                                        <Box sx={{ width: 6, height: 6, borderRadius: '50%', bgcolor: isOnline ? '#10b981' : '#f43f5e' }} />
                                        {isOnline ? t('nodePanel.reachable') : t('nodePanel.unreachable')}
                                    </Typography>
                                </Box>
                                <Box>
                                    <Typography variant="caption" color="text.secondary" display="block">{t('nodePanel.latency')}</Typography>
                                    <Typography variant="body2" sx={{ color: (node.ping ?? 0) < 100 ? '#10b981' : ((node.ping ?? 0) < 300 ? '#f59e0b' : '#f43f5e') }}>
                                        {isOnline ? `${node.ping ?? 0}ms` : '-'}
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
                        {isLocalEdit && (
                            <Alert severity="info" sx={{ borderRadius: 2 }}>
                                本机节点只能改备注名。地址与集群密钥由本机自身配置决定，密钥请到「集群设置 → 本机集群密钥」管理。
                            </Alert>
                        )}
                        <Box>
                            <Typography variant="subtitle2" sx={{ mb: 1 }}>{t('nodePanel.remarkInfo')}</Typography>
                            <TextField fullWidth size="small" placeholder={t('nodePanel.remarkPlaceholder')} value={nodeName} onChange={e => setNodeName(e.target.value)} />
                        </Box>
                        <Box>
                            <Typography variant="subtitle2" sx={{ mb: 1 }}>{t('nodePanel.remoteAddress')}</Typography>
                            <TextField
                                fullWidth size="small"
                                placeholder="127.0.0.1:8000 或 https://panel.example.com"
                                value={nodeAddress}
                                disabled={isLocalEdit}
                                onChange={e => { setNodeAddress(e.target.value); setProbeResult(null); setSaveProbe(null); }}
                                helperText={isLocalEdit
                                    ? '本机节点地址不可修改'
                                    : '格式：IP:端口 或 https://域名（可带端口）。不写协议时默认按 http 处理；必须是浏览器能直接访问的外网地址。'}
                            />
                        </Box>
                        <Box>
                            <Typography variant="subtitle2" sx={{ mb: 1 }}>{t('nodePanel.apiKeyLabel')}</Typography>
                            <TextField
                                fullWidth size="small" type="password"
                                placeholder={t('nodePanel.apiKeyPlaceholder')}
                                value={nodeApiKey}
                                disabled={isLocalEdit}
                                onChange={e => { setNodeApiKey(e.target.value); setProbeResult(null); setSaveProbe(null); }}
                                helperText={isLocalEdit
                                    ? '本机节点无需填写密钥'
                                    : (editNodeId
                                        ? '留空表示不修改现有密钥。密钥在对方面板的「集群设置 → 本机集群密钥」里复制。'
                                        : '填对方面板「集群设置 → 本机集群密钥」里的那串密钥。')}
                            />
                        </Box>
                        {!isLocalEdit && (
                            <Box>
                                <FormControlLabel
                                    control={<Switch checked={nodeInsecureTls} disabled={!isHttps} onChange={e => { setNodeInsecureTls(e.target.checked); setProbeResult(null); }} />}
                                    label="跳过证书校验"
                                />
                                <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                                    {isHttps ? '对方使用自签证书时才需要打开。' : '仅 https:// 地址需要，当前地址不走 TLS。'}
                                </Typography>
                            </Box>
                        )}

                        {probeResult?.ok && (
                            <Alert severity="success" sx={{ borderRadius: 2 }}>
                                <AlertTitle sx={{ fontWeight: 700 }}>连接成功</AlertTitle>
                                延迟 {probeResult.ping_ms ?? '-'}ms
                                {probeResult.remote_app_version ? ` · 对方版本 v${probeResult.remote_app_version}` : ''}
                                {probeResult.remote_instances ? ` · 对方实例 ${probeResult.remote_instances.running}/${probeResult.remote_instances.total} 运行中` : ''}
                            </Alert>
                        )}
                        {probeResult && !probeResult.ok && renderProbeFailure(probeResult)}

                        {saveProbe && (
                            <Box>
                                {renderProbeFailure(saveProbe)}
                                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
                                    节点没有通过握手，默认不会保存。如果只是对方暂时没启动，可以选择「仍然保存」，恢复连通后会自动上线。
                                </Typography>
                            </Box>
                        )}
                    </Box>
                </DialogContent>
                <DialogActions sx={{ p: 3, pt: 0, gap: 1 }}>
                    {!isLocalEdit && (
                        <Button
                            onClick={handleProbe}
                            disabled={!nodeAddress || probing}
                            startIcon={probing ? <CircularProgress size={16} /> : <NetworkCheckIcon />}
                            sx={{ borderRadius: 2, mr: 'auto' }}
                        >
                            测试连接
                        </Button>
                    )}
                    <Button onClick={() => setOpenDialog(false)} color="inherit" sx={{ borderRadius: 2 }}>{t('nodePanel.cancel')}</Button>
                    {saveProbe && (
                        <Button onClick={handleForceSave} color="warning" disabled={saving} sx={{ borderRadius: 2 }}>
                            仍然保存
                        </Button>
                    )}
                    <Button variant="contained" onClick={handleSave} disabled={!nodeName || !nodeAddress || saving} sx={{ borderRadius: 2, boxShadow: 'none' }}>
                        {t('nodePanel.saveNode')}
                    </Button>
                </DialogActions>
            </Dialog>

            {/* ============ 删除确认 ============ */}
            <Dialog open={Boolean(deleteTarget)} onClose={() => setDeleteTarget(null)} maxWidth="xs" fullWidth PaperProps={{ sx: { borderRadius: 3, backgroundImage: 'none', bgcolor: theme.palette.mode === 'dark' ? '#1e1e1e' : '#fff' } }}>
                <DialogTitle sx={{ fontWeight: 700 }}>删除节点 {deleteTarget?.name}</DialogTitle>
                <DialogContent>
                    <Alert severity="info" sx={{ borderRadius: 2, mb: 2 }}>
                        这是软删除：节点记录会保留，之后重新添加同一地址会复用原来的节点 ID，已经分配给用户的实例授权不会失效。
                    </Alert>
                    <Typography variant="body2" color="text.secondary">
                        删除后该节点上的实例会从列表中移除，节点重新加回来时会自动重新同步。
                    </Typography>
                </DialogContent>
                <DialogActions sx={{ p: 3, pt: 0 }}>
                    <Button onClick={() => setDeleteTarget(null)} color="inherit" sx={{ borderRadius: 2 }}>{t('nodePanel.cancel')}</Button>
                    <Button onClick={handleConfirmDelete} color="error" variant="contained" disabled={deleting} sx={{ borderRadius: 2, boxShadow: 'none' }}>
                        {t('admin.deleteText')}
                    </Button>
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
