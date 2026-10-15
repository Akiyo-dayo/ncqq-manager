/**
 * BasicInfo 组件 - 容器基本信息（现代化三列仪表盘风格）
 */
import { useEffect, useState, useRef, useCallback } from 'react';
import {
    Box, Typography, Button, CircularProgress, Chip,
    Grid, useTheme, IconButton, Tooltip,
    Dialog, DialogTitle, DialogContent, DialogActions, FormControlLabel, Checkbox
} from '@mui/material';
import RefreshIcon from '@mui/icons-material/Refresh';
import QrCode2Icon from '@mui/icons-material/QrCode2';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import StopIcon from '@mui/icons-material/Stop';
import RestartAltIcon from '@mui/icons-material/RestartAlt';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import OpenInNewIcon from '@mui/icons-material/OpenInNew';
import LoginIcon from '@mui/icons-material/Login';
import LogoutIcon from '@mui/icons-material/Logout';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import HttpIcon from '@mui/icons-material/Http';
import CableIcon from '@mui/icons-material/Cable';
import CloudUploadIcon from '@mui/icons-material/CloudUpload';
import CloudDownloadIcon from '@mui/icons-material/CloudDownload';
import HubIcon from '@mui/icons-material/Hub';
import { useNavigate } from 'react-router-dom';
import { containerApi, type ContainerStats, type QRResponse } from '../services/api';
import { useTranslate } from '../i18n';
import { useToast } from './Toast';
import { useAuth } from '../contexts/AuthContext';


const qrImageUrl = (data: QRResponse): string => {
    if (data.image_base64) return `data:image/png;base64,${data.image_base64}`;
    const url = data.url || '';
    if (!url) return '';
    if (url.startsWith('data:image/')) return url;
    if (url.startsWith('http')) return `https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(url)}`;
    return url;
};

const formatQrAge = (generatedAt?: number, ageSeconds?: number | null): string => {
    const age = typeof ageSeconds === 'number'
        ? ageSeconds
        : (generatedAt ? Math.max(0, Math.floor(Date.now() / 1000) - generatedAt) : null);
    if (age === null) return '刷新时间未知';
    if (age < 60) return `刷新于 ${age} 秒前`;
    return `刷新于 ${Math.floor(age / 60)} 分 ${age % 60} 秒前`;
};

interface BasicInfoProps {
    name: string;
    node_id: string;
}

/** 操作的最终结论。superseded/unknown 后端已回落到真实 Docker 状态，只能说“结果未知”，不能报成功也不能报失败 */
type ActionOutcome = 'succeeded' | 'failed' | 'stuck' | 'unknown' | 'pending';

const OPERATION_OUTCOMES: Record<string, ActionOutcome> = {
    succeeded: 'succeeded',
    failed: 'failed',
    timeout: 'failed',
    stuck: 'stuck',
    superseded: 'unknown',
    unknown: 'unknown',
};

export const BasicInfo = ({ name, node_id }: BasicInfoProps) => {
    const [stats, setStats] = useState<Partial<ContainerStats>>({});
    const [qrcode, setQrcode] = useState('');
    const [showQrcode, setShowQrcode] = useState(false);
    const [qrExpiresIn, setQrExpiresIn] = useState<number | null>(null);
    const [qrMeta, setQrMeta] = useState<Pick<QRResponse, 'generated_at' | 'fetched_at' | 'age_seconds' | 'expires_in' | 'expires_at' | 'source' | 'type'> | null>(null);
    const [qrRefreshing, setQrRefreshing] = useState(false);
    const [loading, setLoading] = useState(false);
    const [actionLoading, setActionLoading] = useState('');
    const [deleteDialog, setDeleteDialog] = useState({ open: false, deleteData: false });
    // 退出登录确认：用应用内对话框替代原生 window.confirm（可国际化、可主题化、不阻塞）
    const [logoutDialog, setLogoutDialog] = useState(false);
    // 二维码接口回传的登录结论。SnowLuma 账号的登录态可能先由 QR 接口确认，
    // 而 stats.uin 要等状态引擎（最长 4 分钟）才更新 —— 两者取或，避免"已登录却一直待登录"。
    const [qrLoggedIn, setQrLoggedIn] = useState(false);
    // 二维码不可用时的原因提示（区分"一键登录界面"与"窗口未就绪"），避免无限转圈。
    const [qrNotice, setQrNotice] = useState<{ kind: '' | 'oneclick' | 'notready' | 'wrongaccount'; text: string }>({ kind: '', text: '' });
    const theme = useTheme();
    const navigate = useNavigate();
    const t = useTranslate();
    const toast = useToast();
    const { isAdmin } = useAuth();

    // useRef 持有登录状态，避免 setInterval 闭包快照 bug
    const isLoggedInRef = useRef(false);
    // 操作轮询最长会跑一分多钟，用户切走后必须能提前退出
    const mountedRef = useRef(true);
    useEffect(() => {
        mountedRef.current = true;
        return () => { mountedRef.current = false; };
    }, []);

    const fetchStats = useCallback(async () => {
        setLoading(true);
        try {
            const data = await containerApi.getStats(name, node_id);
            setStats(data);
            const loggedIn = !!(data.uin && data.uin !== '未登录 / Not Logged In');
            isLoggedInRef.current = loggedIn;
            if (loggedIn) {
                setShowQrcode(false);
            }
        } catch {
            toast.error(t('basicInfo.fetchStatusFailed'));
        } finally {
            setLoading(false);
        }
    }, [name, node_id]);

    // 刷新按钮：直接读取容器状态 + 本地二维码文件（零阻塞）
    const handleRefresh = async () => {
        setLoading(true);
        try {
            await Promise.all([fetchStats(), fetchQrcode()]);
        } catch {
            toast.error(t('basicInfo.refreshFailed'));
        } finally {
            setLoading(false);
        }
    };

    const fetchQrcode = useCallback(async () => {
        try {
            const data = await containerApi.getQR(name, node_id);
            if (data.status === 'logged_in') {
                isLoggedInRef.current = true;
                setQrLoggedIn(true);
                setQrNotice({ kind: '', text: '' });
                setShowQrcode(false);
                setQrcode('');
                setQrExpiresIn(null);
                setQrMeta(null);
                setQrRefreshing(false);
            } else if (data.status === 'wrong_account') {
                // 登错号（A1）：网关日志证明该实例实际登录的是另一个 uin。
                // 绝不给二维码 —— 这个窗口里的码属于别人的号，扫了只会把错号登得更实。
                setShowQrcode(true);
                setQrcode('');
                setQrExpiresIn(null);
                setQrMeta(null);
                setQrNotice({
                    kind: 'wrongaccount',
                    text: t('basicInfo.qrWrongAccount')
                        .replace('{actual}', data.actual_uin || '?')
                        .replace('{configured}', data.configured_uin || '?'),
                });
                setQrRefreshing(false);
            } else if (data.status === 'ok' && (data.url || data.image_base64)) {
                const url = qrImageUrl(data);
                setQrcode(url);
                setQrExpiresIn(data.expires_in ?? null);
                setQrMeta({
                    generated_at: data.generated_at,
                    fetched_at: data.fetched_at,
                    age_seconds: data.age_seconds,
                    expires_in: data.expires_in,
                    expires_at: data.expires_at,
                    source: data.source,
                    type: data.type,
                });
                setQrNotice({ kind: '', text: '' });
                setShowQrcode(true);
                setQrRefreshing(false);
            } else {
                // waiting/expired 等：区分原因给出可操作提示，不再让界面无限"取码中"
                setShowQrcode(true);
                setQrcode('');
                setQrExpiresIn(null);
                setQrMeta(null);
                const isOneclick = data.oneclick === true || data.source === 'snowluma_oneclick';
                setQrNotice(
                    isOneclick
                        ? { kind: 'oneclick', text: t('basicInfo.qrOneclickHint') }
                        // 聚合面板代理远端节点取码时会超时，路由层回退成
                        // source=remote_unavailable。这**不是**「登录窗口未就绪」，
                        // 说清楚原因用户才知道该重试而不是干等（2026-10-15 用户反馈）。
                        : data.source === 'remote_unavailable'
                            ? { kind: 'notready', text: t('basicInfo.qrRemoteTimeout') }
                            : { kind: 'notready', text: t('basicInfo.qrNotReady') },
                );
                setQrRefreshing(false);
            }
        } catch (e) {
            // 请求失败时仍显示二维码区域（等待/加载中），避免界面无反应。
            // 但要**如实**区分失败原因：过去一律报「登录窗口未就绪」，
            // 会把 429 限速/网络故障误报成 QQ 侧未就绪，误导排查方向。
            if (!isLoggedInRef.current) {
                const msg = e instanceof Error ? e.message : '';
                setShowQrcode(true);
                setQrNotice({
                    kind: 'notready',
                    text: msg.includes('429')
                        ? t('basicInfo.qrTooFrequent')
                        : t('basicInfo.qrFetchFailed'),
                });
            }
        }
    }, [name, node_id]);

    const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

    /** 轮询期间刷新一次容器状态，不动全局 loading（那是刷新按钮的转圈） */
    const syncStats = useCallback(async () => {
        try {
            const data = await containerApi.getStats(name, node_id);
            setStats(data);
            const loggedIn = !!(data.uin && data.uin !== '未登录 / Not Logged In');
            isLoggedInRef.current = loggedIn;
            if (loggedIn) setShowQrcode(false);
        } catch {
            // 状态引擎刚刷新时 stats 可能短暂不可用，下一轮再试
        }
    }, [name, node_id]);

    /**
     * 等操作跑完，结论只认后端 operation 的 phase。
     * 之前这里自己按 stats.status 轮询完就无条件报成功，结论可能和列表页完全相反。
     */
    const waitForOperation = async (operationId: string, action: string): Promise<ActionOutcome> => {
        const maxAttempts = action === 'restart' ? 40 : 25;
        for (let i = 0; i < maxAttempts; i++) {
            await wait(i === 0 ? 800 : 1500);
            if (!mountedRef.current) return 'pending';
            let phase = '';
            try {
                const data = await containerApi.getOperation(operationId);
                phase = data.operation?.phase || data.phase || '';
            } catch {
                return 'unknown';
            }
            await syncStats();
            const outcome = OPERATION_OUTCOMES[phase];
            if (outcome) return outcome;
        }
        return 'pending';
    };

    const handleAction = async (action: string, skipConfirm = false) => {
        if (action === 'delete') {
            setDeleteDialog({ open: true, deleteData: false });
            return;
        }
        if (action === 'logout' && !skipConfirm) {
            // 退出登录（仅 SnowLuma 虚拟账号）：把该账号的 QQ 配置整体移走（可逆备份），
            // 之后必须重新扫码 —— 属破坏性操作，必须二次确认。
            // 用应用内对话框替代原生 window.confirm：可国际化、可主题化、不阻塞 JS 线程。
            setLogoutDialog(true);
            return;
        }
        setActionLoading(action);
        const actionStart = Math.floor(Date.now() / 1000);
        if (action === 'start' || action === 'restart' || action === 'logout') {
            setShowQrcode(true);
            setQrcode('');
            setQrExpiresIn(null);
            setQrMeta(null);
            setQrRefreshing(true);
            setQrLoggedIn(false);
            setQrNotice({ kind: '', text: '' });
        }
        try {
            const accepted = await containerApi.action(name, action, node_id);
            const actionStartedAt = Math.floor(accepted.action_started_at || accepted.started_at || actionStart);
            const outcome: ActionOutcome = accepted.operation_id
                ? await waitForOperation(accepted.operation_id, action)
                : 'succeeded';
            if (!accepted.operation_id) await syncStats();
            const actionLabel = action === 'logout' ? '退出登录' : action;
            if (outcome === 'succeeded') toast.success(`${name} → ${actionLabel} ✓`);
            else if (outcome === 'failed') toast.error(`${name} → ${actionLabel} ✗ 操作失败`);
            else if (outcome === 'stuck') toast.warning(`${name} → ${actionLabel} 卡住了，请检查容器日志`);
            else if (outcome === 'pending') toast.warning(`${name} → ${actionLabel} 仍在执行，状态稍后自动刷新`);
            else toast.warning(`${name} → ${actionLabel} 结果未知，请手动确认容器状态`);
            // 只有确认重启成功才值得追新二维码；失败还挂着“刷新中”会一直转圈
            if (action === 'restart' && outcome === 'succeeded') {
                for (let i = 0; i < 6; i++) {
                    await wait(i === 0 ? 500 : 3000);
                    if (!mountedRef.current) return;
                    const data = await containerApi.getQR(name, node_id);
                    if (data.status === 'logged_in') {
                        isLoggedInRef.current = true;
                        setQrLoggedIn(true);
                        setQrNotice({ kind: '', text: '' });
                        setShowQrcode(false);
                        setQrcode('');
                        setQrExpiresIn(null);
                        setQrMeta(null);
                        setQrRefreshing(false);
                        return;
                    }
                    if (data.status === 'ok' && (data.url || data.image_base64) && (data.generated_at || 0) >= actionStartedAt) {
                        const url = qrImageUrl(data);
                        setQrcode(url);
                        setQrExpiresIn(data.expires_in ?? null);
                        setQrMeta({
                            generated_at: data.generated_at,
                            fetched_at: data.fetched_at,
                            age_seconds: data.age_seconds,
                            expires_in: data.expires_in,
                            expires_at: data.expires_at,
                            source: data.source,
                            type: data.type,
                        });
                        setShowQrcode(true);
                        setQrRefreshing(false);
                        return;
                    }
                }
                // 轮询用尽仍没等到新码：先把转圈收掉，交给常规轮询继续拉，别让界面一直卡在“刷新中”
                setQrRefreshing(false);
                fetchQrcode();
            } else {
                setQrRefreshing(false);
                fetchQrcode();
            }
        } catch (e) {
            toast.error(`${name} ${action} ✗`);
            setQrRefreshing(false);
            fetchStats();
        }
        finally { setActionLoading(''); }
    };

    const confirmDelete = async () => {
        setActionLoading('delete');
        try {
            await containerApi.action(name, 'delete', node_id, deleteDialog.deleteData);
            toast.success(`${name} deleted ✓`);
            const sp = new URLSearchParams(window.location.search);
            const nodeParam = sp.get('node');
            navigate(nodeParam ? `/admin?node=${nodeParam}` : '/admin');
        } catch (e) { toast.error(`${name} delete ✗`); }
        finally {
            setActionLoading('');
            setDeleteDialog({ open: false, deleteData: false });
        }
    };

    const openWebUI = () => {
        if (stats.webui_port && stats.webui_token) {
            // 本地节点用当前浏览器 hostname，远程节点后续通过节点地址替换
            const host = window.location.hostname;
            window.open(`http://${host}:${stats.webui_port}/webui/?token=${stats.webui_token}`, '_blank');
        }
    };

    useEffect(() => {
        fetchStats();
        fetchQrcode();

        let si: ReturnType<typeof setInterval>;
        let qi: ReturnType<typeof setInterval>;

        const startPolling = () => {
            clearInterval(si);
            clearInterval(qi);
            // 已登录：60s 状态巡检；未登录：15s 状态巡检 + 10s QR 实时拉取。
            // restart 成功后的短轮询在 handleAction 里额外执行，避免继续展示旧 QR。
            si = setInterval(fetchStats, isLoggedInRef.current ? 60000 : 15000);
            if (!isLoggedInRef.current) {
                qi = setInterval(fetchQrcode, 10000);
            }
        };
        const stopPolling = () => {
            clearInterval(si);
            clearInterval(qi);
        };
        const handleVisibility = () => {
            if (document.visibilityState === 'visible') {
                fetchStats();
                fetchQrcode();
                startPolling();
            } else {
                stopPolling();
            }
        };

        startPolling();
        document.addEventListener('visibilitychange', handleVisibility);
        return () => {
            stopPolling();
            document.removeEventListener('visibilitychange', handleVisibility);
        };
    }, [name, node_id, fetchStats, fetchQrcode]);

    const formatMB = (mb: number) => {
        if (!mb) return '-';
        if (mb < 1024) return `${mb.toFixed(1)} MB`;
        return `${(mb / 1024).toFixed(2)} GB`;
    };

    const cpuPct = Math.min(stats.cpu_percent || 0, 100);
    const memPct = stats.mem_limit ? Math.min((stats.mem_usage || 0) / stats.mem_limit * 100, 100) : 0;

    const isRunning = stats.status === 'running';
    // 登录态取两个来源的「或」：状态引擎的 uin，或二维码接口的 logged_in 结论。
    // SnowLuma 账号的 uin 要等状态引擎刷新（最长 4 分钟），只认 stats.uin 会导致
    // 「Bot 已经在正常刷日志、面板却一直显示待登录」。
    const statsLoggedIn = !!(stats.uin && stats.uin !== '未登录 / Not Logged In');
    const isLoggedIn = statsLoggedIn || qrLoggedIn;
    const qqNumber = stats.uin ? String(stats.uin).replace(/\D/g, '') : '';
    const avatarUrl = (isLoggedIn && qqNumber)
        ? `/api/resource/avatar/${qqNumber}`
        : "https://napneko.github.io/assets/newnewlogo.png";

    // 3色状态灯：灰=停止，蓝=运行待登录，绿=运行已登录
    // 一键登录：仅 SnowLuma 虚拟账号有效。
    // QQ 已记住账号时登录窗显示的是一键登录界面（不是二维码），此按钮让服务端
    // 先校验窗口类型、再勾「自动登录」并点「登录」。非 SnowLuma 账号会返回 400。
    const handleOneclickLogin = async () => {
        setActionLoading('oneclick');
        try {
            const res = await containerApi.oneclickLogin(name, node_id);
            // 结论只认后端给的 message（助手退出码契约翻译而来）：
            // 旧实现只判 ok，而「没有登录窗」时助手也返回 0 ⇒ 面板报成功、账号一步没动。
            const detail = res.message || res.reason || res.log || '未知原因';
            if (res.ok) {
                toast.success(`${name}：${res.message || '一键登录已触发'}`);
            } else {
                toast.warning(`一键登录未执行：${detail}`);
            }
            if (res.code === 4 || res.code === 5) {
                // rc=4：该号停在扫码登录界面，助手只刷新了二维码；
                // rc=5：该号是 680×440 双栏登录窗（左栏密码为空，一键登录无从下手）。
                // 两种都是「该扫码」，直接把码取回来给用户扫（双栏取码已在
                // snowluma-qr-extractor.py 里实现）。
                await fetchQrcode();
            }
            for (let i = 0; i < 6; i++) {
                await wait(i === 0 ? 1500 : 3000);
                try {
                    const data = await containerApi.getStats(name, node_id);
                    setStats(data);
                    if (data.uin && data.uin !== '未登录 / Not Logged In') {
                        isLoggedInRef.current = true;
                        setShowQrcode(false);
                        break;
                    }
                } catch { /* 状态引擎刷新中，继续轮询 */ }
            }
            await fetchStats();
        } catch (err) {
            toast.error(`一键登录失败：${err instanceof Error ? err.message : String(err)}`);
        } finally {
            setActionLoading('');
        }
    };

    const dotColor = !isRunning ? '#94a3b8' : isLoggedIn ? '#10b981' : '#3b82f6';
    const isDark = theme.palette.mode === 'dark';
    const glass = {
        background: isDark ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
        backdropFilter: 'blur(16px) saturate(1.2)',
        WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
        border: `1px solid ${isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
        boxShadow: isDark ? 'none' : '0 2px 12px rgba(0,0,0,0.06)',
    } as const;

    // 网络端点配置
    const netStats = [
        { label: t('basicInfo.netTotal'), value: Object.values(stats.network_endpoints || {}).reduce((a, b) => a + b, 0), icon: <HubIcon sx={{ fontSize: 18 }} />, color: '#6366f1' },
        { label: t('basicInfo.netHttp'), value: stats.network_endpoints?.http || 0, icon: <HttpIcon sx={{ fontSize: 18 }} />, color: '#10b981' },
        { label: t('basicInfo.netHttpClient'), value: stats.network_endpoints?.http_client || 0, icon: <CloudUploadIcon sx={{ fontSize: 18 }} />, color: '#3b82f6' },
        { label: t('basicInfo.netWs'), value: stats.network_endpoints?.ws || 0, icon: <CableIcon sx={{ fontSize: 18 }} />, color: '#8b5cf6' },
        { label: t('basicInfo.netWsClient'), value: stats.network_endpoints?.ws_client || 0, icon: <CloudDownloadIcon sx={{ fontSize: 18 }} />, color: '#ec4899' },
    ];

    return (
        <Box>
            {/* ── 操作按钮栏 ── */}
            <Box sx={{ ...glass, borderRadius: 3, p: 2, mb: 3, display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap', justifyContent: 'space-between' }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
                    <Box sx={{ position: 'relative' }}>
                        <Box component="img" src={avatarUrl} sx={{ width: 40, height: 40, borderRadius: '50%', objectFit: 'cover', filter: isRunning ? 'none' : 'grayscale(100%)', opacity: isRunning ? 1 : 0.6 }} />
                        <Box sx={{ position: 'absolute', bottom: 0, right: 0, width: 11, height: 11, borderRadius: '50%', bgcolor: dotColor, border: `2px solid ${isDark ? 'rgba(20,20,22,0.9)' : '#fff'}` }} />
                    </Box>
                    <Box>
                        <Typography variant="subtitle1" sx={{ fontWeight: 700, lineHeight: 1.2 }}>{name}</Typography>
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8 }}>
                            <Chip label={isRunning ? t('basicInfo.running') : (stats.status || t('basicInfo.unknown'))} size="small"
                                sx={{ height: 18, fontSize: '0.68rem', fontWeight: 600,
                                    bgcolor: isRunning ? 'rgba(16,185,129,0.12)' : 'rgba(148,163,184,0.12)',
                                    color: isRunning ? '#10b981' : '#64748b',
                                    border: `1px solid ${isRunning ? 'rgba(16,185,129,0.25)' : 'rgba(148,163,184,0.2)'}` }} />
                            {isLoggedIn && qqNumber && (
                                <Typography variant="caption" sx={{ color: 'text.secondary', fontFamily: 'monospace' }}>{qqNumber}</Typography>
                            )}
                        </Box>
                    </Box>
                </Box>
                <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                    {!isRunning ? (
                        <Button size="small" variant="contained" color="success" startIcon={<PlayArrowIcon />}
                            onClick={() => handleAction('start')} disabled={!!actionLoading}
                            sx={{ borderRadius: 2, boxShadow: 'none', textTransform: 'none', height: 34 }}>{t('basicInfo.start')}</Button>
                    ) : (<>
                        <Button size="small" variant="outlined" color="warning" startIcon={<StopIcon />}
                            onClick={() => handleAction('stop')} disabled={!!actionLoading}
                            sx={{ borderRadius: 2, textTransform: 'none', height: 34 }}>{t('basicInfo.stop')}</Button>
                        <Button size="small" variant="outlined" color="info" startIcon={<RestartAltIcon />}
                            onClick={() => handleAction('restart')} disabled={!!actionLoading}
                            sx={{ borderRadius: 2, textTransform: 'none', height: 34 }}>{t('basicInfo.restart')}</Button>
                    </>)}
                    {/* 一键登录：仅在运行中且未登录时出现（SnowLuma 已记住账号的场景） */}
                    {isRunning && !isLoggedIn && (
                        <Tooltip title={t('basicInfo.qrOneclickHint')}>
                            <Button size="small" variant="contained" color="primary" startIcon={<LoginIcon />}
                                onClick={handleOneclickLogin} disabled={!!actionLoading}
                                sx={{ borderRadius: 2, boxShadow: 'none', textTransform: 'none', height: 34 }}>
                                {t('basicInfo.oneclick')}
                            </Button>
                        </Tooltip>
                    )}
                    {/* 退出登录：仅在运行中且已登录时出现（SnowLuma 虚拟账号） */}
                    {isRunning && isLoggedIn && (
                        <Tooltip title={t('basicInfo.logoutConfirmBody').replace('{name}', name)}>
                            <Button size="small" variant="outlined" color="warning" startIcon={<LogoutIcon />}
                                onClick={() => handleAction('logout')} disabled={!!actionLoading}
                                sx={{ borderRadius: 2, textTransform: 'none', height: 34 }}>
                                {t('basicInfo.logout')}
                            </Button>
                        </Tooltip>
                    )}
                    {/* 注意：必须布尔化。webui_port 对 SnowLuma 虚拟账号恒为 0，
                        直接 `stats.webui_port && ...` 会短路成数字 0 并被 React 渲染成
                        文本「0」—— 就是之前一键登录右侧那个莫名其妙的 0。 */}
                    {!!stats.webui_port && !!stats.webui_token && (
                        <Button size="small" variant="contained" startIcon={<OpenInNewIcon />} onClick={openWebUI}
                            sx={{ borderRadius: 2, background: 'linear-gradient(135deg,#3b82f6,#2563eb)', boxShadow: '0 4px 14px rgba(59,130,246,0.3)', textTransform: 'none', height: 34, '&:hover': { background: 'linear-gradient(135deg,#2563eb,#1d4ed8)' } }}>
                            {t('basicInfo.webui')}
                        </Button>
                    )}
                    <Tooltip title={t('basicInfo.refreshTooltip')}>
                        <IconButton size="small" onClick={handleRefresh} disabled={loading}
                            sx={{ border: `1px solid ${isDark ? 'rgba(255,255,255,0.12)' : 'rgba(0,0,0,0.12)'}`, borderRadius: 2, width: 34, height: 34 }}>
                            {loading ? <CircularProgress size={16} /> : <RefreshIcon fontSize="small" />}
                        </IconButton>
                    </Tooltip>
                    {/* 删除容器：仅管理员可见（普通用户既看不到按钮，后端也会 403） */}
                    {isAdmin && (
                        <Button size="small" variant="outlined" color="error" startIcon={<DeleteOutlineIcon />}
                            onClick={() => handleAction('delete')} disabled={!!actionLoading}
                            sx={{ borderRadius: 2, textTransform: 'none', height: 34 }}>{t('basicInfo.delete')}</Button>
                    )}
                </Box>
            </Box>

            {/* 二维码登录区域 */}
            {showQrcode && !isLoggedIn && (
                <Box sx={{ ...glass, borderRadius: 3, p: 3, mb: 3,
                    border: '1px solid rgba(245,158,11,0.3)',
                    background: isDark ? 'rgba(245,158,11,0.06)' : 'rgba(255,251,235,0.7)' }}>
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 2 }}>
                        <Box sx={{ p: 1, borderRadius: 2, bgcolor: 'rgba(245,158,11,0.15)', display: 'flex' }}>
                            <QrCode2Icon sx={{ fontSize: 22, color: '#f59e0b' }} />
                        </Box>
                        <Box>
                            <Typography variant="subtitle1" sx={{ fontWeight: 700, color: '#f59e0b' }}>{t('basicInfo.qrLogin')}</Typography>
                            <Typography variant="caption" color="text.secondary">{t('basicInfo.qrLoginDesc')}</Typography>
                        </Box>
                    </Box>
                    <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}>
                        {qrRefreshing ? (
                            <Box sx={{ py: 4, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2 }}>
                                <CircularProgress size={36} thickness={4} sx={{ color: '#f59e0b' }} />
                                <Typography variant="body2" color="text.secondary">等待新二维码生成/刷新中</Typography>
                            </Box>
                        ) : qrcode ? (
                            <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1.2 }}>
                                <Box sx={{ p: 2, bgcolor: '#fff', borderRadius: 3, boxShadow: '0 8px 32px rgba(0,0,0,0.1)' }}>
                                    <img src={qrcode} alt="QR Code" style={{ width: 200, height: 200, display: 'block', borderRadius: 6 }} />
                                </Box>
                                <Typography variant="caption" sx={{ color: 'text.secondary', fontWeight: 600 }}>
                                    {formatQrAge(qrMeta?.generated_at, qrMeta?.age_seconds)}{qrMeta?.source ? ` · 来源 ${qrMeta.source}` : ''}
                                </Typography>
                                {qrExpiresIn !== null && (
                                    <Typography variant="caption" sx={{
                                        color: qrExpiresIn < 30 ? '#ef4444' : '#f59e0b',
                                        fontWeight: 600,
                                        fontFamily: 'monospace',
                                    }}>
                                        {qrExpiresIn > 0 ? `二维码剩余有效期 ${qrExpiresIn}s` : '二维码已过期，请刷新'}
                                    </Typography>
                                )}
                                <Button variant="text" size="small" startIcon={<RefreshIcon />} onClick={fetchQrcode}
                                    sx={{ color: '#f59e0b', fontWeight: 600, borderRadius: 2 }}>
                                    {t('basicInfo.refreshQr')}
                                </Button>
                            </Box>
                        ) : qrNotice.kind ? (
                            <Box sx={{ py: 3, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1.5, textAlign: 'center', maxWidth: 420 }}>
                                <WarningAmberIcon sx={{ fontSize: 34, color: '#f59e0b' }} />
                                <Typography variant="body2" color="text.secondary">{qrNotice.text}</Typography>
                                {qrNotice.kind === 'wrongaccount' ? (
                                    <Button size="small" variant="contained" color="warning" startIcon={<LogoutIcon />}
                                        onClick={() => handleAction('logout')} disabled={!!actionLoading}
                                        sx={{ borderRadius: 2, boxShadow: 'none', textTransform: 'none' }}>
                                        {t('basicInfo.logout')}
                                    </Button>
                                ) : qrNotice.kind === 'oneclick' ? (
                                    <Button size="small" variant="contained" color="primary" startIcon={<LoginIcon />}
                                        onClick={handleOneclickLogin} disabled={!!actionLoading}
                                        sx={{ borderRadius: 2, boxShadow: 'none', textTransform: 'none' }}>
                                        {t('basicInfo.oneclick')}
                                    </Button>
                                ) : (
                                    <Button variant="text" size="small" startIcon={<RefreshIcon />} onClick={fetchQrcode}
                                        sx={{ color: '#f59e0b', fontWeight: 600, borderRadius: 2 }}>
                                        {t('basicInfo.refreshQr')}
                                    </Button>
                                )}
                            </Box>
                        ) : (
                            <Box sx={{ py: 4, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2 }}>
                                <CircularProgress size={36} thickness={4} sx={{ color: '#f59e0b' }} />
                                <Typography variant="body2" color="text.secondary">{t('basicInfo.fetchingQr')}</Typography>
                            </Box>
                        )}
                    </Box>
                </Box>
            )}

            {/* ── 主信息面板（三列布局）── */}
            <Box sx={{ ...glass, borderRadius: 3, overflow: 'hidden', mb: 3 }}>
                <Grid container>
                    {/* ── 左列：头像 + 账号信息 ── */}
                    <Grid item xs={12} md={4} sx={{
                        p: 3,
                        borderRight: { md: `1px solid ${isDark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.06)'}` },
                        borderBottom: { xs: `1px solid ${isDark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.06)'}`, md: 'none' },
                    }}>
                        {/* 头像区 */}
                        <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', mb: 3 }}>
                            <Box sx={{ position: 'relative', mb: 1.5 }}>
                                <Box sx={{
                                    width: 88, height: 88, borderRadius: '50%', overflow: 'hidden',
                                    border: `3px solid ${isDark ? 'rgba(255,255,255,0.12)' : 'rgba(0,0,0,0.08)'}`,
                                    boxShadow: isRunning ? `0 0 0 4px ${dotColor}22` : 'none',
                                    transition: 'box-shadow 0.4s',
                                }}>
                                    <Box component="img" src={avatarUrl}
                                        sx={{ width: '100%', height: '100%', objectFit: 'cover', filter: isRunning ? 'none' : 'grayscale(80%)', opacity: isRunning ? 1 : 0.7 }} />
                                </Box>
                                <Box sx={{
                                    position: 'absolute', bottom: 2, right: 2, width: 16, height: 16,
                                    borderRadius: '50%', bgcolor: dotColor,
                                    border: `2.5px solid ${isDark ? 'rgba(18,18,20,0.95)' : '#f8f8f8'}`,
                                    boxShadow: `0 0 6px ${dotColor}88`,
                                }} />
                            </Box>
                            <Typography variant="h6" sx={{ fontWeight: 700, textAlign: 'center', lineHeight: 1.2 }}>
                                {name}
                            </Typography>
                            {qqNumber && (
                                <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace', mt: 0.3 }}>
                                    {qqNumber}
                                </Typography>
                            )}
                            <Box sx={{ mt: 1 }}>
                                <Chip
                                    label={isRunning ? (isLoggedIn ? t('basicInfo.running') : t('basicInfo.notLoggedIn')) : (stats.status || t('basicInfo.unknown'))}
                                    size="small"
                                    sx={{
                                        fontWeight: 700, fontSize: '0.72rem', height: 22,
                                        bgcolor: isRunning ? (isLoggedIn ? 'rgba(16,185,129,0.15)' : 'rgba(59,130,246,0.15)') : 'rgba(148,163,184,0.15)',
                                        color: isRunning ? (isLoggedIn ? '#10b981' : '#3b82f6') : '#94a3b8',
                                        border: `1px solid ${isRunning ? (isLoggedIn ? 'rgba(16,185,129,0.3)' : 'rgba(59,130,246,0.3)') : 'rgba(148,163,184,0.2)'}`,
                                    }}
                                />
                            </Box>
                        </Box>
                        {/* 信息列表 */}
                        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.2 }}>
                            <InfoRow label={t('basicInfo.napcatVersion')} value={stats.version || '-'} accent />
                            <InfoRow label={t('basicInfo.platform')} value={stats.platform || '-'} />
                            <InfoRow label={t('basicInfo.uptime')} value={stats.uptime_formatted || '-'} />
                            <InfoRow label={t('basicInfo.webuiPort')} value={stats.webui_port ? String(stats.webui_port) : '-'} />
                        </Box>
                    </Grid>

                    {/* ── 中列：CPU / 内存数值统计 ── */}
                    <Grid item xs={12} md={4} sx={{
                        p: 3,
                        borderRight: { md: `1px solid ${isDark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.06)'}` },
                        borderBottom: { xs: `1px solid ${isDark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.06)'}`, md: 'none' },
                    }}>
                        {/* CPU 区块 */}
                        <Box sx={{ mb: 3 }}>
                            <Typography variant="caption" sx={{ fontWeight: 800, letterSpacing: 1.5, textTransform: 'uppercase', color: '#3b82f6', display: 'block', mb: 1.5 }}>
                                CPU
                            </Typography>
                            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                                <StatRow label={t('basicInfo.cpuUsage')} value={`${cpuPct.toFixed(1)}%`} valueColor={cpuPct > 80 ? '#ef4444' : cpuPct > 50 ? '#f59e0b' : '#10b981'} />
                            </Box>
                        </Box>
                        {/* 内存 区块 */}
                        <Box>
                            <Typography variant="caption" sx={{ fontWeight: 800, letterSpacing: 1.5, textTransform: 'uppercase', color: '#10b981', display: 'block', mb: 1.5 }}>
                                {t('basicInfo.memory')}
                            </Typography>
                            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                                <StatRow label={t('basicInfo.memTotal')} value={formatMB(stats.mem_limit || 0)} />
                                <StatRow label={t('basicInfo.memUsed')} value={formatMB(stats.mem_usage || 0)} valueColor={memPct > 80 ? '#ef4444' : memPct > 60 ? '#f59e0b' : undefined} />
                            </Box>
                        </Box>
                    </Grid>

                    {/* ── 右列：圆形仪表盘 ── */}
                    <Grid item xs={12} md={4} sx={{ p: 3, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 3 }}>
                        <CircularGauge value={cpuPct} label={t('basicInfo.cpuUsage')} color="#3b82f6" trackColor={isDark ? 'rgba(59,130,246,0.12)' : 'rgba(59,130,246,0.08)'} />
                        <CircularGauge value={memPct} label={t('basicInfo.memUsage')} color="#ec4899" trackColor={isDark ? 'rgba(236,72,153,0.12)' : 'rgba(236,72,153,0.08)'} />
                    </Grid>
                </Grid>

                {/* ── 底部网络端点统计栏 ── */}
                <Box sx={{
                    display: 'flex', flexWrap: 'wrap',
                    borderTop: `1px solid ${isDark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.06)'}`,
                }}>
                    {netStats.map((s, i) => (
                        <Box key={i} sx={{
                            flex: '1 1 0', minWidth: 80, py: 1.8, px: 1, textAlign: 'center',
                            borderRight: i < netStats.length - 1 ? `1px solid ${isDark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.06)'}` : 'none',
                            transition: 'background 0.2s',
                            '&:hover': { background: isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.02)' },
                        }}>
                            <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 0.6, mb: 0.5, color: s.color }}>
                                {s.icon}
                                <Typography variant="h6" sx={{ fontWeight: 800, lineHeight: 1, color: s.color }}>{s.value}</Typography>
                            </Box>
                            <Typography variant="caption" color="text.secondary" sx={{ fontSize: '0.68rem', fontWeight: 600 }}>{s.label}</Typography>
                        </Box>
                    ))}
                </Box>
            </Box>

            {/* 删除确认对话框 */}
            <Dialog open={deleteDialog.open} onClose={() => setDeleteDialog({ ...deleteDialog, open: false })}
                PaperProps={{ sx: { borderRadius: 3, p: 1, minWidth: 420 } }}>
                <DialogTitle sx={{ fontWeight: 700, display: 'flex', alignItems: 'center', gap: 1 }}>
                    <WarningAmberIcon sx={{ color: '#ef4444' }} />
                    {t('basicInfo.confirmDeleteTitle')}
                </DialogTitle>
                <DialogContent>
                    <Typography variant="body2" sx={{ mb: 2 }}>
                        {t('basicInfo.deleteInstanceDesc').split('{name}')[0]}
                        <strong>{name}</strong>
                        {t('basicInfo.deleteInstanceDesc').split('{name}')[1]}
                    </Typography>
                    <FormControlLabel
                        control={
                            <Checkbox checked={deleteDialog.deleteData} color="error"
                                onChange={e => setDeleteDialog({ ...deleteDialog, deleteData: e.target.checked })} />
                        }
                        label={
                            <Box>
                                <Typography variant="body2" sx={{ fontWeight: 600 }}>{t('basicInfo.deleteWithData')}</Typography>
                                <Typography variant="caption" color="text.secondary">
                                    {t('basicInfo.deleteDataWarning').replace('{name}', name)}
                                </Typography>
                            </Box>
                        }
                    />
                </DialogContent>
                <DialogActions sx={{ p: 2, pt: 0 }}>
                    <Button onClick={() => setDeleteDialog({ ...deleteDialog, open: false })} color="inherit"
                        disabled={actionLoading === 'delete'} sx={{ borderRadius: 2 }}>{t('basicInfo.cancel')}</Button>
                    {/* 提交中禁用 + 转圈，避免双击发出两次 DELETE */}
                    <Button onClick={confirmDelete} variant="contained" color="error" disableElevation
                        disabled={actionLoading === 'delete'}
                        startIcon={actionLoading === 'delete' ? <CircularProgress size={14} color="inherit" /> : undefined}
                        sx={{ borderRadius: 2 }}>
                        {deleteDialog.deleteData ? t('basicInfo.deleteInstanceAndData') : t('basicInfo.deleteInstanceOnly')}
                    </Button>
                </DialogActions>
            </Dialog>

            {/* 退出登录确认对话框（替代原生 window.confirm） */}
            <Dialog open={logoutDialog} onClose={() => setLogoutDialog(false)} maxWidth="xs" fullWidth>
                <DialogTitle sx={{ fontWeight: 700 }}>{t('basicInfo.logoutConfirmTitle')}</DialogTitle>
                <DialogContent>
                    <Typography variant="body2" color="text.secondary">
                        {t('basicInfo.logoutConfirmBody').replace('{name}', name)}
                    </Typography>
                </DialogContent>
                <DialogActions sx={{ p: 2, pt: 0 }}>
                    <Button onClick={() => setLogoutDialog(false)} color="inherit"
                        disabled={actionLoading === 'logout'} sx={{ borderRadius: 2 }}>{t('common.cancel')}</Button>
                    <Button onClick={() => { setLogoutDialog(false); handleAction('logout', true); }}
                        variant="contained" color="warning" disableElevation
                        disabled={actionLoading === 'logout'}
                        startIcon={actionLoading === 'logout' ? <CircularProgress size={14} color="inherit" /> : undefined}
                        sx={{ borderRadius: 2 }}>
                        {t('basicInfo.logoutConfirmOk')}
                    </Button>
                </DialogActions>
            </Dialog>
        </Box>
    );
};

/** 左列：标签 + 值的横向行 */
const InfoRow = ({ label, value, accent }: { label: string; value: string; accent?: boolean }) => (
    <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', py: 0.8 }}>
        <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600, flexShrink: 0, mr: 1 }}>{label}</Typography>
        <Typography variant="caption" sx={{
            fontWeight: 700, fontFamily: 'monospace', textAlign: 'right',
            color: accent ? '#3b82f6' : 'text.primary',
            bgcolor: accent ? 'rgba(59,130,246,0.08)' : 'transparent',
            px: accent ? 1 : 0, borderRadius: 1,
        }}>{value}</Typography>
    </Box>
);

/** 中列：数值统计行 */
const StatRow = ({ label, value, valueColor }: { label: string; value: string; valueColor?: string }) => (
    <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        px: 1.5, py: 1, borderRadius: 2, bgcolor: 'rgba(128,128,128,0.04)',
        border: '1px solid rgba(128,128,128,0.06)' }}>
        <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>{label}</Typography>
        <Typography variant="caption" sx={{ fontWeight: 700, fontFamily: 'monospace', color: valueColor || 'text.primary' }}>{value}</Typography>
    </Box>
);

/** 右列：圆形仪表盘 */
const CircularGauge = ({ value, label, color, trackColor }: { value: number; label: string; color: string; trackColor: string }) => (
    <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1 }}>
        <Box sx={{ position: 'relative', display: 'inline-flex', width: 100, height: 100 }}>
            {/* 轨道 */}
            <CircularProgress variant="determinate" value={100} size={100} thickness={5}
                sx={{ color: trackColor, position: 'absolute', left: 0, top: 0 }} />
            {/* 进度 */}
            <CircularProgress variant="determinate" value={value} size={100} thickness={5}
                sx={{
                    color,
                    position: 'absolute', left: 0, top: 0,
                    filter: `drop-shadow(0 0 6px ${color}66)`,
                    '& .MuiCircularProgress-circle': { strokeLinecap: 'round' },
                }} />
            {/* 中心数值 */}
            <Box sx={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center' }}>
                <Typography sx={{ fontWeight: 800, fontSize: '1.2rem', lineHeight: 1, color }}>{Math.round(value)}</Typography>
                <Typography variant="caption" sx={{ fontWeight: 600, color, opacity: 0.8, fontSize: '0.6rem' }}>%</Typography>
            </Box>
        </Box>
        <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 700, letterSpacing: 0.5 }}>{label}</Typography>
    </Box>
);