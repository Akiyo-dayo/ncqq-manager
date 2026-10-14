import React, { useEffect, useState } from 'react';
import {
    Box, Typography, TextField, Button, CircularProgress, Card, CardContent, Grid, useTheme, Alert, Switch, FormControlLabel, Collapse, IconButton, Tooltip, Chip, Stack
} from '@mui/material';
import SaveIcon from '@mui/icons-material/Save';
import CableIcon from '@mui/icons-material/Cable';
import VpnKeyIcon from '@mui/icons-material/VpnKey';
import VisibilityIcon from '@mui/icons-material/Visibility';
import VisibilityOffIcon from '@mui/icons-material/VisibilityOff';
import ContentCopyIcon from '@mui/icons-material/ContentCopy';
import AutorenewIcon from '@mui/icons-material/Autorenew';
import AddCircleOutlineIcon from '@mui/icons-material/AddCircleOutline';
import RemoveCircleOutlineIcon from '@mui/icons-material/RemoveCircleOutline';
import { useTranslate } from '../i18n';
import { useToast } from '../components/Toast';
import { nodeApi } from '../services/api';

/**
 * 这里刻意不包含 api_key。
 *
 * GET /cluster/config 返回的是掩码后的 "***"，早期实现把整个 config 原样回传，
 * 于是保存一次就把真实集群密钥覆写成字面量 "***"，所有已加本机为节点的面板
 * 立刻 401 掉线。密钥改由「本机集群密钥」卡片单独读写。
 */
interface ClusterConfig {
    docker_image: string;
    container_keywords: string;
    webui_base_port: number;
    http_base_port: number;
    ws_base_port: number;
    data_dir: string;
    init_ws_client_enabled: boolean;
    init_ws_client_url: string;
    init_ws_client_token: string;
    init_auto_join_groups_enabled: boolean;
    init_auto_join_groups: string;
}

const DEFAULT_CONFIG: ClusterConfig = {
    docker_image: "mlikiowa/napcat-docker:latest",
    container_keywords: '["napcat"]',
    webui_base_port: 6000, http_base_port: 3000, ws_base_port: 3001,
    data_dir: "",
    init_ws_client_enabled: false,
    init_ws_client_url: "ws://127.0.0.1:5100/onebot/v11/ws",
    init_ws_client_token: "",
    init_auto_join_groups_enabled: false,
    init_auto_join_groups: "[]",
};

export default function ClusterSettings() {
    const theme = useTheme();
    const t = useTranslate();
    const toast = useToast();
    const [config, setConfig] = useState<ClusterConfig>(DEFAULT_CONFIG);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    // 自动加群通知：逐条列表，每个群号一项
    const [autoGroups, setAutoGroups] = useState<string[]>([]);

    // 本机集群密钥（与上面的 config 表单完全独立，避免又被整体回传覆写）
    const [clusterKey, setClusterKey] = useState('');
    const [keyVisible, setKeyVisible] = useState(false);
    const [keyLoading, setKeyLoading] = useState(true);
    const [keyResetting, setKeyResetting] = useState(false);

    useEffect(() => {
        const fetchConfig = async () => {
            try {
                const data = await nodeApi.getClusterConfig();
                const fetched = (data as Record<string, unknown>).config as Partial<ClusterConfig>;
                const merged = { ...DEFAULT_CONFIG, ...fetched };
                setConfig(merged);
                // 从 JSON 字符串还原自动加群群号
                try {
                    const grpArr = JSON.parse(merged.init_auto_join_groups || '[]');
                    setAutoGroups(Array.isArray(grpArr) ? grpArr.filter(Boolean) : []);
                } catch { setAutoGroups([]); }
            } catch (e) {
                console.error("Failed to fetch cluster config", e);
            } finally {
                setLoading(false);
            }
        };
        fetchConfig();
    }, []);

    useEffect(() => {
        nodeApi.getClusterKey()
            .then(res => setClusterKey(res.api_key || ''))
            .catch(e => console.error("Failed to fetch cluster key", e))
            .finally(() => setKeyLoading(false));
    }, []);

    const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        const value = (e.target.type === 'number') ? (parseInt(e.target.value) || 0) : e.target.value;
        setConfig({ ...config, [e.target.name]: value });
    };

    const handleToggle = (key: keyof ClusterConfig) => {
        setConfig(prev => ({ ...prev, [key]: !prev[key] }));
    };

    const handleSave = async () => {
        setSaving(true);
        try {
            // 逐字段显式列举而不是 {...config}：后端返回的额外字段（含掩码后的
            // api_key）绝不能顺着 state 被原样写回去。
            const payload = {
                docker_image: config.docker_image,
                container_keywords: config.container_keywords,
                webui_base_port: config.webui_base_port,
                http_base_port: config.http_base_port,
                ws_base_port: config.ws_base_port,
                data_dir: config.data_dir,
                init_ws_client_enabled: config.init_ws_client_enabled,
                init_ws_client_url: config.init_ws_client_url,
                init_ws_client_token: config.init_ws_client_token,
                init_auto_join_groups_enabled: config.init_auto_join_groups_enabled,
                init_auto_join_groups: JSON.stringify(autoGroups.filter(Boolean)),
            };
            await nodeApi.saveClusterConfig(payload);
            toast.success(t('config.saved') || 'Saved Successfully');
        } catch (e) {
            console.error(e);
            toast.error(t('config.saveFailed') || 'Save failed');
        } finally {
            setSaving(false);
        }
    };

    const handleCopyKey = async () => {
        if (!clusterKey) return;
        try {
            await navigator.clipboard.writeText(clusterKey);
            toast.success(t('已复制到剪贴板'));
        } catch {
            toast.error(t('复制失败，请手动选中复制'));
        }
    };

    const handleResetKey = async () => {
        // 两道确认：重置密钥会让所有把本机加为节点的面板立刻掉线
        if (!window.confirm(t('重置后，所有把本机加为节点的其它面板都会立即显示离线，必须手动更新新密钥。确定继续？'))) return;
        if (!window.confirm(t('再次确认：这一步不可撤销，旧密钥立即失效。'))) return;
        setKeyResetting(true);
        try {
            const res = await nodeApi.resetClusterKey();
            setClusterKey(res.api_key || '');
            setKeyVisible(true); // 重置后直接亮出来，方便立刻复制去更新其它面板
            toast.success(res.warning || t('集群密钥已重置'));
        } catch (e) {
            toast.error(String(e));
        } finally {
            setKeyResetting(false);
        }
    };

    if (loading) {
        return <Box sx={{ display: 'flex', justifyContent: 'center', p: 5 }}><CircularProgress /></Box>;
    }

    return (
        <Box sx={{ maxWidth: 900, mx: 'auto', p: { xs: 2, md: 4 } }}>
            <Box sx={{ mb: 4, pb: 2, borderBottom: `1px solid ${theme.palette.divider}` }}>
                <Typography variant="h4" sx={{ fontWeight: 800, color: 'text.primary', mb: 1 }}>
                    {t('clusterConfig.title')}
                </Typography>
                <Typography variant="body1" color="text.secondary">
                    {t('clusterConfig.description')}
                </Typography>
            </Box>

            <Card variant="outlined" sx={{
                borderRadius: 4,
                bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                backdropFilter: 'blur(16px) saturate(1.2)',
                WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
                boxShadow: theme.palette.mode === 'dark' ? 'none' : '0 2px 12px rgba(0,0,0,0.06)'
            }}>
                <CardContent sx={{ p: { xs: 3, md: 5 } }}>
                    <Grid container spacing={4}>
                        <Grid item xs={12}>
                            <Typography variant="subtitle2" sx={{ mb: 1.5, fontWeight: 600, color: 'text.primary' }}>
                                {t('clusterConfig.dockerImage')}
                            </Typography>
                            <TextField
                                fullWidth
                                name="docker_image"
                                value={config.docker_image || ''}
                                onChange={handleChange}
                                placeholder="mlikiowa/napcat-docker:latest"
                                helperText={t('clusterConfig.dockerImageHelp')}
                                size="medium"
                                sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2, bgcolor: theme.palette.mode === 'dark' ? 'rgba(0,0,0,0.2)' : '#fff' } }}
                            />
                        </Grid>

                        <Grid item xs={12}>
                            <Typography variant="subtitle2" sx={{ mb: 1.5, fontWeight: 600, color: 'text.primary' }}>
                                {t('clusterConfig.containerKeywords')}
                            </Typography>
                            <TextField
                                fullWidth
                                name="container_keywords"
                                value={config.container_keywords || '[]'}
                                onChange={handleChange}
                                placeholder='["napcat"]'
                                helperText={t('clusterConfig.containerKeywordsHelp')}
                                size="medium"
                                sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2, bgcolor: theme.palette.mode === 'dark' ? 'rgba(0,0,0,0.2)' : '#fff' } }}
                            />
                        </Grid>

                        <Grid item xs={12} md={4}>
                            <Typography variant="subtitle2" sx={{ mb: 1.5, fontWeight: 600, color: 'text.primary' }}>
                                {t('clusterConfig.webuiBasePort')}
                            </Typography>
                            <TextField
                                fullWidth
                                name="webui_base_port"
                                type="number"
                                value={config.webui_base_port}
                                onChange={handleChange}
                                helperText={t('clusterConfig.webuiBasePortHelp')}
                                size="medium"
                                sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2, bgcolor: theme.palette.mode === 'dark' ? 'rgba(0,0,0,0.2)' : '#fff' } }}
                            />
                        </Grid>

                        <Grid item xs={12} md={4}>
                            <Typography variant="subtitle2" sx={{ mb: 1.5, fontWeight: 600, color: 'text.primary' }}>
                                {t('clusterConfig.httpBasePort')}
                            </Typography>
                            <TextField
                                fullWidth
                                name="http_base_port"
                                type="number"
                                value={config.http_base_port}
                                onChange={handleChange}
                                helperText={t('clusterConfig.httpBasePortHelp')}
                                size="medium"
                                sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2, bgcolor: theme.palette.mode === 'dark' ? 'rgba(0,0,0,0.2)' : '#fff' } }}
                            />
                        </Grid>

                        <Grid item xs={12} md={4}>
                            <Typography variant="subtitle2" sx={{ mb: 1.5, fontWeight: 600, color: 'text.primary' }}>
                                {t('clusterConfig.wsBasePort')}
                            </Typography>
                            <TextField
                                fullWidth
                                name="ws_base_port"
                                type="number"
                                value={config.ws_base_port}
                                onChange={handleChange}
                                helperText={t('clusterConfig.wsBasePortHelp')}
                                size="medium"
                                sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2, bgcolor: theme.palette.mode === 'dark' ? 'rgba(0,0,0,0.2)' : '#fff' } }}
                            />
                        </Grid>

                        <Grid item xs={12}>
                            <Typography variant="subtitle2" sx={{ mb: 1.5, fontWeight: 600, color: 'text.primary' }}>
                                {t('clusterConfig.dataDirLabel')}
                            </Typography>
                            <TextField
                                fullWidth
                                name="data_dir"
                                value={config.data_dir}
                                onChange={handleChange}
                                helperText={t('clusterConfig.dataDirHelp')}
                                size="medium"
                                sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2, bgcolor: theme.palette.mode === 'dark' ? 'rgba(0,0,0,0.2)' : '#fff' } }}
                            />
                        </Grid>
                    </Grid>
                </CardContent>
            </Card>

            {/* ── 本机集群密钥 ── */}
            <Card variant="outlined" sx={{
                borderRadius: 4, mt: 3,
                bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                backdropFilter: 'blur(16px) saturate(1.2)',
                WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
                boxShadow: theme.palette.mode === 'dark' ? 'none' : '0 2px 12px rgba(0,0,0,0.06)'
            }}>
                <CardContent sx={{ p: { xs: 3, md: 5 } }}>
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 1.5 }}>
                        <VpnKeyIcon sx={{ color: '#f59e0b' }} />
                        <Typography variant="h6" sx={{ fontWeight: 700 }}>
                            {t('本机集群密钥')}
                        </Typography>
                    </Box>
                    <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                        {t('其它面板要把这台机器加为节点时，需要填这个密钥。')}
                    </Typography>

                    {keyLoading ? (
                        <CircularProgress size={22} />
                    ) : (
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                            <TextField
                                fullWidth
                                value={clusterKey}
                                type={keyVisible ? 'text' : 'password'}
                                InputProps={{ readOnly: true, sx: { fontFamily: 'monospace' } }}
                                size="small"
                                sx={{ flex: 1, minWidth: 260, '& .MuiOutlinedInput-root': { borderRadius: 2, bgcolor: theme.palette.mode === 'dark' ? 'rgba(0,0,0,0.2)' : '#fff' } }}
                            />
                            <Tooltip title={keyVisible ? t('隐藏') : t('显示')}>
                                <IconButton onClick={() => setKeyVisible(v => !v)}>
                                    {keyVisible ? <VisibilityOffIcon fontSize="small" /> : <VisibilityIcon fontSize="small" />}
                                </IconButton>
                            </Tooltip>
                            <Tooltip title={t('复制')}>
                                <span>
                                    <IconButton onClick={handleCopyKey} disabled={!clusterKey}>
                                        <ContentCopyIcon fontSize="small" />
                                    </IconButton>
                                </span>
                            </Tooltip>
                            <Button
                                variant="outlined" color="error" size="small"
                                startIcon={keyResetting ? <CircularProgress size={16} color="inherit" /> : <AutorenewIcon />}
                                onClick={handleResetKey}
                                disabled={keyResetting}
                                sx={{ borderRadius: 2, textTransform: 'none', whiteSpace: 'nowrap' }}
                            >
                                {t('重置密钥')}
                            </Button>
                        </Box>
                    )}

                    <Alert severity="warning" sx={{ borderRadius: 2, mt: 2 }}>
                        {t('重置密钥后，所有已经把本机添加为节点的其它面板都必须同步更新密钥，否则那些面板上本机会一直显示离线。')}
                    </Alert>
                </CardContent>
            </Card>

            {/* WS 客户端注入卡片 */}
            <Card variant="outlined" sx={{
                borderRadius: 4, mt: 3,
                bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                backdropFilter: 'blur(16px) saturate(1.2)',
                WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
                boxShadow: theme.palette.mode === 'dark' ? 'none' : '0 2px 12px rgba(0,0,0,0.06)'
            }}>
                <CardContent sx={{ p: { xs: 3, md: 5 } }}>
                    <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 2 }}>
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
                            <CableIcon color="primary" />
                            <Typography variant="h6" sx={{ fontWeight: 700 }}>
                                {t('clusterConfig.wsClientInitTitle')}
                            </Typography>
                        </Box>
                        <FormControlLabel
                            control={
                                <Switch
                                    checked={config.init_ws_client_enabled}
                                    onChange={() => handleToggle('init_ws_client_enabled')}
                                    color="primary"
                                />
                            }
                            label={t('clusterConfig.wsClientInitEnable')}
                            labelPlacement="start"
                            sx={{ mr: 0 }}
                        />
                    </Box>
                    <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                        {t('clusterConfig.wsClientInitDesc')}
                    </Typography>

                    <Collapse in={config.init_ws_client_enabled}>
                        <Grid container spacing={3} sx={{ mt: 0.5 }}>
                            <Grid item xs={12}>
                                <Typography variant="subtitle2" sx={{ mb: 1.5, fontWeight: 600 }}>
                                    {t('clusterConfig.wsClientUrl')}
                                </Typography>
                                <TextField
                                    fullWidth
                                    name="init_ws_client_url"
                                    value={config.init_ws_client_url}
                                    onChange={handleChange}
                                    placeholder="ws://127.0.0.1:5100/onebot/v11/ws"
                                    helperText={t('clusterConfig.wsClientUrlHelp')}
                                    size="medium"
                                    sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2, bgcolor: theme.palette.mode === 'dark' ? 'rgba(0,0,0,0.2)' : '#fff' } }}
                                />
                            </Grid>
                            <Grid item xs={12}>
                                <Typography variant="subtitle2" sx={{ mb: 1.5, fontWeight: 600 }}>
                                    {t('clusterConfig.wsClientToken')}
                                </Typography>
                                <TextField
                                    fullWidth
                                    name="init_ws_client_token"
                                    value={config.init_ws_client_token}
                                    onChange={handleChange}
                                    placeholder="optional"
                                    helperText={t('clusterConfig.wsClientTokenHelp')}
                                    size="medium"
                                    sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2, bgcolor: theme.palette.mode === 'dark' ? 'rgba(0,0,0,0.2)' : '#fff' } }}
                                />
                            </Grid>
                        </Grid>
                    </Collapse>
                </CardContent>
            </Card>

            {/* ── 自动加群通知设置 ── */}
            <Card elevation={0} sx={{
                borderRadius: 3, mt: 3, mb: 3,
                bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                backdropFilter: 'blur(16px) saturate(1.2)',
                WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
            }}>
                <CardContent>
                    <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 1 }}>
                        <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
                            {t('clusterConfig.autoJoinGroupsTitle')}
                        </Typography>
                        <FormControlLabel
                            control={
                                <Switch
                                    checked={config.init_auto_join_groups_enabled}
                                    onChange={() => handleToggle('init_auto_join_groups_enabled')}
                                    color="primary"
                                />
                            }
                            label={t('clusterConfig.autoJoinGroupsEnable')}
                            labelPlacement="start"
                            sx={{ mr: 0 }}
                        />
                    </Box>
                    <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                        {t('clusterConfig.autoJoinGroupsDesc')}
                    </Typography>
                    <Collapse in={config.init_auto_join_groups_enabled}>
                        <Stack spacing={1}>
                            {autoGroups.map((gid, idx) => (
                                <Box key={idx} sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                                    <Chip label={idx + 1} size="small" sx={{ minWidth: 28, fontWeight: 700 }} />
                                    <TextField
                                        fullWidth size="small"
                                        value={gid}
                                        onChange={e => {
                                            const next = [...autoGroups];
                                            next[idx] = e.target.value;
                                            setAutoGroups(next);
                                        }}
                                        placeholder={t('clusterConfig.autoJoinGroupsPlaceholder')}
                                        sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2, bgcolor: theme.palette.mode === 'dark' ? 'rgba(0,0,0,0.2)' : '#fff' } }}
                                    />
                                    <Tooltip title={t('common.delete') || 'Remove'}>
                                        <IconButton size="small" color="error"
                                            onClick={() => setAutoGroups(autoGroups.filter((_, i) => i !== idx))}>
                                            <RemoveCircleOutlineIcon fontSize="small" />
                                        </IconButton>
                                    </Tooltip>
                                </Box>
                            ))}
                        </Stack>
                        <Button
                            variant="outlined" size="small"
                            startIcon={<AddCircleOutlineIcon />}
                            onClick={() => setAutoGroups([...autoGroups, ''])}
                            sx={{ mt: 1, alignSelf: 'flex-start', borderRadius: 2, textTransform: 'none' }}
                        >
                            {t('clusterConfig.autoJoinGroupsAdd')}
                        </Button>
                    </Collapse>
                </CardContent>
            </Card>

            {/* 全局保存按钮 */}
            <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 4 }}>
                <Button
                    variant="contained"
                    startIcon={saving ? <CircularProgress size={20} color="inherit" /> : <SaveIcon />}
                    onClick={handleSave}
                    disabled={saving}
                    disableElevation
                    sx={{
                        borderRadius: 2,
                        px: 5,
                        py: 1.2,
                        fontWeight: 700,
                        background: 'linear-gradient(90deg, #2563eb, #3b82f6)',
                        '&:hover': {
                            boxShadow: '0 4px 12px rgba(37,99,235,0.4)',
                        }
                    }}
                >
                    {t('config.saveConfig') || 'Save'}
                </Button>
            </Box>
        </Box>
    );
}
