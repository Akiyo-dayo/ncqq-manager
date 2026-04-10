/**
 * Bot 后端页面 — NCQQ 卡片风格，管理对端 Bot 框架端点，弹窗编辑，多选注入
 */
import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
    Box, Typography, Paper, Grid, Button, TextField, IconButton,
    Chip, CircularProgress, Alert, Tooltip, Dialog, DialogTitle,
    DialogContent, DialogActions, Checkbox, Pagination, useTheme,
} from '@mui/material';
import AddCircleOutlineIcon from '@mui/icons-material/AddCircleOutline';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import WifiTetheringIcon from '@mui/icons-material/WifiTethering';
import WifiTetheringOffIcon from '@mui/icons-material/WifiTetheringOff';
import RadarIcon from '@mui/icons-material/Radar';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import SettingsIcon from '@mui/icons-material/Settings';
import { useTranslate } from '../i18n';
import {
    botshepherdApi, containerApi, instanceNetworkApi,
    type Container, type BSConnection, type RadarEndpoint,
} from '../services/api';
import { useToast } from '../components/Toast';

// ─── 类型 ──────────────────────────────────────────────────────────────────

interface EndpointEntry {
    url: string;
    alias: string;
    online: boolean | null;
    latency_ms: number | null;
    probing: boolean;
    note?: string;
    token: string;
}

function isValidWsUrl(url: string): boolean {
    return /^wss?:\/\/.+/.test(url.trim());
}

// ─── EditDialog：右上角编辑弹窗 ──────────────────────────────────────────────

interface EditDialogProps {
    open: boolean;
    entry: EndpointEntry;
    allAliases: string[];
    onClose: () => void;
    onSave: (patch: { url: string; alias: string; token: string }) => void;
}

function EditDialog({ open, entry, allAliases, onClose, onSave }: EditDialogProps) {
    const t = useTranslate();
    const toast = useToast();
    const [url, setUrl] = useState(entry.url);
    const [alias, setAlias] = useState(entry.alias);
    const [token, setToken] = useState(entry.token);

    useEffect(() => {
        if (open) { setUrl(entry.url); setAlias(entry.alias); setToken(entry.token); }
    }, [open, entry]);

    const handleSave = () => {
        const trimUrl = url.trim();
        if (!isValidWsUrl(trimUrl)) { toast.error(t('botRadar.invalidUrl')); return; }
        const trimAlias = alias.trim();
        if (trimAlias && trimAlias !== entry.alias &&
            allAliases.filter(a => a === trimAlias).length > 0) {
            toast.warning(t('botRadar.aliasDuplicate')); return;
        }
        onSave({ url: trimUrl, alias: trimAlias, token: token.trim() });
        onClose();
    };

    return (
        <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
            <DialogTitle sx={{ fontWeight: 700 }}>{t('botRadar.editEndpoint')}</DialogTitle>
            <DialogContent sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: '16px !important' }}>
                <TextField label="WebSocket URL" placeholder={t('botRadar.urlPlaceholder')}
                    value={url} onChange={e => setUrl(e.target.value)} fullWidth size="small"
                    inputProps={{ style: { fontFamily: 'monospace' } }} />
                <TextField label={t('botRadar.alias')} placeholder={t('botRadar.aliasPlaceholder')}
                    value={alias} onChange={e => setAlias(e.target.value)} fullWidth size="small" />
                <TextField label={t('botRadar.token')} placeholder="Bearer token / access_token"
                    value={token} onChange={e => setToken(e.target.value)} fullWidth size="small" />
            </DialogContent>
            <DialogActions>
                <Button onClick={onClose}>{t('botRadar.cancelText')}</Button>
                <Button variant="contained" onClick={handleSave}>保存 / Save</Button>
            </DialogActions>
        </Dialog>
    );
}

// ─── InjectBSDialog：注入到 BS 连接（多选 + 分页） ────────────────────────────

interface InjectBSDialogProps {
    open: boolean;
    entry: EndpointEntry;
    bsConnections: Record<string, BSConnection>;
    onClose: () => void;
    onConfirm: (connIds: string[]) => Promise<void>;
}

const PAGE_SIZE = 8;

function InjectBSDialog({ open, entry, bsConnections, onClose, onConfirm }: InjectBSDialogProps) {
    const t = useTranslate();
    const [search, setSearch] = useState('');
    const [selected, setSelected] = useState<string[]>([]);
    const [page, setPage] = useState(1);
    const [loading, setLoading] = useState(false);

    useEffect(() => { if (open) { setSelected([]); setSearch(''); setPage(1); } }, [open]);

    const allOptions = Object.entries(bsConnections).map(([id, c]) => ({
        id, label: `${c.name || id}  (${id})`,
    }));
    const filtered = allOptions.filter(o =>
        o.label.toLowerCase().includes(search.toLowerCase())
    );
    const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
    const pageItems = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

    const toggle = (id: string) => {
        setSelected(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
    };
    const toggleAll = () => {
        const pageIds = pageItems.map(o => o.id);
        const allChecked = pageIds.every(id => selected.includes(id));
        if (allChecked) setSelected(prev => prev.filter(id => !pageIds.includes(id)));
        else setSelected(prev => [...new Set([...prev, ...pageIds])]);
    };

    const handleConfirm = async () => {
        if (selected.length === 0) return;
        setLoading(true);
        await onConfirm(selected);
        setLoading(false);
        onClose();
    };

    const pageIds = pageItems.map(o => o.id);
    const allPageChecked = pageIds.length > 0 && pageIds.every(id => selected.includes(id));

    return (
        <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
            <DialogTitle sx={{ fontWeight: 700 }}>
                {t('botRadar.injectBSTitle')}
                <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                    {entry.alias || entry.url}
                </Typography>
            </DialogTitle>
            <DialogContent>
                {allOptions.length === 0 ? (
                    <Alert severity="warning" sx={{ mt: 1 }}>{t('botRadar.noBS')}</Alert>
                ) : (
                    <>
                        <TextField size="small" fullWidth placeholder={t('botRadar.searchPlaceholder')}
                            value={search} onChange={e => { setSearch(e.target.value); setPage(1); }}
                            sx={{ mb: 1.5 }} />
                        <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.5 }}>
                            <Checkbox size="small" checked={allPageChecked}
                                indeterminate={pageIds.some(id => selected.includes(id)) && !allPageChecked}
                                onChange={toggleAll} />
                            <Typography variant="caption" color="text.secondary">
                                {t('botRadar.selected').replace('{n}', String(selected.length))}
                            </Typography>
                        </Box>
                        {pageItems.map(o => (
                            <Box key={o.id} sx={{ display: 'flex', alignItems: 'center' }}>
                                <Checkbox size="small" checked={selected.includes(o.id)}
                                    onChange={() => toggle(o.id)} />
                                <Typography variant="body2" sx={{ fontSize: '0.82rem' }}>{o.label}</Typography>
                            </Box>
                        ))}
                        {pageCount > 1 && (
                            <Box sx={{ display: 'flex', justifyContent: 'center', mt: 1.5 }}>
                                <Pagination count={pageCount} page={page} size="small"
                                    onChange={(_, v) => setPage(v)} />
                            </Box>
                        )}
                    </>
                )}
            </DialogContent>
            <DialogActions>
                <Button onClick={onClose}>{t('botRadar.cancelText')}</Button>
                <Button variant="contained" disabled={selected.length === 0 || loading}
                    onClick={handleConfirm} startIcon={loading ? <CircularProgress size={16} /> : undefined}>
                    {t('botRadar.confirmInject')}
                </Button>
            </DialogActions>
        </Dialog>
    );
}



// ─── InjectNCDialog：注入到 NCQQ 实例（多选 + 分页） ────────────────────────────

interface InjectNCDialogProps {
    open: boolean;
    entry: EndpointEntry;
    containers: Container[];
    onClose: () => void;
    onConfirm: (containerNames: string[]) => Promise<void>;
}

function InjectNCDialog({ open, entry, containers, onClose, onConfirm }: InjectNCDialogProps) {
    const t = useTranslate();
    const [search, setSearch] = useState('');
    const [selected, setSelected] = useState<string[]>([]);
    const [page, setPage] = useState(1);
    const [loading, setLoading] = useState(false);

    useEffect(() => { if (open) { setSelected([]); setSearch(''); setPage(1); } }, [open]);

    const allOptions = containers
        .filter(c => c.uin && c.uin !== '未登录 / Not Logged In')
        .map(c => ({ name: c.name, label: `${c.name}  (${c.uin})` }));
    const filtered = allOptions.filter(o =>
        o.label.toLowerCase().includes(search.toLowerCase())
    );
    const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
    const pageItems = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

    const toggle = (name: string) => {
        setSelected(prev => prev.includes(name) ? prev.filter(x => x !== name) : [...prev, name]);
    };
    const toggleAll = () => {
        const pageNames = pageItems.map(o => o.name);
        const allChecked = pageNames.every(n => selected.includes(n));
        if (allChecked) setSelected(prev => prev.filter(n => !pageNames.includes(n)));
        else setSelected(prev => [...new Set([...prev, ...pageNames])]);
    };

    const handleConfirm = async () => {
        if (selected.length === 0) return;
        setLoading(true);
        await onConfirm(selected);
        setLoading(false);
        onClose();
    };

    const pageNames = pageItems.map(o => o.name);
    const allPageChecked = pageNames.length > 0 && pageNames.every(n => selected.includes(n));

    return (
        <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
            <DialogTitle sx={{ fontWeight: 700 }}>
                {t('botRadar.injectNCTitle')}
                <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                    {entry.alias || entry.url}
                </Typography>
            </DialogTitle>
            <DialogContent>
                {allOptions.length === 0 ? (
                    <Alert severity="warning" sx={{ mt: 1 }}>{t('botRadar.noNC')}</Alert>
                ) : (
                    <>
                        <TextField size="small" fullWidth placeholder={t('botRadar.searchPlaceholder')}
                            value={search} onChange={e => { setSearch(e.target.value); setPage(1); }}
                            sx={{ mb: 1.5 }} />
                        <Box sx={{ display: 'flex', alignItems: 'center', mb: 0.5 }}>
                            <Checkbox size="small" checked={allPageChecked}
                                indeterminate={pageNames.some(n => selected.includes(n)) && !allPageChecked}
                                onChange={toggleAll} />
                            <Typography variant="caption" color="text.secondary">
                                {t('botRadar.selected').replace('{n}', String(selected.length))}
                            </Typography>
                        </Box>
                        {pageItems.map(o => (
                            <Box key={o.name} sx={{ display: 'flex', alignItems: 'center' }}>
                                <Checkbox size="small" checked={selected.includes(o.name)}
                                    onChange={() => toggle(o.name)} />
                                <Typography variant="body2" sx={{ fontSize: '0.82rem' }}>{o.label}</Typography>
                            </Box>
                        ))}
                        {pageCount > 1 && (
                            <Box sx={{ display: 'flex', justifyContent: 'center', mt: 1.5 }}>
                                <Pagination count={pageCount} page={page} size="small"
                                    onChange={(_, v) => setPage(v)} />
                            </Box>
                        )}
                        <Alert severity="warning" sx={{ mt: 1.5, fontSize: '0.75rem' }}>
                            {t('botRadar.ncReloadHint')}
                        </Alert>
                    </>
                )}
            </DialogContent>
            <DialogActions>
                <Button onClick={onClose}>{t('botRadar.cancelText')}</Button>
                <Button variant="contained" disabled={selected.length === 0 || loading}
                    onClick={handleConfirm} startIcon={loading ? <CircularProgress size={16} /> : undefined}>
                    {t('botRadar.confirmInject')}
                </Button>
            </DialogActions>
        </Dialog>
    );
}


// ─── EndpointCard：NCQQ 卡片风格 ─────────────────────────────────────────────

interface EndpointCardProps {
    entry: EndpointEntry;
    index: number;
    allAliases: string[];
    bsConnections: Record<string, BSConnection>;
    containers: Container[];
    onProbe: (index: number) => void;
    onDelete: (index: number) => void;
    onEdit: (index: number, patch: { url: string; alias: string; token: string }) => void;
    onInjectBS: (index: number, connIds: string[]) => Promise<void>;
    onInjectNC: (index: number, containerNames: string[]) => Promise<void>;
}

function EndpointCard({
    entry, index, allAliases, bsConnections, containers,
    onProbe, onDelete, onEdit, onInjectBS, onInjectNC,
}: EndpointCardProps) {
    const t = useTranslate();
    const theme = useTheme();
    const [editOpen, setEditOpen] = useState(false);
    const [bsOpen, setBsOpen] = useState(false);
    const [ncOpen, setNcOpen] = useState(false);

    const isHandshakeRejected = entry.online === true && entry.note === 'handshake_rejected';
    const statusColor = entry.online === null ? '#9ca3af'
        : isHandshakeRejected ? '#f59e0b'
        : entry.online ? '#22c55e'
        : '#ef4444';
    const statusLabel = entry.online === null ? t('botRadar.unknown')
        : isHandshakeRejected ? t('botRadar.handshakeRejected')
        : entry.online ? t('botRadar.online')
        : t('botRadar.offline');
    const StatusIcon = (entry.online && !isHandshakeRejected) ? WifiTetheringIcon : WifiTetheringOffIcon;

    const cardBg = theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)';
    const cardBorder = theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)';

    return (
        <>
            <Paper elevation={0} sx={{
                borderRadius: 3, border: `1px solid ${cardBorder}`, overflow: 'hidden',
                background: cardBg,
                backdropFilter: 'blur(16px) saturate(1.2)',
                WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                display: 'flex', flexDirection: 'column',
            }}>
                {/* 卡片主体 */}
                <Box sx={{ p: 2, flex: 1 }}>
                    {/* 顶栏：昵称 + 编辑 + 删除 */}
                    <Box sx={{ display: 'flex', alignItems: 'center', mb: 1 }}>
                        <Typography sx={{
                            flex: 1, fontWeight: 700, fontSize: '0.95rem',
                            color: entry.alias ? 'text.primary' : 'text.disabled',
                        }}>
                            {entry.alias || t('botRadar.aliasPlaceholder')}
                        </Typography>
                        <Tooltip title={t('botRadar.editEndpoint')}>
                            <IconButton size="small" onClick={() => setEditOpen(true)} sx={{ opacity: 0.6 }}>
                                <SettingsIcon sx={{ fontSize: 18 }} />
                            </IconButton>
                        </Tooltip>
                        <Tooltip title={t('botRadar.deleteEndpoint')}>
                            <IconButton size="small" color="error" onClick={() => onDelete(index)} sx={{ opacity: 0.6 }}>
                                <DeleteOutlineIcon sx={{ fontSize: 18 }} />
                            </IconButton>
                        </Tooltip>
                    </Box>

                    {/* URL */}
                    <Typography sx={{
                        fontFamily: 'monospace', fontSize: '0.75rem',
                        color: 'text.secondary', wordBreak: 'break-all', mb: 1.5,
                    }}>
                        {entry.url}
                    </Typography>

                    {/* 状态行 */}
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                        <StatusIcon sx={{ color: statusColor, fontSize: 18 }} />
                        <Chip size="small" label={entry.probing ? t('botRadar.probing') : statusLabel}
                            sx={{ bgcolor: `${statusColor}22`, color: statusColor, fontWeight: 600, fontSize: '0.7rem' }} />
                        {entry.latency_ms !== null && (
                            <Chip size="small" label={`${entry.latency_ms}ms`} variant="outlined" sx={{ fontSize: '0.7rem' }} />
                        )}
                        <Tooltip title={t('botRadar.probe')}>
                            <span>
                                <IconButton size="small" onClick={() => onProbe(index)} disabled={entry.probing}>
                                    {entry.probing ? <CircularProgress size={14} /> : <RadarIcon sx={{ fontSize: 16 }} />}
                                </IconButton>
                            </span>
                        </Tooltip>
                    </Box>
                </Box>

                {/* 卡片底部 footer */}
                <Box sx={{
                    display: 'flex', gap: 1, px: 2, pb: 2, pt: 0,
                }}>
                    <Button size="small" variant="contained" sx={{ flex: 1, fontSize: '0.75rem' }}
                        onClick={() => setBsOpen(true)}>
                        {t('botRadar.injectToBS')}
                    </Button>
                    <Button size="small" variant="outlined" sx={{ flex: 1, fontSize: '0.75rem' }}
                        onClick={() => setNcOpen(true)}>
                        {t('botRadar.injectToNC')}
                    </Button>
                </Box>
            </Paper>

            <EditDialog open={editOpen} entry={entry} allAliases={allAliases}
                onClose={() => setEditOpen(false)}
                onSave={patch => onEdit(index, patch)} />
            <InjectBSDialog open={bsOpen} entry={entry} bsConnections={bsConnections}
                onClose={() => setBsOpen(false)}
                onConfirm={connIds => onInjectBS(index, connIds)} />
            <InjectNCDialog open={ncOpen} entry={entry} containers={containers}
                onClose={() => setNcOpen(false)}
                onConfirm={names => onInjectNC(index, names)} />
        </>
    );
}


// ─── 主页面 ────────────────────────────────────────────────────────────────────

export default function BotBackend() {
    const t = useTranslate();
    const toast = useToast();
    const theme = useTheme();

    const [endpoints, setEndpoints] = useState<EndpointEntry[]>([]);
    const [newUrl, setNewUrl] = useState('');
    const [bsConnections, setBsConnections] = useState<Record<string, BSConnection>>({});
    const [containers, setContainers] = useState<Container[]>([]);
    const [collectingBS, setCollectingBS] = useState(false);
    const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    // 初始化：加载持久化端点 + BS 连接 + 容器列表
    useEffect(() => {
        botshepherdApi.radarEndpoints().then(res => {
            if (res.endpoints?.length) {
                setEndpoints(res.endpoints.map((ep: RadarEndpoint) => ({
                    url: ep.url, alias: ep.alias, token: ep.token,
                    online: null, latency_ms: null, probing: false,
                })));
            }
        }).catch(() => {});
        botshepherdApi.connections().then(res => {
            if (res.connections) setBsConnections(res.connections);
        }).catch(() => {});
        containerApi.list().then(res => {
            setContainers(res.containers || []);
        }).catch(() => {});
    }, []);

    // 自动保存 debounce 1s
    useEffect(() => {
        if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
        saveTimerRef.current = setTimeout(() => {
            const payload: RadarEndpoint[] = endpoints.map(e => ({
                alias: e.alias, url: e.url, token: e.token,
            }));
            botshepherdApi.saveRadarEndpoints(payload).catch(() => {});
        }, 1000);
        return () => { if (saveTimerRef.current) clearTimeout(saveTimerRef.current); };
    }, [endpoints]);

    // 添加端点
    const handleAdd = () => {
        const url = newUrl.trim();
        if (!isValidWsUrl(url)) { toast.error(t('botRadar.invalidUrl')); return; }
        if (endpoints.some(e => e.url === url)) { toast.warning(t('botRadar.alreadyExists')); return; }
        setEndpoints(prev => [...prev, { url, alias: '', online: null, latency_ms: null, probing: false, token: '' }]);
        setNewUrl('');
    };

    // 编辑端点（弹窗保存）
    const handleEdit = useCallback((index: number, patch: { url: string; alias: string; token: string }) => {
        setEndpoints(prev => prev.map((e, i) => i === index ? { ...e, ...patch } : e));
    }, []);

    // 删除端点
    const handleDelete = (index: number) => {
        setEndpoints(prev => prev.filter((_, i) => i !== index));
    };

    // 探测单个
    const handleProbe = useCallback(async (index: number) => {
        setEndpoints(prev => prev.map((e, i) => i === index ? { ...e, probing: true } : e));
        try {
            const entry = endpoints[index];
            const res = await botshepherdApi.probeTarget(entry.url, entry.token);
            setEndpoints(prev => prev.map((e, i) => i === index
                ? { ...e, online: res.online, latency_ms: res.latency_ms ?? null, note: res.note, probing: false }
                : e));
        } catch {
            setEndpoints(prev => prev.map((e, i) => i === index ? { ...e, online: false, probing: false } : e));
        }
    }, [endpoints]);

    // 全部探测
    const handleProbeAll = () => Promise.all(endpoints.map((_, i) => handleProbe(i)));

    // 从 BS 自动收集 target_endpoints
    const handleAutoCollect = async () => {
        setCollectingBS(true);
        try {
            const res = await botshepherdApi.connections();
            const conns = res.connections || {};
            if (res.connections) setBsConnections(res.connections);
            const urls = new Set<string>();
            Object.values(conns).forEach(c => (c.target_endpoints || []).forEach(u => urls.add(u)));
            let added = 0;
            urls.forEach(url => {
                if (!endpoints.some(e => e.url === url) && isValidWsUrl(url)) {
                    setEndpoints(prev => [...prev, { url, alias: '', online: null, latency_ms: null, probing: false, token: '' }]);
                    added++;
                }
            });
            toast.success(t('botRadar.autoCollectDone').replace('{n}', String(added)));
        } catch {
            toast.error(t('botRadar.collectFailed'));
        } finally {
            setCollectingBS(false);
        }
    };

    // 注入到多个 BS 连接（先读后合并再写）
    const handleInjectBS = useCallback(async (index: number, connIds: string[]) => {
        const url = endpoints[index].url;
        let ok = 0; let fail = 0;
        for (const connId of connIds) {
            try {
                const res = await botshepherdApi.connections();
                const conn = (res.connections || {})[connId];
                if (!conn) { fail++; continue; }
                const targets: string[] = conn.target_endpoints || [];
                if (targets.includes(url)) { ok++; continue; }
                await botshepherdApi.updateConnection(connId, { ...conn, target_endpoints: [...targets, url] });
                ok++;
            } catch { fail++; }
        }
        if (fail === 0) toast.success(t('botRadar.injectBsSuccess').replace('{n}', String(ok)));
        else toast.warning(t('botRadar.partialSuccess').replace('{ok}', String(ok)).replace('{fail}', String(fail)));
    }, [endpoints, t, toast]);

    // 注入到多个 NCQQ 实例
    const handleInjectNC = useCallback(async (index: number, containerNames: string[]) => {
        const url = endpoints[index].url;
        const token = endpoints[index].token;
        let ok = 0; let fail = 0;
        for (const containerName of containerNames) {
            try {
                const container = containers.find(c => c.name === containerName);
                const uin = container?.uin || 'default';
                let existingClients: Record<string, unknown>[] = [];
                try {
                    const cfgRes = await containerApi.getConfig(containerName, `config/onebot11_${uin}.json`, 'local');
                    if (cfgRes.status === 'ok' && cfgRes.content) {
                        const parsed = JSON.parse(cfgRes.content);
                        const wsc = parsed?.network?.websocketClients;
                        if (Array.isArray(wsc)) existingClients = wsc;
                    }
                } catch { /* 从空开始 */ }
                if (existingClients.some(c => (c as { url?: string }).url === url)) { ok++; continue; }
                const alias = endpoints[index].alias;
                const clientName = alias ? alias : url;
                const newClient = {
                    name: clientName, enable: true, url,
                    reportSelfMessage: false, messagePostFormat: 'array',
                    token: token || '', debug: false,
                    heartInterval: 30000, reconnectInterval: 30000,
                };
                await instanceNetworkApi.injectNetworkConfig(containerName, {
                    uin, network: { websocketClients: [...existingClients, newClient] as never },
                });
                ok++;
            } catch { fail++; }
        }
        if (fail === 0) toast.success(t('botRadar.injectNcSuccess').replace('{n}', String(ok)));
        else toast.warning(t('botRadar.partialSuccess').replace('{ok}', String(ok)).replace('{fail}', String(fail)));
    }, [endpoints, containers, t, toast]);


    return (
        <Box sx={{ p: 3, maxWidth: 1200, mx: 'auto' }}>
            {/* 页头 */}
            <Box sx={{ mb: 3 }}>
                <Typography variant="h5" sx={{ fontWeight: 700, display: 'flex', alignItems: 'center', gap: 1 }}>
                    <RadarIcon sx={{ color: '#60a5fa' }} />
                    {t('botRadar.title')}
                </Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                    {t('botRadar.subtitle')}
                </Typography>
            </Box>

            {/* 工具栏 */}
            <Paper elevation={1} sx={{ p: 2, mb: 3, borderRadius: 3,
                bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                backdropFilter: 'blur(16px) saturate(1.2)',
                WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}` }}>
                <Box sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap', alignItems: 'center' }}>
                    <TextField
                        size="small" label={t('botRadar.endpointUrl')}
                        placeholder={t('botRadar.urlPlaceholder')}
                        value={newUrl} onChange={e => setNewUrl(e.target.value)}
                        onKeyDown={e => e.key === 'Enter' && handleAdd()}
                        sx={{ flex: 1, minWidth: 260 }}
                    />
                    <Button variant="contained" startIcon={<AddCircleOutlineIcon />} onClick={handleAdd}>
                        {t('botRadar.addEndpoint')}
                    </Button>
                    <Button variant="outlined" startIcon={<AutoAwesomeIcon />}
                        onClick={handleAutoCollect} disabled={collectingBS}>
                        {collectingBS ? <CircularProgress size={16} sx={{ mr: 1 }} /> : null}
                        {t('botRadar.autoCollect')}
                    </Button>
                    {endpoints.length > 0 && (
                        <Button variant="outlined" color="secondary" startIcon={<RadarIcon />}
                            onClick={handleProbeAll}>
                            {t('botRadar.probeAll')}
                        </Button>
                    )}
                </Box>
            </Paper>

            {/* 端点卡片列表 */}
            {endpoints.length === 0 ? (
                <Alert severity="info" sx={{ borderRadius: 3 }}>{t('botRadar.noEndpoints')}</Alert>
            ) : (
                <Grid container spacing={2}>
                    {endpoints.map((entry, i) => (
                        <Grid item xs={12} sm={6} lg={4} key={entry.url + i}>
                            <EndpointCard
                                entry={entry} index={i}
                                allAliases={endpoints.map(e => e.alias)}
                                bsConnections={bsConnections}
                                containers={containers}
                                onProbe={handleProbe}
                                onDelete={handleDelete}
                                onEdit={handleEdit}
                                onInjectBS={handleInjectBS}
                                onInjectNC={handleInjectNC}
                            />
                        </Grid>
                    ))}
                </Grid>
            )}
        </Box>
    );
}
