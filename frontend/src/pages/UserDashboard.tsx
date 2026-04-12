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
import { useTranslate } from '../i18n';
import { publicApi, type Container } from '../services/api';
import { usePublicWebSocket } from '../hooks/usePublicWebSocket';
import LazyQRImage from '../components/LazyQRImage';

interface QRState {
    status: 'logged_in' | 'loaded' | 'waiting' | 'error' | 'scan_confirmed' | 'inject_pending' | 'injected' | 'onebot_ready' | 'loading' | 'expired';
    url?: string;
    uin?: string;
    reason?: string;
    last_uin?: string;
}

export default function UserDashboard() {
    const navigate = useNavigate();
    const theme = useTheme();
    const colorMode = useContext(ThemeModeContext);
    const { toggleLanguage } = useContext(LanguageContext);
    const t = useTranslate();

    const [loading, setLoading] = useState(true);
    const [qrCodes, setQrCodes] = useState<Record<string, QRState>>({});
    const [searchQuery, setSearchQuery] = useState('');
    const [refreshingCards, setRefreshingCards] = useState<Record<string, boolean>>({});
    const [bgUrl, setBgUrl] = useState('');
    const [qrDialogName, setQrDialogName] = useState<string | null>(null);

    // ---- WS 驱动：替代 HTTP 轮询 ----
    const {
        containers: wsContainers,
        qrStates: wsQrStates,
        connected: wsConnected,
        reconnectAttempt: wsReconnectAttempt,
        lastDisconnectReason: wsLastDisconnectReason,
    } = usePublicWebSocket();
    const containers = wsContainers;

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
                if (item.status === 'logged_in') {
                    next[name] = { status: 'logged_in', uin: item.uin };
                } else if (item.status === 'ok' && item.url) {
                    const url = item.type === 'file' ? item.url
                        : `https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(item.url)}`;
                    next[name] = { status: 'loaded', url, last_uin: (item as any).last_uin };
                } else if (item.status === 'expired') {
                    next[name] = { status: 'expired', last_uin: (item as any).last_uin };
                } else if (item.status === 'need_auth') {
                    next[name] = { status: 'waiting', last_uin: (item as any).last_uin };
                } else {
                    next[name] = { status: 'waiting', last_uin: (item as any).last_uin };
                }
            }
            return next;
        });
    }, [wsQrStates, containers]);

    // QQ号遮蔽：385***633
    const maskUin = (uin: string) => {
        const digits = uin.replace(/\D/g, '');
        if (digits.length <= 4) return digits;
        return digits.slice(0, 3) + '***' + digits.slice(-3);
    };

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
            || c.status.toLowerCase().includes(q);
    });
    const totalPages = Math.ceil(filteredContainers.length / rowsPerPage);
    const displayedContainers = filteredContainers.slice((page - 1) * rowsPerPage, page * rowsPerPage);

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
            if (data.status === 'logged_in') {
                setQrCodes(prev => ({ ...prev, [name]: { status: 'logged_in', uin: data.uin } }));
            } else if (data.status === 'ok' && data.url) {
                const url = data.type === 'file' ? data.url
                    : `https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(data.url)}`;
                setQrCodes(prev => ({ ...prev, [name]: { status: 'loaded', url } }));
            } else if (data.status === 'expired') {
                setQrCodes(prev => ({ ...prev, [name]: { status: 'expired' } }));
            } else {
                setQrCodes(prev => ({ ...prev, [name]: { status: 'waiting' } }));
            }
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
                            const uinDigits = qr.uin ? String(qr.uin).replace(/\D/g, '') : '';
                            // last_uin: 从容器数据或 QR 状态获取，掉线后仍能显示最后登录的Q号
                            const lastUinDigits = c.last_uin ? String(c.last_uin).replace(/\D/g, '')
                                : (qr.last_uin ? String(qr.last_uin).replace(/\D/g, '') : '');
                            const displayUin = uinDigits || lastUinDigits;
                            const avatarSrc = displayUin ? `/api/resource/avatar/${displayUin}` : '';
                            const isLastUinOnly = !uinDigits && !!lastUinDigits;
                            return (
                                <Box key={c.id} sx={{
                                    background: theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.04)' : 'rgba(255,255,255,0.65)',
                                    backdropFilter: 'blur(12px)',
                                    borderRadius: 3, border: `1px solid ${theme.palette.divider}`,
                                    p: 2, display: 'flex', flexDirection: 'row', alignItems: 'stretch',
                                    transition: 'all 0.2s', gap: 1.5,
                                    position: 'relative', overflow: 'hidden',
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
                                                filter: (qr.status !== 'logged_in' || isLastUinOnly) ? 'blur(4px) grayscale(100%) opacity(0.3)' : 'blur(4px) saturate(1.8)',
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
                                        {/* 头像 + QQ号（有 uin 或 last_uin 就显示，居中） */}
                                        {displayUin && (
                                            <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', mt: 1, gap: 0.5 }}>
                                                <Box component="img"
                                                    src={avatarSrc}
                                                    sx={{ width: 32, height: 32, borderRadius: '50%', objectFit: 'cover', filter: (qr.status !== 'logged_in' || isLastUinOnly) ? 'grayscale(100%) opacity(0.6)' : 'none' }}
                                                />
                                                <Typography variant="caption" sx={{ color: isLastUinOnly ? 'text.disabled' : 'text.secondary', fontSize: '0.72rem', fontStyle: isLastUinOnly ? 'italic' : 'normal' }}>
                                                    {isLastUinOnly ? `QQ: ${maskUin(displayUin)} (离线)` : `QQ: ${maskUin(displayUin)}`}
                                                </Typography>
                                            </Box>
                                        )}
                                        {/* 底部：状态 + 刷新按钮，两端对齐 */}
                                        <Box sx={{ mt: 'auto', pt: 1, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                                            {c.status === 'running' && qr.status === 'logged_in' ? (
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
                                            )}
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
                                            <LazyQRImage src={qr.url!} alt="QR" width="100%" height="100%" style={{ objectFit: 'cover' }} />
                                        ) : qr.status === 'logged_in' ? (
                                            <Typography variant="caption" sx={{ color: '#059669', fontWeight: 600, fontSize: '0.7rem' }}>{t('user.loggedIn')}</Typography>
                                        ) : qr.status === 'waiting' || qr.status === 'loading' ? (
                                            <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1 }}>
                                                <CircularProgress size={24} sx={{ color: '#94a3b8' }} />
                                                <Typography variant="caption" sx={{ color: '#94a3b8', fontSize: '0.65rem' }}>{t('user.waitingQr')}</Typography>
                                            </Box>
                                        ) : qr.status === 'expired' ? (
                                            <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1 }}>
                                                <CircularProgress size={24} sx={{ color: '#f59e0b' }} />
                                                <Typography variant="caption" sx={{ color: '#f59e0b', fontSize: '0.65rem' }}>{t('user.qrExpired')}</Typography>
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
                            </>
                        );
                    })()}
                </DialogContent>
            </Dialog>
        </Box>
    );
}
