import React, { useEffect, useState, useContext, useCallback } from 'react';
import { Box, Typography, IconButton, List, ListItem, ListItemButton, ListItemIcon, ListItemText, Drawer, useTheme } from '@mui/material';
import { useNavigate, Outlet, useLocation } from 'react-router-dom';
import PublicIcon from '@mui/icons-material/Public';
import NapCatIcon from '../components/NapCatIcon';
import DashboardIcon from '@mui/icons-material/Dashboard';
import StorageIcon from '@mui/icons-material/Storage';
import ExitToAppIcon from '@mui/icons-material/ExitToApp';
import Brightness4Icon from '@mui/icons-material/Brightness4';
import Brightness7Icon from '@mui/icons-material/Brightness7';
import TranslateIcon from '@mui/icons-material/Translate';
import SettingsIcon from '@mui/icons-material/Settings';
import HubIcon from '@mui/icons-material/Hub';
import HistoryIcon from '@mui/icons-material/History';
import PeopleIcon from '@mui/icons-material/People';
import ImageIcon from '@mui/icons-material/Image';
import NotificationsActiveIcon from '@mui/icons-material/NotificationsActive';
import BackupIcon from '@mui/icons-material/Backup';
import ScheduleIcon from '@mui/icons-material/Schedule';
import PetsIcon from '@mui/icons-material/Pets';
import TrackChangesIcon from '@mui/icons-material/TrackChanges';
import { ThemeModeContext, LanguageContext } from '../App';
import { useTranslate } from '../i18n';
import { containerApi, authApi, type Container } from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import { useWebSocket } from '../hooks/useWebSocket';
import { useToast } from '../components/Toast';
import FiberManualRecordIcon from '@mui/icons-material/FiberManualRecord';

const drawerWidth = 280;

export default function AdminLayout() {
    const navigate = useNavigate();
    const location = useLocation();
    const theme = useTheme();
    const colorMode = useContext(ThemeModeContext);
    const { toggleLanguage } = useContext(LanguageContext);
    const t = useTranslate();
    const [containers, setContainers] = useState<Container[]>([]);
    const toast = useToast();
    const { isAdmin } = useAuth();
    const [bgUrl, setBgUrl] = useState('');

    // WS 驱动容器列表（替代 HTTP 轮询，后端 3s 推送一次含 uin）
    const {
        data: wsData,
        connected: wsConnected,
        reconnectAttempt: wsReconnectAttempt,
        lastDisconnectReason: wsLastDisconnectReason,
    } = useWebSocket<{ type: string; data: Container[] }>({
        path: '/ws/events',
    });

    // WS 推送到达时同步 containers state
    useEffect(() => {
        if (wsData?.type === 'containers' && Array.isArray(wsData.data)) {
            setContainers(wsData.data);
        }
    }, [wsData]);

    // 手动刷新（操作后立即反馈，不等 WS 3s 推送）
    const refreshContainers = useCallback(async () => {
        try {
            const data = await containerApi.list();
            setContainers(data.containers || []);
        } catch {
            toast.error(t('admin.refreshContainersFailed'));
        }
    }, []);

    // WS 未连接时回退到 HTTP 轮询（首次加载 + 断线容灾，指数退避 5s→60s）
    useEffect(() => {
        if (wsConnected) return;
        let timer: ReturnType<typeof setTimeout>;
        let delay = 5000;
        const poll = () => {
            refreshContainers();
            delay = Math.min(delay * 1.5, 60000);
            timer = setTimeout(poll, delay);
        };
        refreshContainers();
        timer = setTimeout(poll, delay);
        return () => clearTimeout(timer);
    }, [wsConnected, refreshContainers]);

    // 加载管理员后台背景壁纸
    useEffect(() => {
        let cancelled = false;
        let picked: { landscape: string; portrait: string } | null = null;
        const pick = (list: string[]) => list.length ? list[Math.floor(Math.random() * list.length)] : '';
        const applyOrientation = () => {
            if (!picked) return;
            const isLandscape = window.innerWidth >= window.innerHeight;
            const url = isLandscape ? (picked.landscape || picked.portrait) : (picked.portrait || picked.landscape);
            if (url) setBgUrl(url);
        };
        (async () => {
            try {
                const res = await fetch('/api/resource/wallpapers?category=admin');
                const json = await res.json();
                if (cancelled || json.status !== 'ok') return;
                picked = { landscape: pick(json.landscape || []), portrait: pick(json.portrait || []) };
                applyOrientation();
            } catch { /* ignore */ }
        })();
        const onResize = () => applyOrientation();
        window.addEventListener('resize', onResize);
        return () => { cancelled = true; window.removeEventListener('resize', onResize); };
    }, []);

    const handleLogout = async () => {
        try { await authApi.logout(); } catch { /* ignore */ }
        navigate('/login');
    };

    return (
        <Box sx={{ display: 'flex', minHeight: '100vh', position: 'relative' }}>
            {/* 管理员后台背景壁纸：zIndex:-1 确保穿透所有 fixed stacking context（包括 Drawer paper） */}
            {bgUrl && (
                <Box aria-hidden="true" sx={{
                    position: 'fixed', inset: 0, zIndex: -1,
                    backgroundImage: `url(${bgUrl})`,
                    backgroundSize: 'cover', backgroundPosition: 'center',
                    opacity: theme.palette.mode === 'dark' ? 0.12 : 0.18,
                    pointerEvents: 'none',
                }} />
            )}
            {/* Sidebar */}
            <Drawer
                variant="permanent"
                sx={{
                    width: drawerWidth,
                    flexShrink: 0,
                    position: 'relative', zIndex: 1,
                    '& .MuiDrawer-paper': {
                        width: drawerWidth,
                        boxSizing: 'border-box',
                        // 不再在 paper 上重复绘制壁纸；通过半透明背景 + backdropFilter 复用根层全屏壁纸
                        backgroundImage: 'none',
                        backgroundColor: theme.palette.mode === 'dark'
                            ? 'rgba(30,30,32,0.35)'
                            : 'rgba(255,255,255,0.25)',
                        backdropFilter: 'blur(16px) saturate(1.2)',
                        WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                        borderRight: 'none',
                    },
                }}
            >
                <Box sx={{ p: 3, display: 'flex', alignItems: 'center', gap: 2, mb: 2 }}>
                    <Box sx={{ p: 0.5, borderRadius: 2, bgcolor: '#fff', display: 'flex' }}>
                        <NapCatIcon fontSize="medium" />
                    </Box>
                    <Box>
                        <Typography variant="subtitle1" sx={{ fontWeight: 700, lineHeight: 1.2 }}>{t('admin.title')}</Typography>
                        <Typography variant="caption" color="text.secondary">{t('admin.subtitle')}</Typography>
                    </Box>
                </Box>
                <Box sx={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
                    <List component="nav" sx={{ px: 2, py: 2 }}>
                        {([
                            { path: '/admin', icon: <DashboardIcon />, label: t('admin.managedInstances'), adminOnly: false },
                            { path: '/admin/cluster-settings', icon: <SettingsIcon />, label: t('admin.instanceSettings'), adminOnly: true },
                            { path: '/admin/nodes', icon: <HubIcon />, label: t('admin.nodes'), adminOnly: true },
                            { path: '/admin/users', icon: <PeopleIcon />, label: t('admin.userManagement'), adminOnly: true },
                            { path: '/admin/operation-logs', icon: <HistoryIcon />, label: t('admin.operationLogs'), adminOnly: true },
                            { path: '/admin/images', icon: <ImageIcon />, label: t('admin.imageManager'), adminOnly: true },
                            { path: '/admin/alerts', icon: <NotificationsActiveIcon />, label: t('admin.alerts'), adminOnly: true },
                            { path: '/admin/backup', icon: <BackupIcon />, label: t('admin.backup'), adminOnly: true },
                            { path: '/admin/scheduler', icon: <ScheduleIcon />, label: t('admin.scheduler'), adminOnly: true },
                            { path: '/admin/botshepherd', icon: <PetsIcon />, label: t('admin.botshepherd'), adminOnly: true },
                            { path: '/admin/bot-radar', icon: <TrackChangesIcon />, label: t('admin.botRadar'), adminOnly: true },
                        ] as { path: string; icon: React.ReactNode; label: string; adminOnly: boolean }[])
                        .filter(item => !item.adminOnly || isAdmin)
                        .map(item => {
                            const isActive = location.pathname === item.path;
                            return (
                                <ListItem disablePadding sx={{ mb: 1 }} key={item.path}>
                                    <ListItemButton
                                        selected={isActive}
                                        onClick={() => navigate(item.path)}
                                        sx={{ borderRadius: 2, '&.Mui-selected': { bgcolor: 'rgba(59, 130, 246, 0.15)', '&:hover': { bgcolor: 'rgba(59, 130, 246, 0.25)' } } }}
                                    >
                                        <ListItemIcon sx={{ minWidth: 40, color: isActive ? '#60a5fa' : 'text.secondary' }}>{item.icon}</ListItemIcon>
                                        <ListItemText primary={item.label} primaryTypographyProps={{ fontSize: '0.875rem', fontWeight: isActive ? 600 : 500, color: isActive ? '#60a5fa' : 'text.primary' }} />
                                    </ListItemButton>
                                </ListItem>
                            );
                        })}

                    </List>
                </Box>
                <Box sx={{ flexShrink: 0, p: 2 }}>
                    <ListItem disablePadding sx={{ mb: 1 }}>
                        <ListItemButton onClick={() => navigate('/')} sx={{ borderRadius: 2 }}>
                            <ListItemIcon sx={{ minWidth: 40 }}><PublicIcon sx={{ color: 'text.secondary' }} /></ListItemIcon>
                            <ListItemText primary={t('admin.userSpaceBoard')} primaryTypographyProps={{ fontSize: '0.875rem', fontWeight: 500, color: 'text.secondary' }} />
                        </ListItemButton>
                    </ListItem>
                    <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', px: 2, pb: 1 }}>
                        {/* WS 连接状态指示 */}
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                            <FiberManualRecordIcon sx={{ fontSize: 10, color: wsConnected ? '#22c55e' : '#ef4444' }} />
                            <Typography variant="caption" color="text.secondary" sx={{ fontSize: '0.7rem' }}>
                                {wsConnected ? t('admin.wsConnected') : t('admin.wsDisconnected')}
                            </Typography>
                            {!wsConnected && wsReconnectAttempt > 0 && (
                                <Typography variant="caption" color="text.secondary" sx={{ fontSize: '0.7rem' }}>
                                    {`(${t('admin.wsRetry')}: ${wsReconnectAttempt})`}
                                </Typography>
                            )}
                            {!wsConnected && wsLastDisconnectReason && (
                                <Typography variant="caption" color="text.secondary" sx={{ fontSize: '0.7rem' }}>
                                    {`- ${t(`admin.wsDisconnectReason.${wsLastDisconnectReason}`)}`}
                                </Typography>
                            )}
                        </Box>
                        <Box>
                            <IconButton onClick={toggleLanguage} size="small" sx={{ mr: 1 }} aria-label="Toggle language">
                                <TranslateIcon fontSize="small" />
                            </IconButton>
                            <IconButton onClick={colorMode.toggleTheme} size="small" sx={{ mr: 1 }} aria-label="Toggle theme">
                                {theme.palette.mode === 'dark' ? <Brightness7Icon fontSize="small" /> : <Brightness4Icon fontSize="small" />}
                            </IconButton>
                            <IconButton onClick={handleLogout} size="small" sx={{ color: 'error.main' }}>
                                <ExitToAppIcon fontSize="small" />
                            </IconButton>
                        </Box>
                    </Box>
                </Box>
            </Drawer>

            {/* Main content Area */}
            <Box component="main" sx={{ flexGrow: 1, p: 0, bgcolor: 'transparent', minHeight: '100vh', overflow: 'auto', position: 'relative', zIndex: 1 }}>
                <Outlet context={{ containers, refreshContainers }} />
            </Box>
        </Box>
    );
}
