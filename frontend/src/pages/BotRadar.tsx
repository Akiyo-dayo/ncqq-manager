/**
 * Bot 雷达 — 登记 Bot 框架的 OneBot v11 反向 WS 端点，探测在线状态，一键注入到 NapCat 实例
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import {
    Box, Typography, Paper, Grid, Button, TextField, IconButton,
    Chip, CircularProgress, Alert, Tooltip, Dialog, DialogTitle,
    DialogContent, DialogActions, Checkbox, Pagination, Collapse, useTheme,
} from '@mui/material';
import AddCircleOutlineIcon from '@mui/icons-material/AddCircleOutline';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import WifiTetheringIcon from '@mui/icons-material/WifiTethering';
import WifiTetheringOffIcon from '@mui/icons-material/WifiTetheringOff';
import RadarIcon from '@mui/icons-material/Radar';
import SettingsIcon from '@mui/icons-material/Settings';
import HelpOutlineIcon from '@mui/icons-material/HelpOutline';
import ExpandLessIcon from '@mui/icons-material/ExpandLess';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import { useTranslate } from '../i18n';
import {
    botRadarApi, containerApi,
    type Container, type RadarEndpoint,
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

const PAGE_SIZE = 8;
const GUIDE_STORAGE_KEY = 'botRadar.guideCollapsed';

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
                    value={alias} onChange={e => setAlias(e.target.value)} fullWidth size="small"
                    helperText={t('别名是注入和外部脚本调用的唯一标识，建议填写')} />
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
                            注入只是改写实例的 OneBot 配置，必须重启该实例后才会真正连上这个框架。
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
    containers: Container[];
    onProbe: (index: number) => void;
    onDelete: (index: number) => void;
    onEdit: (index: number, patch: { url: string; alias: string; token: string }) => void;
    onInjectNC: (index: number, containerNames: string[]) => Promise<void>;
}

function EndpointCard({
    entry, index, allAliases, containers,
    onProbe, onDelete, onEdit, onInjectNC,
}: EndpointCardProps) {
    const t = useTranslate();
    const theme = useTheme();
    const [editOpen, setEditOpen] = useState(false);
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
                    {isHandshakeRejected && (
                        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
                            端口通了但握手被拒，通常是 token 填错，不是网络不通。
                        </Typography>
                    )}
                </Box>

                {/* 卡片底部 footer */}
                <Box sx={{
                    display: 'flex', gap: 1, px: 2, pb: 2, pt: 0,
                }}>
                    <Button size="small" variant="contained" sx={{ flex: 1, fontSize: '0.75rem' }}
                        onClick={() => setNcOpen(true)}>
                        {t('botRadar.injectToNC')}
                    </Button>
                </Box>
            </Paper>

            <EditDialog open={editOpen} entry={entry} allAliases={allAliases}
                onClose={() => setEditOpen(false)}
                onSave={patch => onEdit(index, patch)} />
            <InjectNCDialog open={ncOpen} entry={entry} containers={containers}
                onClose={() => setNcOpen(false)}
                onConfirm={names => onInjectNC(index, names)} />
        </>
    );
}


// ─── 主页面 ────────────────────────────────────────────────────────────────────

export default function BotRadar() {
    const t = useTranslate();
    const toast = useToast();
    const theme = useTheme();

    const [endpoints, setEndpoints] = useState<EndpointEntry[]>([]);
    const [newAlias, setNewAlias] = useState('');
    const [newUrl, setNewUrl] = useState('');
    const [newToken, setNewToken] = useState('');
    const [containers, setContainers] = useState<Container[]>([]);
    // 引导卡片默认展开，折叠状态记在 localStorage —— 老用户不必每次关一遍
    const [guideOpen, setGuideOpen] = useState(() => localStorage.getItem(GUIDE_STORAGE_KEY) !== '1');
    const aliasInputRef = useRef<HTMLInputElement>(null);
    const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    // 初始化：加载持久化端点 + 容器列表
    useEffect(() => {
        botRadarApi.endpoints().then(res => {
            if (res.endpoints?.length) {
                setEndpoints(res.endpoints.map((ep: RadarEndpoint) => ({
                    url: ep.url, alias: ep.alias, token: ep.token,
                    online: null, latency_ms: null, probing: false,
                })));
            }
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
            botRadarApi.saveEndpoints(payload).catch(() => {});
        }, 1000);
        return () => { if (saveTimerRef.current) clearTimeout(saveTimerRef.current); };
    }, [endpoints]);

    const toggleGuide = () => {
        setGuideOpen(prev => {
            const next = !prev;
            localStorage.setItem(GUIDE_STORAGE_KEY, next ? '0' : '1');
            return next;
        });
    };

    // 添加端点
    const handleAdd = () => {
        const url = newUrl.trim();
        if (!isValidWsUrl(url)) { toast.error(t('botRadar.invalidUrl')); return; }
        if (endpoints.some(e => e.url === url)) { toast.warning(t('botRadar.alreadyExists')); return; }
        const alias = newAlias.trim();
        if (alias && endpoints.some(e => e.alias === alias)) { toast.warning(t('botRadar.aliasDuplicate')); return; }
        setEndpoints(prev => [...prev, { url, alias, online: null, latency_ms: null, probing: false, token: newToken.trim() }]);
        setNewUrl(''); setNewAlias(''); setNewToken('');
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
            const res = await botRadarApi.probeTarget(entry.url, entry.token);
            setEndpoints(prev => prev.map((e, i) => i === index
                ? { ...e, online: res.online, latency_ms: res.latency_ms ?? null, note: res.note, probing: false }
                : e));
        } catch {
            setEndpoints(prev => prev.map((e, i) => i === index ? { ...e, online: false, probing: false } : e));
        }
    }, [endpoints]);

    // 全部探测
    const handleProbeAll = () => Promise.all(endpoints.map((_, i) => handleProbe(i)));

    // 注入到多个 NCQQ 实例
    const handleInjectNC = useCallback(async (index: number, containerNames: string[]) => {
        const entry = endpoints[index];
        // 后端的注入接口按别名寻址，没有别名就没法调；顺带逼着用户把别名填上，
        // 外部脚本才有稳定的调用标识。
        if (!entry.alias) {
            toast.error(t('请先给这个端点设置别名（卡片右上角齿轮），注入接口按别名识别端点'));
            return;
        }
        let ok = 0; let fail = 0; let needsRestart = false;
        for (const containerName of containerNames) {
            try {
                const container = containers.find(c => c.name === containerName);
                const res = await botRadarApi.injectByAlias({
                    alias: entry.alias,
                    container_name: containerName,
                    uin: container?.uin,
                });
                if (res.success) {
                    ok++;
                    if (res.needs_restart) needsRestart = true;
                } else {
                    fail++;
                }
            } catch { fail++; }
        }
        if (fail === 0) toast.success(t('botRadar.injectNcSuccess').replace('{n}', String(ok)));
        else toast.warning(t('botRadar.partialSuccess').replace('{ok}', String(ok)).replace('{fail}', String(fail)));
        if (needsRestart) toast.warning(t('配置已写入，需要重启对应实例后才会生效'));
    }, [endpoints, containers, t, toast]);

    const cardSx = {
        bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
        backdropFilter: 'blur(16px) saturate(1.2)',
        WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
        border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
    };

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

            {/* 使用引导 */}
            <Paper elevation={0} sx={{ p: 2, mb: 3, borderRadius: 3, ...cardSx }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, cursor: 'pointer' }} onClick={toggleGuide}>
                    <HelpOutlineIcon sx={{ color: '#60a5fa' }} />
                    <Typography sx={{ flex: 1, fontWeight: 700 }}>这个页面怎么用？</Typography>
                    <IconButton size="small">
                        {guideOpen ? <ExpandLessIcon /> : <ExpandMoreIcon />}
                    </IconButton>
                </Box>
                <Collapse in={guideOpen}>
                    <Box sx={{ pt: 2, display: 'flex', flexDirection: 'column', gap: 2 }}>
                        <Typography variant="body2" color="text.secondary" sx={{ lineHeight: 1.8 }}>
                            这里用来登记你的 Bot 框架（AstrBot / NoneBot2 / Koishi 等）的 <b>OneBot v11 反向 WS 端点</b>，
                            随时探测它是否在线，并一键注入到任意 NapCat 实例 —— 省得每次手抄一遍 URL 和 token。
                        </Typography>

                        <Box>
                            <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>典型工作流（三步）</Typography>
                            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                                {[
                                    <>从你的 Bot 框架里找到 OneBot v11 <b>反向 WS 地址</b>，形如 <Box component="code" sx={{ fontFamily: 'monospace', fontSize: '0.8rem' }}>ws://1.2.3.4:8080/onebot/v11/ws</Box></>,
                                    <>在下面「添加目标端点」填 <b>别名 + URL +（可选）token</b>，点探测按钮确认在线</>,
                                    <>对着某个 NapCat 实例点「注入到 NCQQ」，<b>重启该实例</b>即可让它连上这个框架</>,
                                ].map((step, i) => (
                                    <Box key={i} sx={{ display: 'flex', gap: 1.5, alignItems: 'flex-start' }}>
                                        <Chip label={i + 1} size="small" sx={{ minWidth: 26, fontWeight: 700 }} />
                                        <Typography variant="body2" color="text.secondary" sx={{ lineHeight: 1.8 }}>{step}</Typography>
                                    </Box>
                                ))}
                            </Box>
                        </Box>

                        <Box>
                            <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>探测结果怎么读</Typography>
                            <Typography variant="body2" color="text.secondary" sx={{ lineHeight: 1.8 }}>
                                <b>在线</b> = 握手成功，可以直接用；
                                <b>在线但握手被拒</b> = 端口是通的，通常是 token 填错了，不是网络问题；
                                <b>离线</b> = 端口不通，检查框架有没有起来、地址端口对不对、防火墙有没有放行。
                            </Typography>
                        </Box>

                        <Box>
                            <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>别名是给自动化用的</Typography>
                            <Typography variant="body2" color="text.secondary" sx={{ lineHeight: 1.8 }}>
                                填了别名后，外部脚本可以跳过界面直接调用：
                            </Typography>
                            <Box component="pre" sx={{
                                mt: 1, mb: 0, p: 1.5, borderRadius: 2, overflowX: 'auto',
                                fontFamily: 'monospace', fontSize: '0.75rem',
                                bgcolor: theme.palette.mode === 'dark' ? 'rgba(0,0,0,0.35)' : 'rgba(0,0,0,0.05)',
                            }}>
{`POST /api/bot-radar/inject-by-alias
{"alias":"gscore","container_name":"miya"}`}
                            </Box>
                        </Box>
                    </Box>
                </Collapse>
            </Paper>

            {/* 工具栏 */}
            <Paper elevation={1} sx={{ p: 2, mb: 3, borderRadius: 3, ...cardSx }}>
                <Box sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap', alignItems: 'center' }}>
                    <TextField
                        size="small" label={t('botRadar.alias')}
                        placeholder={t('botRadar.aliasPlaceholder')}
                        inputRef={aliasInputRef}
                        value={newAlias} onChange={e => setNewAlias(e.target.value)}
                        sx={{ width: 160 }}
                    />
                    <TextField
                        size="small" label={t('botRadar.endpointUrl')}
                        placeholder={t('botRadar.urlPlaceholder')}
                        value={newUrl} onChange={e => setNewUrl(e.target.value)}
                        onKeyDown={e => e.key === 'Enter' && handleAdd()}
                        sx={{ flex: 1, minWidth: 260 }}
                    />
                    <TextField
                        size="small" label={t('botRadar.token')}
                        placeholder="optional"
                        value={newToken} onChange={e => setNewToken(e.target.value)}
                        onKeyDown={e => e.key === 'Enter' && handleAdd()}
                        sx={{ width: 180 }}
                    />
                    <Button variant="contained" startIcon={<AddCircleOutlineIcon />} onClick={handleAdd}>
                        {t('botRadar.addEndpoint')}
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
                <Paper elevation={0} sx={{ p: 4, borderRadius: 3, textAlign: 'center', ...cardSx }}>
                    <RadarIcon sx={{ fontSize: 48, color: 'text.disabled', mb: 1 }} />
                    <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>端点库还是空的</Typography>
                    <Typography variant="body2" color="text.secondary" sx={{ maxWidth: 560, mx: 'auto', lineHeight: 1.8 }}>
                        先去你的 Bot 框架（AstrBot / NoneBot2 / Koishi 等）里找到 OneBot v11 的反向 WS 地址，
                        形如 <Box component="code" sx={{ fontFamily: 'monospace', fontSize: '0.8rem' }}>ws://1.2.3.4:8080/onebot/v11/ws</Box>，
                        起个好记的别名登记进来。之后就能随时探测在线状态，并一键注入到任意 NapCat 实例。
                    </Typography>
                    <Button variant="contained" startIcon={<AddCircleOutlineIcon />} sx={{ mt: 2.5 }}
                        onClick={() => aliasInputRef.current?.focus()}>
                        添加第一个端点
                    </Button>
                </Paper>
            ) : (
                <Grid container spacing={2}>
                    {endpoints.map((entry, i) => (
                        <Grid item xs={12} sm={6} lg={4} key={entry.url + i}>
                            <EndpointCard
                                entry={entry} index={i}
                                allAliases={endpoints.map(e => e.alias)}
                                containers={containers}
                                onProbe={handleProbe}
                                onDelete={handleDelete}
                                onEdit={handleEdit}
                                onInjectNC={handleInjectNC}
                            />
                        </Grid>
                    ))}
                </Grid>
            )}
        </Box>
    );
}
