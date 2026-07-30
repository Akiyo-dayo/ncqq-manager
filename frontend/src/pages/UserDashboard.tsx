import { useEffect, useState, useContext, useRef } from 'react';
import {
    Box, Typography, CircularProgress,
    Button, IconButton, useTheme, Skeleton, Pagination,
    TextField, InputAdornment, Dialog, DialogContent
} from '@mui/material';
import { useNavigate } from 'react-router-dom';
import RefreshIcon from '@mui/icons-material/Refresh';
import CloudOffIcon from '@mui/icons-material/CloudOff';
import AdminPanelSettingsIcon from '@mui/icons-material/AdminPanelSettings';
import Brightness4Icon from '@mui/icons-material/Brightness4';
import Brightness7Icon from '@mui/icons-material/Brightness7';
import TranslateIcon from '@mui/icons-material/Translate';
import SearchIcon from '@mui/icons-material/Search';
import { ThemeModeContext, LanguageContext } from '../App';
import { useAuth } from '../contexts/AuthContext';
import { useTranslate } from '../i18n';
import { publicApi, type Container, type QRResponse } from '../services/api';
import { usePublicWebSocket } from '../hooks/usePublicWebSocket';
import LazyQRImage from '../components/LazyQRImage';

const ACTIVE_ACTION_PHASES = new Set(['queued', 'running']);
// superseded（被更新的操作取代）和 unknown（监控自身异常）后端都会回落到真实 Docker 状态，
// 当成错误徽标展示只会误导人，所以和 succeeded 一样静默。
const SILENT_ACTION_PHASES = new Set(['succeeded', 'superseded', 'unknown']);
const actionVerb = (action?: string) => action === 'start' ? '启动' : action === 'stop' ? '停止' : action === 'restart' ? '重启' : '操作';
const actionPhaseLabel = (container: Container) => {
    const action = container.action;
    const phase = container.action_phase;
    const verb = actionVerb(action);
    if (!phase || SILENT_ACTION_PHASES.has(phase)) return null;
    if (ACTIVE_ACTION_PHASES.has(phase)) return `${verb}中`;
    if (phase === 'stuck') return `卡在${verb}中`;
    if (phase === 'failed' || phase === 'timeout') return `${verb}失败`;
    return phase;
};
const actionPhaseColor = (phase?: string) => phase === 'stuck'
    ? '#d97706'
    : (phase === 'failed' || phase === 'timeout') ? '#dc2626' : '#2563eb';

interface QRState {
    status: 'logged_in' | 'loaded' | 'waiting' | 'error' | 'scan_confirmed' | 'inject_pending' | 'injected' | 'onebot_ready' | 'loading' | 'expired' | 'refreshing' | 'need_auth';
    url?: string;
    uin?: string;
    reason?: string;
    last_uin?: string;
    configured_uin?: string;
    generated_at?: number;
    fetched_at?: number;
    age_seconds?: number | null;
    expires_in?: number | null;
    expires_at?: number;
    source?: string;
    type?: string;
    action_started_at?: number;
}


const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

const qrImageUrl = (item: QRResponse): string => {
    if (item.image_base64) return `data:image/png;base64,${item.image_base64}`;
    const url = item.url || '';
    if (!url) return '';
    if (url.startsWith('data:image/')) return url;
    if (url.startsWith('http')) return `https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(url)}`;
    return url;
};

const qrStateFromResponse = (item: QRResponse): QRState => {
    if (item.status === 'logged_in') {
        return { status: 'logged_in', uin: item.uin, last_uin: item.last_uin, configured_uin: item.configured_uin };
    }
    if (item.status === 'ok' && (item.url || item.image_base64)) {
        const url = qrImageUrl(item);
        if (url) {
            return {
                status: 'loaded',
                url,
                last_uin: item.last_uin,
                configured_uin: item.configured_uin,
                generated_at: item.generated_at,
                fetched_at: item.fetched_at,
                age_seconds: item.age_seconds,
                expires_in: item.expires_in,
                expires_at: item.expires_at,
                source: item.source,
                type: item.type,
            };
        }
    }
    if (item.status === 'expired') return { status: 'expired', last_uin: item.last_uin, configured_uin: item.configured_uin, source: item.source, generated_at: item.generated_at, fetched_at: item.fetched_at, age_seconds: item.age_seconds, expires_at: item.expires_at, expires_in: item.expires_in, type: item.type };
    // 后端 to_qr_dict_public 用 need_auth 表示“有码但要登录才能看”。
    // 不识别的话会落到 waiting，卡片就一直转圈等一个永远不会来的二维码。
    if (item.status === 'need_auth') {
        return { status: 'need_auth', last_uin: item.last_uin, configured_uin: item.configured_uin, source: item.source, generated_at: item.generated_at, fetched_at: item.fetched_at, age_seconds: item.age_seconds, expires_at: item.expires_at, expires_in: item.expires_in, type: item.type };
    }
    return { status: 'waiting', last_uin: item.last_uin, configured_uin: item.configured_uin, source: item.source };
};

const formatQrAge = (qr: QRState): string => {
    const now = Math.floor(Date.now() / 1000);
    const age = typeof qr.age_seconds === 'number'
        ? qr.age_seconds
        : (qr.generated_at ? Math.max(0, now - qr.generated_at) : null);
    if (age === null) return '刷新时间未知';
    if (age < 60) return `刷新于 ${age} 秒前`;
    return `刷新于 ${Math.floor(age / 60)} 分 ${age % 60} 秒前`;
};

const formatQrMeta = (qr: QRState): string => {
    const parts = [formatQrAge(qr)];
    if (qr.source) parts.push(`来源 ${qr.source}`);
    const expiresIn = typeof qr.expires_in === 'number'
        ? qr.expires_in
        : (qr.expires_at ? Math.max(0, qr.expires_at - Math.floor(Date.now() / 1000)) : null);
    if (expiresIn !== null) parts.push(expiresIn > 0 ? `有效约 ${expiresIn}s` : '已过期');
    return parts.join(' · ');
};

export default function UserDashboard() {
    const navigate = useNavigate();
    const theme = useTheme();
    const colorMode = useContext(ThemeModeContext);
    const { toggleLanguage } = useContext(LanguageContext);
    const t = useTranslate();
    const { user } = useAuth();
    const isAuthenticated = !!user;

    const [loading, setLoading] = useState(true);
    const [qrCodes, setQrCodes] = useState<Record<string, QRState>>({});
    const [searchQuery, setSearchQuery] = useState('');
    const [refreshingCards, setRefreshingCards] = useState<Record<string, boolean>>({});
    const [bgUrl, setBgUrl] = useState('');
    const [qrDialogName, setQrDialogName] = useState<string | null>(null);
    /** 重启后的二维码轮询任务，key = "节点:容器:操作开始时间" */
    const restartPollRef = useRef<Map<string, AbortController>>(new Map());

    // ---- WS 驱动：替代 HTTP 轮询 ----
    const {
        containers: wsContainers,
        qrStates: wsQrStates,
        connected: wsConnected,
        reconnectAttempt: wsReconnectAttempt,
        lastDisconnectReason: wsLastDisconnectReason,
    } = usePublicWebSocket();
    const containers = wsContainers;

    // 轮询任务要读最新的 qrCodes / 推送快照，但 effect 不能因此依赖它们（依赖一变就重跑）
    const qrCodesRef = useRef(qrCodes);
    qrCodesRef.current = qrCodes;
    const wsQrStatesRef = useRef(wsQrStates);
    wsQrStatesRef.current = wsQrStates;

    // WS 首次推送到达后取消 loading
    const initializedRef = useRef(false);
    useEffect(() => {
        if (containers.length > 0 && !initializedRef.current) {
            initializedRef.current = true;
            setLoading(false);
        }
        // WS 连接成功但容器为空也取消 loading（真正 0 个容器的情况）
        if (wsConnected && !initializedRef.current) {
            const timer = setTimeout(() => {
                if (!initializedRef.current) {
                    initializedRef.current = true;
                    setLoading(false);
                }
            }, 3500);
            return () => clearTimeout(timer);
        }
    }, [containers, wsConnected]);

    // WS 推送的 QR 状态 → 合并到 qrCodes
    useEffect(() => {
        if (!wsQrStates || Object.keys(wsQrStates).length === 0) return;
        setQrCodes(prev => {
            const next = { ...prev };
            for (const [name, item] of Object.entries(wsQrStates)) {
                const current = prev[name];
                const merged = qrStateFromResponse(item as QRResponse);
                if (merged.status !== 'logged_in') {
                    // 推送是周期快照，可能还没扫到用户手动刷出来的新码；
                    // 无条件覆盖会让手动刷新出的二维码下一个 tick 就被打回旧的。
                    if (current?.generated_at && merged.generated_at && merged.generated_at < current.generated_at) continue;
                    const waitingForFreshRestart = current?.status === 'refreshing'
                        && current.action_started_at
                        && (!merged.generated_at || merged.generated_at < current.action_started_at);
                    if (waitingForFreshRestart) continue;
                }
                next[name] = merged;
            }
            return next;
        });
    }, [wsQrStates, containers]);

    useEffect(() => {
        const restarting = containers.filter(c =>
            c.action === 'restart'
            && ACTIVE_ACTION_PHASES.has(c.action_phase || '')
            && c.action_started_at
        );
        if (restarting.length === 0) return;
        setQrCodes(prev => {
            // 每次 WS 推送都会跑这个 effect，占位没变就别造新对象，白白触发整页重渲染
            const changed = restarting.some(c => {
                const cur = prev[c.name];
                return cur?.status !== 'refreshing' || cur.action_started_at !== c.action_started_at;
            });
            if (!changed) return prev;
            const next = { ...prev };
            for (const c of restarting) {
                next[c.name] = { status: 'refreshing', action_started_at: c.action_started_at, source: 'restart_action' };
            }
            return next;
        });
    }, [containers]);

    /**
     * 重启后 NapCat 要过十几秒才会写出新二维码，这里补一段短轮询。
     *
     * 轮询任务必须挂在 ref 上、只在卸载时取消：之前定时器挂在 effect cleanup 里，
     * 而 effect 依赖 qrCodes —— 每来一次 WS 推送就把 6 个定时器全清掉，
     * 可 key 已经记成“已调度”不会重排，于是二维码永久卡在“刷新中”。
     */
    useEffect(() => {
        const pending = containers.filter(c => {
            const qr = qrCodesRef.current[c.name];
            return c.status === 'running'
                && c.action === 'restart'
                && c.action_started_at
                && (!c.action_phase || SILENT_ACTION_PHASES.has(c.action_phase))
                && (!qr || qr.status === 'refreshing' || !qr.generated_at || qr.generated_at < (c.action_started_at || 0));
        });
        for (const c of pending) {
            const startedAt = c.action_started_at || 0;
            const key = `${c.node_id}:${c.name}:${startedAt}`;
            if (restartPollRef.current.has(key)) continue;
            const controller = new AbortController();
            restartPollRef.current.set(key, controller);
            void (async () => {
                try {
                    for (let i = 0; i < 6; i++) {
                        await wait(i === 0 ? 300 : 3000 + i * 2000);
                        if (controller.signal.aborted) return;
                        try {
                            const next = qrStateFromResponse(await publicApi.getQR(c.name, c.node_id));
                            const fresh = next.status === 'logged_in'
                                || (next.status === 'loaded' && (next.generated_at || 0) >= startedAt);
                            if (fresh) {
                                setQrCodes(prev => ({ ...prev, [c.name]: next }));
                                return;
                            }
                            setQrCodes(prev => prev[c.name]?.status === 'refreshing'
                                ? prev
                                : { ...prev, [c.name]: { status: 'refreshing', action_started_at: startedAt, source: 'restart_action' } });
                        } catch { /* 容器刚起来时 QR 接口会短暂失败，继续等下一轮 */ }
                    }
                    // 轮询用尽仍没等到新码：必须撤掉占位交回 WS 推送，否则卡片会一直转圈
                    if (controller.signal.aborted) return;
                    setQrCodes(prev => {
                        if (prev[c.name]?.status !== 'refreshing') return prev;
                        const latest = wsQrStatesRef.current[c.name];
                        if (latest) return { ...prev, [c.name]: qrStateFromResponse(latest) };
                        const rest = { ...prev };
                        delete rest[c.name];
                        return rest;
                    });
                } finally {
                    restartPollRef.current.delete(key);
                }
            })();
        }
    }, [containers]);

    // 只在离开页面时收掉在飞的轮询 —— 依赖数组为空，不会被 WS 推送打断
    useEffect(() => {
        const polls = restartPollRef.current;
        return () => {
            polls.forEach(c => c.abort());
            polls.clear();
        };
    }, []);

    // QQ号遮蔽：仅未登录前端显示层使用，API/WS/搜索/头像均保留完整QQ号。
    const maskUin = (uin: string) => {
        const digits = uin.replace(/\D/g, '');
        if (digits.length <= 4) return digits;
        return digits.slice(0, 3) + '***' + digits.slice(-3);
    };

    const displayQqText = (uin: string) => isAuthenticated ? uin : maskUin(uin);

    // 加载背景壁纸：根据窗口方向选择横图/竖图
    useEffect(() => {
        let cancelled = false;
        // 每个方向只随机选一次，resize 时仅切换方向不重新随机
        let picked: { landscape: string; portrait: string } | null = null;

        const pick = (list: string[]) => list.length ? list[Math.floor(Math.random() * list.length)] : '';

        const applyOrientation = () => {
            if (!picked) return;
            const isLandscape = window.innerWidth >= window.innerHeight;
            const url = isLandscape
                ? (picked.landscape || picked.portrait)
                : (picked.portrait || picked.landscape);
            if (url) setBgUrl(url);
        };

        (async () => {
            try {
                const res = await fetch('/api/resource/wallpapers?category=user-dashboard');
                const json = await res.json();
                if (cancelled || json.status !== 'ok') return;
                picked = {
                    landscape: pick(json.landscape || []),
                    portrait: pick(json.portrait || []),
                };
                applyOrientation();
            } catch { /* ignore */ }
        })();

        const onResize = () => applyOrientation();
        window.addEventListener('resize', onResize);
        return () => { cancelled = true; window.removeEventListener('resize', onResize); };
    }, []);

    // ---- 搜索过滤 + 分页（保留 MCSM 式按页订阅） ----
    const [page, setPage] = useState(1);
    const rowsPerPage = 12;
    const filteredContainers = containers.filter(c => {
        if (!searchQuery.trim()) return true;
        const q = searchQuery.toLowerCase();
        return c.name.toLowerCase().includes(q)
            || (c.uin && c.uin.toLowerCase().includes(q))
            || (c.last_uin && c.last_uin.toLowerCase().includes(q))
            || (c.display_status && c.display_status.toLowerCase().includes(q))
            || c.status.toLowerCase().includes(q);
    });
    const totalPages = Math.ceil(filteredContainers.length / rowsPerPage);
    const displayedContainers = filteredContainers.slice((page - 1) * rowsPerPage, page * rowsPerPage);

    // WS 推送删掉容器后 totalPages 会缩水，page 停在越界页会导致列表空白且分页控件被隐藏，只能刷新页面
    useEffect(() => {
        if (totalPages > 0 && page > totalPages) setPage(totalPages);
        else if (totalPages === 0 && page !== 1) setPage(1);
    }, [page, totalPages]);

    // 搜索高亮
    const highlight = (text: string) => {
        const q = searchQuery.trim();
        if (!q) return text;
        const idx = text.toLowerCase().indexOf(q.toLowerCase());
        if (idx === -1) return text;
        return <>{text.slice(0, idx)}<Box component="span" sx={{ bgcolor: '#fef08a', color: '#000', borderRadius: 0.5, px: 0.25 }}>{text.slice(idx, idx + q.length)}</Box>{text.slice(idx + q.length)}</>;
    };

    // 单容器 QR 加载（仅用于 refreshCard 单卡手动刷新，保留独立请求）
    const loadQR = async (name: string, node_id = 'local') => {
        try {
            const data = await publicApi.getQR(name, node_id);
            setQrCodes(prev => ({ ...prev, [name]: qrStateFromResponse(data) }));
        } catch {
            setQrCodes(prev => ({ ...prev, [name]: { status: 'error' } }));
        }
    };

    // 单卡片刷新：手动触发独立请求
    const refreshCard = async (name: string, node_id = 'local') => {
        setRefreshingCards(prev => ({ ...prev, [name]: true }));
        try {
            await loadQR(name, node_id);
        } finally {
            setRefreshingCards(prev => ({ ...prev, [name]: false }));
        }
    };

    return (
        <Box sx={{
            p: { xs: 2, md: 4, lg: 6 }, minHeight: '100vh',
            bgcolor: 'background.default',
            position: 'relative',
            '&::before': bgUrl ? {
                content: '""', position: 'fixed', inset: 0, zIndex: 0,
                backgroundImage: `url(${bgUrl})`,
                backgroundSize: 'cover', backgroundPosition: 'center',
                opacity: theme.palette.mode === 'dark' ? 0.15 : 0.2,
                pointerEvents: 'none',
            } : {},
        }}>
            <Box sx={{ maxWidth: 1100, mx: 'auto', position: 'relative', zIndex: 1 }}>

                {/* Header */}
                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
                    <Typography variant="h5" sx={{ fontWeight: 800, color: 'text.primary' }}>
                        {t('user.title')}
                    </Typography>
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                            <Typography variant="caption" color="text.secondary" sx={{ fontSize: '0.75rem' }}>
                                {wsConnected ? t('admin.wsConnected') : t('admin.wsDisconnected')}
                            </Typography>
                            {!wsConnected && wsReconnectAttempt > 0 && (
                                <Typography variant="caption" color="text.secondary" sx={{ fontSize: '0.75rem' }}>
                                    {`(${t('admin.wsRetry')}: ${wsReconnectAttempt})`}
                                </Typography>
                            )}
                            {!wsConnected && wsLastDisconnectReason && (
                                <Typography variant="caption" color="text.secondary" sx={{ fontSize: '0.75rem' }}>
                                    {t(`admin.wsDisconnectReason.${wsLastDisconnectReason}`)}
                                </Typography>
                            )}
                            {!wsConnected && wsLastDisconnectReason === 'capacity_limited' && (
                                // 连接数打满会一直自动排队重连，明确说清楚，免得用户以为要自己刷新页面
                                <Typography variant="caption" sx={{ fontSize: '0.75rem', color: '#d97706', fontWeight: 600 }}>
                                    {t('当前在线人数已满，正在自动排队重连，无需刷新页面')}
                                </Typography>
                            )}
                        </Box>
                        <TextField
                            size="small"
                            placeholder={t('user.searchPlaceholder')}
                            value={searchQuery}
                            onChange={e => { setSearchQuery(e.target.value); setPage(1); }}
                            InputProps={{
                                startAdornment: (
                                    <InputAdornment position="start">
                                        <SearchIcon sx={{ fontSize: 18, color: 'text.secondary' }} />
                                    </InputAdornment>
                                ),
                            }}
                            sx={{
                                width: { xs: 140, sm: 200 },
                                '& .MuiOutlinedInput-root': {
                                    borderRadius: 2,
                                    height: 36,
                                    fontSize: '0.85rem',
                                },
                            }}
                        />
                        <Button
                            variant="outlined"
                            color="inherit"
                            size="small"
                            startIcon={<AdminPanelSettingsIcon />}
                            onClick={() => navigate('/login')}
                            sx={{ borderColor: 'divider', color: 'text.secondary', borderRadius: 2, height: 36, textTransform: 'none' }}
                        >
                            {t('user.adminLogin')}
                        </Button>
                        <IconButton onClick={toggleLanguage} aria-label="Toggle language">
                            <TranslateIcon />
                        </IconButton>
                        <IconButton onClick={colorMode.toggleTheme} aria-label="Toggle theme">
                            {theme.palette.mode === 'dark' ? <Brightness7Icon /> : <Brightness4Icon />}
                        </IconButton>
                    </Box>
                </Box>

                {/* Cards */}
                <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 2 }}>
                    {loading ? [...Array(4)].map((_, i) => <Skeleton key={i} variant="rounded" height={120} sx={{ borderRadius: 3, bgcolor: theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.06)' : 'rgba(255,255,255,0.6)' }} />)
                        : filteredContainers.length === 0 ? (
                            <Box sx={{ gridColumn: '1 / -1', py: 8, display: 'flex', flexDirection: 'column', alignItems: 'center', borderRadius: 3, border: `1px solid ${theme.palette.divider}` }}>
                                <CloudOffIcon sx={{ fontSize: 48, color: '#94a3b8', mb: 1.5 }} />
                                <Typography variant="body1" sx={{ fontWeight: 600, color: 'text.primary' }}>{searchQuery ? t('user.noSearchResults') : t('user.noBots')}</Typography>
                                <Typography variant="body2" color="text.secondary">{searchQuery ? t('user.tryDifferentSearch') : t('user.contactAdmin')}</Typography>
                            </Box>
                        ) : displayedContainers.map(c => {
                            const qr = qrCodes[c.name] || { status: 'loading' as const };
                            const isRefreshing = refreshingCards[c.name] || false;
                            const isCurrentLogin = c.login_stage === 'logged_in';
                            const uinDigits = isCurrentLogin ? ((c.uin ? String(c.uin).replace(/\D/g, '') : '') || (qr.status === 'logged_in' && qr.uin ? String(qr.uin).replace(/\D/g, '') : '')) : '';
                            // last_uin: 从容器数据或 QR 状态获取，掉线后仍能显示最后登录的Q号
                            const lastUinDigits = c.last_uin ? String(c.last_uin).replace(/\D/g, '')
                                : (qr.last_uin ? String(qr.last_uin).replace(/\D/g, '') : '');
                            const configuredUinDigits = c.configured_uin ? String(c.configured_uin).replace(/\D/g, '')
                                : (qr.configured_uin ? String(qr.configured_uin).replace(/\D/g, '') : '');
                            const displayUin = uinDigits || lastUinDigits || configuredUinDigits;
                            const avatarSrc = displayUin ? `/api/resource/avatar/${displayUin}` : '';
                            const isLastUinOnly = !uinDigits && !!(lastUinDigits || configuredUinDigits);
                            const offlineLabel = lastUinDigits ? '上次登录' : '配置账号';
                            return (
                                <Box key={c.id} sx={{
                                    background: theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.04)' : 'rgba(255,255,255,0.65)',
                                    backdropFilter: 'blur(12px)',
                                    borderRadius: 3, border: `1px solid ${theme.palette.divider}`,
                                    p: 2, display: 'flex', flexDirection: 'row', alignItems: 'stretch',
                                    transition: 'all 0.2s', gap: 1.5,
                                    position: 'relative', overflow: 'hidden',
                                    // 节点失联时卡片整体灰掉：下面显示的全是最后一次同步到的旧值
                                    filter: c.stale ? 'grayscale(0.9)' : 'none', opacity: c.stale ? 0.72 : 1,
                                    '&:hover': { borderColor: theme.palette.primary.main, boxShadow: `0 0 0 1px ${theme.palette.primary.main}22` }
                                }}>
                                    {/* 头像虚化叠底 — 最底层，覆盖卡片左侧大部分 */}
                                    {avatarSrc && (
                                        <Box
                                            component="img"
                                            src={avatarSrc}
                                            aria-hidden="true"
                                            sx={{
                                                position: 'absolute',
                                                left: '-8%', top: '-15%',
                                                width: '68%', height: '130%',
                                                objectFit: 'cover',
                                                filter: (!isCurrentLogin || isLastUinOnly) ? 'blur(4px) grayscale(100%) opacity(0.3)' : 'blur(4px) saturate(1.8)',
                                                opacity: theme.palette.mode === 'dark' ? 0.55 : 0.62,
                                                zIndex: 0,
                                                pointerEvents: 'none',
                                                borderRadius: 0,
                                                maskImage: 'linear-gradient(to right, black 55%, transparent 100%)',
                                                WebkitMaskImage: 'linear-gradient(to right, black 55%, transparent 100%)',
                                            }}
                                        />
                                    )}
                                    {/* 左侧 - 信息区 */}
                                    <Box sx={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, position: 'relative', zIndex: 1 }}>
                                        {/* 容器名（居中，最多两行自动换行） */}
                                        <Typography variant="subtitle2" sx={{
                                            fontWeight: 700, textAlign: 'center', fontSize: '0.88rem',
                                            display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
                                            overflow: 'hidden', textOverflow: 'ellipsis',
                                            wordBreak: 'break-all', lineHeight: 1.35, minHeight: '2.4em',
                                        }}>{highlight(c.name)}</Typography>
                                        {c.stale && (
                                            <Typography variant="caption" title={t('所属节点已失联，这里显示的是最后一次同步到的旧状态')}
                                                sx={{ mt: 0.25, textAlign: 'center', fontSize: '0.62rem', fontWeight: 700, color: '#d97706', lineHeight: 1.2 }}>
                                                {t('节点失联，状态可能不准确')}
                                            </Typography>
                                        )}
                                        {/* 头像 + QQ号（有 uin 或 last_uin 就显示，居中） */}
                                        {displayUin && (
                                            <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', mt: 1, gap: 0.5 }}>
                                                <Box component="img"
                                                    src={avatarSrc}
                                                    sx={{ width: 32, height: 32, borderRadius: '50%', objectFit: 'cover', filter: (!isCurrentLogin || isLastUinOnly) ? 'grayscale(100%) opacity(0.6)' : 'none' }}
                                                />
                                                <Typography variant="caption" sx={{ color: isLastUinOnly ? 'text.disabled' : 'text.secondary', fontSize: '0.72rem', fontStyle: isLastUinOnly ? 'italic' : 'normal' }}>
                                                    {isLastUinOnly ? `${offlineLabel}：${displayQqText(displayUin)}` : `QQ: ${displayQqText(displayUin)}`}
                                                </Typography>
                                            </Box>
                                        )}
                                        {/* 底部：状态 + 刷新按钮，两端对齐 */}
                                        <Box sx={{ mt: 'auto', pt: 1, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                                            {(() => {
                                                const phaseLabel = actionPhaseLabel(c);
                                                if (!phaseLabel) return null;
                                                const color = actionPhaseColor(c.action_phase);
                                                return (
                                                    <Typography variant="caption" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.4, color, fontWeight: 700, fontSize: '0.7rem' }}>
                                                        <Box sx={{ width: 5, height: 5, bgcolor: color, borderRadius: '50%' }} /> {phaseLabel}
                                                    </Typography>
                                                );
                                            })() || (c.status === 'running' && qr.status === 'logged_in' ? (
                                                // 已登录：根据心跳状态显示 绿/橙/红
                                                c.bot_online ? (
                                                    <Typography variant="caption" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.4, color: '#059669', fontWeight: 600, fontSize: '0.7rem' }}>
                                                        <Box sx={{ width: 5, height: 5, bgcolor: '#10b981', borderRadius: '50%' }} /> {t('admin.online')}
                                                    </Typography>
                                                ) : (c.bot_heartbeat_ts ?? 0) > 0 ? (
                                                    <Typography variant="caption" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.4, color: '#dc2626', fontWeight: 600, fontSize: '0.7rem' }}>
                                                        <Box sx={{ width: 5, height: 5, bgcolor: '#ef4444', borderRadius: '50%' }} /> {t('admin.heartbeatLost')}
                                                    </Typography>
                                                ) : (
                                                    <Typography variant="caption" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.4, color: '#d97706', fontWeight: 600, fontSize: '0.7rem' }}>
                                                        <Box sx={{ width: 5, height: 5, bgcolor: '#f59e0b', borderRadius: '50%' }} /> {t('admin.botOnline')}
                                                    </Typography>
                                                )
                                            ) : c.status === 'running' ? (
                                                c.login_stage === 'unknown' ? (
                                                    // 运行中但状态未知（登录检测失败） → 橙色
                                                    <Typography variant="caption" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.4, color: '#d97706', fontWeight: 600, fontSize: '0.7rem' }}>
                                                        <Box sx={{ width: 5, height: 5, bgcolor: '#f59e0b', borderRadius: '50%' }} /> {t('admin.statusUnknown')}
                                                    </Typography>
                                                ) : (
                                                    // 运行中但未登录 → 蓝色
                                                    <Typography variant="caption" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.4, color: '#2563eb', fontWeight: 600, fontSize: '0.7rem' }}>
                                                        <Box sx={{ width: 5, height: 5, bgcolor: '#3b82f6', borderRadius: '50%' }} /> {t('admin.pendingLogin')}
                                                    </Typography>
                                                )
                                            ) : (
                                                // 容器未运行 → 灰色
                                                <Typography variant="caption" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.4, color: 'text.secondary', fontWeight: 600, fontSize: '0.7rem' }}>
                                                    <Box sx={{ width: 5, height: 5, bgcolor: '#94a3b8', borderRadius: '50%' }} /> {t('admin.offline')}
                                                </Typography>
                                            ))}
                                            <IconButton
                                                size="small"
                                                disabled={isRefreshing}
                                                onClick={() => refreshCard(c.name, c.node_id)}
                                                sx={{ color: 'text.secondary', p: 0.5 }}
                                            >
                                                {isRefreshing ? <CircularProgress size={14} /> : <RefreshIcon sx={{ fontSize: 16 }} />}
                                            </IconButton>
                                        </Box>
                                    </Box>
                                    {/* 右侧 - QR / 状态区 */}
                                    <Box
                                        onClick={() => qr.status === 'loaded' ? setQrDialogName(c.name) : undefined}
                                        sx={{
                                            width: 140, minHeight: 140, borderRadius: 2, overflow: 'hidden',
                                            display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                                            bgcolor: theme.palette.mode === 'dark' ? '#1e293b' : '#f8fafc',
                                            cursor: qr.status === 'loaded' ? 'pointer' : 'default',
                                            transition: 'transform 0.15s',
                                            '&:hover': qr.status === 'loaded' ? { transform: 'scale(1.04)' } : {},
                                        }}
                                    >
                                        {c.status !== 'running' ? (
                                            <CloudOffIcon sx={{ color: '#94a3b8', fontSize: 32 }} />
                                        ) : qr.status === 'loaded' ? (
                                            <Box sx={{ width: '100%', height: '100%', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 0.5, p: 0.5 }}>
                                                <LazyQRImage src={qr.url!} alt="QR" width="100%" height="calc(100% - 22px)" style={{ objectFit: 'contain' }} />
                                                <Typography variant="caption" sx={{ color: 'text.secondary', fontSize: '0.62rem', lineHeight: 1, textAlign: 'center', maxWidth: '100%', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                                                    {formatQrAge(qr)}
                                                </Typography>
                                            </Box>
                                        ) : qr.status === 'logged_in' ? (
                                            <Typography variant="caption" sx={{ color: '#059669', fontWeight: 600, fontSize: '0.7rem' }}>{t('user.loggedIn')}</Typography>
                                        ) : qr.status === 'need_auth' ? (
                                            // 二维码是有的，只是没登录看不了 —— 转圈只会让人以为还没生成
                                            <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 0.75, px: 1 }}>
                                                <AdminPanelSettingsIcon sx={{ fontSize: 26, color: '#94a3b8' }} />
                                                <Typography variant="caption" sx={{ color: 'text.secondary', fontSize: '0.65rem', textAlign: 'center', lineHeight: 1.3 }}>
                                                    {t('登录后可查看二维码')}
                                                </Typography>
                                            </Box>
                                        ) : qr.status === 'waiting' || qr.status === 'loading' || qr.status === 'refreshing' ? (
                                            <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1 }}>
                                                <CircularProgress size={24} sx={{ color: '#94a3b8' }} />
                                                <Typography variant="caption" sx={{ color: '#94a3b8', fontSize: '0.65rem', textAlign: 'center' }}>
                                                    {qr.status === 'refreshing' ? '刷新中' : t('user.waitingQr')}
                                                </Typography>
                                            </Box>
                                        ) : qr.status === 'expired' ? (
                                            <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 0.5 }}>
                                                <CircularProgress size={24} sx={{ color: '#f59e0b' }} />
                                                <Typography variant="caption" sx={{ color: '#f59e0b', fontSize: '0.65rem' }}>{t('user.qrExpired')}</Typography>
                                                {qr.generated_at && (
                                                    <Typography variant="caption" sx={{ color: 'text.secondary', fontSize: '0.6rem', lineHeight: 1 }}>{formatQrAge(qr)}</Typography>
                                                )}
                                            </Box>
                                        ) : (
                                            <Typography variant="caption" color="error" sx={{ fontSize: '0.7rem' }}>{t('user.loadFailed')}</Typography>
                                        )}
                                    </Box>
                                </Box>
                            );
                        })}
                </Box>

                {totalPages > 1 && (
                    <Box sx={{ display: 'flex', justifyContent: 'center', mt: 4 }}>
                        <Pagination
                            count={totalPages}
                            page={page}
                            onChange={(_, value) => setPage(value)}
                            color="primary"
                            shape="rounded"
                        />
                    </Box>
                )}

            </Box>

            {/* QR 放大弹窗 */}
            <Dialog
                open={!!qrDialogName}
                onClose={() => setQrDialogName(null)}
                maxWidth="xs"
                fullWidth
                PaperProps={{
                    sx: {
                        borderRadius: 4, backgroundImage: 'none',
                        bgcolor: theme.palette.mode === 'dark' ? '#1e1e1e' : '#fff',
                    }
                }}
            >
                <DialogContent sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', p: 4 }}>
                    {qrDialogName && (() => {
                        const qr = qrCodes[qrDialogName];
                        const container = containers.find(c => c.name === qrDialogName);
                        if (!qr || qr.status !== 'loaded') return null;
                        return (
                            <>
                                <Typography variant="h6" sx={{ fontWeight: 700, mb: 2, textAlign: 'center' }}>
                                    {container?.name || qrDialogName}
                                </Typography>
                                <Box sx={{
                                    width: 280, height: 280, borderRadius: 3, overflow: 'hidden',
                                    bgcolor: '#fff', p: 1, border: `1px solid ${theme.palette.divider}`,
                                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                                }}>
                                    <img src={qr.url} alt="QR Code" style={{ width: '100%', height: '100%', objectFit: 'contain' }} />
                                </Box>
                                <Typography variant="body2" color="text.secondary" sx={{ mt: 2, textAlign: 'center' }}>
                                    {t('user.scanToLogin')}
                                </Typography>
                                <Typography variant="caption" color="text.secondary" sx={{ mt: 0.75, textAlign: 'center' }}>
                                    {formatQrMeta(qr)}
                                </Typography>
                            </>
                        );
                    })()}
                </DialogContent>
            </Dialog>
        </Box>
    );
}
