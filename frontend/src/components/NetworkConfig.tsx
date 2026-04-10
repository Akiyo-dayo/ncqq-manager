/**
 * NetworkConfig 组件 - 网络配置编辑器
 * 从 ConfigEditor.tsx 提取，保留原有 UI 风格
 */
import { useState, useEffect } from 'react';
import {
    Box, Typography, Button, TextField, Switch, FormControlLabel,
    Grid, IconButton, Divider, Card, CardContent, useTheme,
    Dialog, DialogTitle, DialogContent, DialogActions, Select, MenuItem, InputLabel, FormControl
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import SettingsIcon from '@mui/icons-material/Settings';
import HttpIcon from '@mui/icons-material/Http';
import CableIcon from '@mui/icons-material/Cable';
import SettingsInputComponentIcon from '@mui/icons-material/SettingsInputComponent';
import CloudUploadIcon from '@mui/icons-material/CloudUpload';
import CloudDownloadIcon from '@mui/icons-material/CloudDownload';
import RefreshIcon from '@mui/icons-material/Refresh';
import SensorsIcon from '@mui/icons-material/Sensors';
import { useTranslate } from '../i18n';
import { containerApi } from '../services/api';
import { useToast } from './Toast';

interface NetworkConfigProps {
    name: string;
    node_id: string;
}

interface EndpointConfig {
    name: string;
    enable: boolean;
    host?: string;
    port?: number;
    url?: string;
    [key: string]: unknown;
}

// 前端内部使用的扁平化网络配置
interface NapcatNetworkConfig {
    http?: EndpointConfig[];
    http_client?: EndpointConfig[];
    http_sse?: EndpointConfig[];
    ws?: EndpointConfig[];
    ws_client?: EndpointConfig[];
    [key: string]: EndpointConfig[] | undefined;
}

interface EditDialogState {
    open: boolean;
    isNew: boolean;
    type: string;
    index: number;
    data: EndpointConfig;
}

// NapCat onebot11_{uin}.json 中的字段名 ↔ 前端内部类型名映射
const NAPCAT_TO_UI: Record<string, string> = {
    httpServers: 'http',
    httpClients: 'http_client',
    httpSseServers: 'http_sse',
    websocketServers: 'ws',
    websocketClients: 'ws_client',
};
const UI_TO_NAPCAT: Record<string, string> = Object.fromEntries(
    Object.entries(NAPCAT_TO_UI).map(([k, v]) => [v, k])
);

/** 各端点类型的完整默认配置 — 严格对齐 onebot11_{uin}.json 格式 */
const ENDPOINT_DEFAULTS: Record<string, EndpointConfig> = {
    http: {
        name: '', enable: true, host: '0.0.0.0', port: 3000,
        enableCors: true, enableWebsocket: false, messagePostFormat: 'array',
        token: '', debug: false,
    },
    http_client: {
        name: '', enable: true, url: 'http://127.0.0.1:8080',
        reportSelfMessage: false, messagePostFormat: 'array',
        token: '', debug: false,
    },
    http_sse: {
        name: '', enable: true, host: '0.0.0.0', port: 3000,
        enableCors: true, enableWebsocket: false, messagePostFormat: 'array',
        token: '', debug: false, reportSelfMessage: false,
    },
    ws: {
        name: '', enable: true, host: '0.0.0.0', port: 3001,
        reportSelfMessage: false, enableForcePushEvent: false,
        messagePostFormat: 'array', token: '', debug: false,
        heartInterval: 30000,
    },
    ws_client: {
        name: '', enable: true, url: 'ws://127.0.0.1:8080',
        reportSelfMessage: false, messagePostFormat: 'array',
        token: '', debug: false, heartInterval: 30000,
        reconnectInterval: 30000,
    },
};

/** 从 onebot11 文件的 network 对象转换为前端扁平格式 */
function parseNetworkToUI(network: Record<string, unknown>): NapcatNetworkConfig {
    const result: NapcatNetworkConfig = {};
    for (const [napcatKey, uiKey] of Object.entries(NAPCAT_TO_UI)) {
        const arr = network[napcatKey];
        if (Array.isArray(arr) && arr.length > 0) {
            // 为每个端点补齐缺失字段（确保旧配置也有完整字段）
            const defaults = ENDPOINT_DEFAULTS[uiKey] || {};
            result[uiKey] = arr.map((item: EndpointConfig) => ({ ...defaults, ...item }));
        }
    }
    return result;
}

/** 将前端扁平格式转换回 onebot11 文件的 network 对象 */
function serializeUIToNetwork(config: NapcatNetworkConfig, originalNetwork?: Record<string, unknown>): Record<string, unknown> {
    // 保留原始 network 中的非端点字段（如 plugins）
    const network: Record<string, unknown> = originalNetwork ? { ...originalNetwork } : {};
    for (const [uiKey, napcatKey] of Object.entries(UI_TO_NAPCAT)) {
        network[napcatKey] = config[uiKey] || [];
    }
    return network;
}

export const NetworkConfig = ({ name, node_id }: NetworkConfigProps) => {
    const [config, setConfig] = useState<NapcatNetworkConfig | null>(null);
    const [loading, setLoading] = useState(false);
    // 保存完整的 onebot11 文件内容（含 musicSignUrl 等非网络字段）和文件名
    const [ob11FileName, setOb11FileName] = useState<string>('');
    const [ob11Extra, setOb11Extra] = useState<Record<string, unknown>>({});
    // 保留原始 network 对象中的非端点字段（如 plugins）
    const [ob11OriginalNetwork, setOb11OriginalNetwork] = useState<Record<string, unknown>>({});
    const [noConfigFile, setNoConfigFile] = useState(false);
    const t = useTranslate();
    const theme = useTheme();
    const toast = useToast();

    useEffect(() => {
        loadConfig();
    }, [name, node_id]);

    /** 查找 onebot11_{uin}.json 文件名 */
    const findOb11File = async (): Promise<string> => {
        try {
            const filesData = await containerApi.listFiles(name, 'config', node_id);
            if (filesData.status === 'ok' && filesData.files) {
                const ob11File = filesData.files
                    .map((f: { name: string }) => f.name)
                    .filter((n: string) => n.startsWith('onebot11_') && n.endsWith('.json'))
                    .sort()
                    .pop();
                if (ob11File) return ob11File;
            }
        } catch (error) {
            console.error('Failed to list config files:', error);
        }
        return '';
    };

    const loadConfig = async () => {
        setNoConfigFile(false);
        try {
            const fileName = await findOb11File();
            if (!fileName) {
                setNoConfigFile(true);
                setConfig({ http: [], http_client: [], http_sse: [], ws: [], ws_client: [] });
                return;
            }
            setOb11FileName(fileName);
            const data = await containerApi.getConfig(name, `config/${fileName}`, node_id);
            if (data.status === 'ok' && data.content) {
                const fullJson = JSON.parse(data.content);
                const network = fullJson.network || {};
                // 保留非 network 字段（musicSignUrl, enableLocalFile2Url 等）
                const { network: _net, ...extra } = fullJson;
                setOb11Extra(extra);
                setOb11OriginalNetwork(network);
                setConfig(parseNetworkToUI(network));
            } else {
                setNoConfigFile(true);
                setConfig({ http: [], http_client: [], http_sse: [], ws: [], ws_client: [] });
            }
        } catch (error) {
            console.error('Failed to load config:', error);
            setNoConfigFile(true);
            setConfig({ http: [], http_client: [], http_sse: [], ws: [], ws_client: [] });
        }
    };

    const [editDialog, setEditDialog] = useState<EditDialogState>({ open: false, isNew: false, type: 'http', index: -1, data: { name: '', enable: true } });

    const saveToServer = async (newConfig: NapcatNetworkConfig) => {
        if (!ob11FileName) {
            toast.error(t('network.noConfigFile'));
            return;
        }
        setLoading(true);
        try {
            // 重新组装完整的 onebot11 文件：保留原有非网络字段 + 更新 network
            const fullJson = {
                ...ob11Extra,
                network: serializeUIToNetwork(newConfig, ob11OriginalNetwork),
            };
            await containerApi.saveConfig(name, `config/${ob11FileName}`, JSON.stringify(fullJson, null, 2), node_id);
            toast.success(t('network.saveApplied'));
            setConfig(newConfig);
        } catch (error) {
            console.error('Failed to save config:', error);
            toast.error(t('network.saveRetryFailed'));
            loadConfig();
        } finally {
            setLoading(false);
        }
    };

    const handleSaveEndpoint = () => {
        const { type, index, isNew, data } = editDialog;
        const newConfig = { ...config };
        if (!newConfig[type]) newConfig[type] = [];

        if (isNew) {
            newConfig[type] = [...newConfig[type], data];
        } else {
            newConfig[type] = [...newConfig[type]];
            newConfig[type][index] = data;
        }

        setEditDialog({ ...editDialog, open: false });
        saveToServer(newConfig);
    };

    const handleDeleteEndpoint = () => {
        const { type, index } = editDialog;
        const newConfig = { ...config };
        if (newConfig[type]) {
            newConfig[type] = [...newConfig[type]];
            newConfig[type].splice(index, 1);
        }

        setEditDialog({ ...editDialog, open: false });
        saveToServer(newConfig);
    };

    const handleToggleEnable = (type: string, index: number, enable: boolean) => {
        const newConfig = { ...config };
        if (newConfig[type]) {
            newConfig[type] = [...newConfig[type]];
            newConfig[type][index] = { ...newConfig[type][index], enable };
        }
        saveToServer(newConfig);
    };

    const openAddDialog = () => {
        const defaults = ENDPOINT_DEFAULTS['http'];
        setEditDialog({ open: true, isNew: true, type: 'http', index: -1, data: { ...defaults, name: t('network.httpServer') } });
    };

    const openEditDialog = (type: string, index: number) => {
        const defaults = ENDPOINT_DEFAULTS[type] || {};
        const existing = config?.[type]?.[index] || {};
        setEditDialog({ open: true, isNew: false, type, index, data: { ...defaults, ...existing } });
    };

    const updateDialogData = (key: string, value: string | number | boolean) => {
        setEditDialog(prev => ({ ...prev, data: { ...prev.data, [key]: value } }));
    };

    const handleTypeChange = (newType: string) => {
        const defaults = ENDPOINT_DEFAULTS[newType] || {};
        const typeLabelMap: Record<string, string> = {
            http: t('network.httpServer'),
            http_client: t('network.httpClient'),
            http_sse: t('network.httpSseServer'),
            ws: t('network.wsServer'),
            ws_client: t('network.wsClient'),
        };
        setEditDialog(prev => ({ ...prev, type: newType, data: { ...defaults, name: typeLabelMap[newType] || t('network.newEndpoint') } }));
    };

    const endpointMeta = [
        { type: 'http', label: t('network.httpServer'), icon: <HttpIcon sx={{ color: '#10b981' }}/>, bg: 'rgba(16,185,129,0.1)' },
        { type: 'http_client', label: t('network.httpClient'), icon: <CloudUploadIcon sx={{ color: '#3b82f6' }}/>, bg: 'rgba(59,130,246,0.1)' },
        { type: 'http_sse', label: t('network.httpSseServer'), icon: <SensorsIcon sx={{ color: '#f59e0b' }}/>, bg: 'rgba(245,158,11,0.1)' },
        { type: 'ws', label: t('network.wsServer'), icon: <SettingsInputComponentIcon sx={{ color: '#8b5cf6' }}/>, bg: 'rgba(139,92,246,0.1)' },
        { type: 'ws_client', label: t('network.wsClient'), icon: <CloudDownloadIcon sx={{ color: '#ec4899' }}/>, bg: 'rgba(236,72,153,0.1)' }
    ];

    if (!config) {
        return (
            <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100%', py: 10 }}>
                <Typography color="text.secondary">{t('network.loading')}</Typography>
            </Box>
        );
    }

    if (noConfigFile) {
        return (
            <Box sx={{ display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', height: '100%', py: 10, gap: 2 }}>
                <Typography color="text.secondary">{t('network.notLoggedIn')}</Typography>
                <Button startIcon={<RefreshIcon />} onClick={loadConfig} variant="outlined" sx={{ borderRadius: 2, textTransform: 'none' }}>
                    {t('network.refresh')}
                </Button>
            </Box>
        );
    }

    const allEndpoints = endpointMeta.flatMap(meta =>
        (config[meta.type] || []).map((item: EndpointConfig, idx: number) => ({
            ...meta,
            item,
            index: idx
        }))
    );

    const isClientConfig = editDialog.type === 'http_client' || editDialog.type === 'ws_client';

    return (
        <Box>
            <Box sx={{
                display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3,
                p: 2.5, borderRadius: 3,
                background: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                backdropFilter: 'blur(16px) saturate(1.2)',
                WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
                boxShadow: theme.palette.mode === 'dark' ? 'none' : '0 2px 12px rgba(0,0,0,0.06)'
            }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                    <Box sx={{ p: 1, borderRadius: 2, bgcolor: 'rgba(59,130,246,0.1)', display: 'flex' }}>
                        <CableIcon sx={{ fontSize: 24, color: '#3b82f6' }} />
                    </Box>
                    <Box>
                        <Typography variant="h6" sx={{ fontWeight: 700, lineHeight: 1.2 }}>{t('network.title')}</Typography>
                        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>{t('network.subtitle')}</Typography>
                    </Box>
                </Box>
                <Box sx={{ display: 'flex', gap: 1 }}>
                    <Button
                        startIcon={<RefreshIcon />}
                        onClick={loadConfig}
                        disabled={loading}
                        variant="outlined"
                        sx={{ borderRadius: 2, textTransform: 'none', px: 2 }}
                    >
                        {t('network.refresh')}
                    </Button>
                    <Button
                        startIcon={<AddIcon />}
                        onClick={openAddDialog}
                        variant="contained"
                        sx={{ borderRadius: 2, background: 'linear-gradient(135deg, #3b82f6 0%, #2563eb 100%)', boxShadow: '0 4px 14px rgba(59,130,246,0.3)', textTransform: 'none', px: 3 }}
                    >
                        {t('network.create')}
                    </Button>
                </Box>
            </Box>

            {/* 端点卡片网格 */}
            <Grid container spacing={3}>
                {allEndpoints.length === 0 ? (
                    <Grid item xs={12}>
                        <Box sx={{ textAlign: 'center', py: 8, color: 'text.secondary' }}>
                            <Typography variant="body1">{t('network.noEndpoints')}</Typography>
                        </Box>
                    </Grid>
                ) : allEndpoints.map((endpoint, i) => (
                    <Grid item xs={12} md={6} lg={4} key={i}>
                        <Card sx={{
                            borderRadius: 3,
                            background: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                            backdropFilter: 'blur(16px) saturate(1.2)',
                            WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                            border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
                            boxShadow: theme.palette.mode === 'dark' ? 'none' : '0 2px 12px rgba(0,0,0,0.06)',
                            transition: 'box-shadow 0.2s',
                            '&:hover': {
                                boxShadow: theme.palette.mode === 'dark' ? '0 8px 32px rgba(0,0,0,0.2)' : '0 12px 32px rgba(0,0,0,0.06)',
                            }
                        }}>
                            <CardContent sx={{ p: 2.5, '&:last-child': { pb: 2.5 } }}>
                                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', mb: 2 }}>
                                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
                                        <Box sx={{ p: 1, borderRadius: 2, bgcolor: endpoint.bg, display: 'flex' }}>
                                            {endpoint.icon}
                                        </Box>
                                        <Box>
                                            <Typography variant="subtitle1" sx={{ fontWeight: 700, lineHeight: 1.2 }}>
                                                {endpoint.item.name || endpoint.label}
                                            </Typography>
                                            <Typography variant="caption" color="text.secondary">
                                                {endpoint.label}
                                            </Typography>
                                        </Box>
                                    </Box>
                                    <IconButton size="small" onClick={() => openEditDialog(endpoint.type, endpoint.index)}>
                                        <SettingsIcon fontSize="small" />
                                    </IconButton>
                                </Box>

                                <Divider sx={{ my: 1.5 }} />

                                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                                    {(endpoint.type === 'http_client' || endpoint.type === 'ws_client') ? (
                                        <Box sx={{ display: 'flex', alignItems: 'center' }}>
                                            <Typography variant="caption" color="text.secondary" sx={{ width: 60, flexShrink: 0 }}>URL:</Typography>
                                            <Typography variant="body2" sx={{ fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                {endpoint.item.url || t('network.notConfigured')}
                                            </Typography>
                                        </Box>
                                    ) : (
                                        <Box sx={{ display: 'flex', alignItems: 'center' }}>
                                            <Typography variant="caption" color="text.secondary" sx={{ width: 60, flexShrink: 0 }}>{t('network.address')}</Typography>
                                            <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                                                {endpoint.item.host || '0.0.0.0'}:{endpoint.item.port || 0}
                                            </Typography>
                                        </Box>
                                    )}
                                    <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mt: 0.5 }}>
                                        <Typography variant="caption" color="text.secondary">{t('network.status')}</Typography>
                                        <FormControlLabel
                                            control={<Switch size="small" checked={endpoint.item.enable || false} onChange={(e) => handleToggleEnable(endpoint.type, endpoint.index, e.target.checked)} color="primary" />}
                                            label={<Typography variant="caption" sx={{ fontWeight: 600 }}>{endpoint.item.enable ? t('network.enabled') : t('network.disabled')}</Typography>}
                                            sx={{ m: 0 }}
                                        />
                                    </Box>
                                </Box>
                            </CardContent>
                        </Card>
                    </Grid>
                ))}
            </Grid>

            {/* 新建/编辑弹窗 — 垂直单列表单，对齐 NapCat 原生 UI */}
            <Dialog open={editDialog.open} onClose={() => setEditDialog({ ...editDialog, open: false })} maxWidth="xs" fullWidth PaperProps={{ sx: { borderRadius: 3, backgroundImage: 'none' } }}>
                <DialogTitle sx={{ pb: 1, borderBottom: `1px solid ${theme.palette.divider}` }}>
                    {editDialog.isNew ? t('network.createEndpoint') : t('network.editEndpoint')}
                </DialogTitle>
                <DialogContent sx={{ pt: '16px !important', px: 2.5 }}>
                    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>

                        {/* 端点类型（仅新建） */}
                        {editDialog.isNew && (
                            <FormControl fullWidth size="small">
                                <InputLabel>{t('network.endpointType')}</InputLabel>
                                <Select value={editDialog.type} label={t('network.endpointType')} onChange={(e) => handleTypeChange(e.target.value)} sx={{ borderRadius: 2 }}>
                                    {endpointMeta.map((m) => (
                                        <MenuItem key={m.type} value={m.type}>
                                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>{m.icon} {m.label}</Box>
                                        </MenuItem>
                                    ))}
                                </Select>
                            </FormControl>
                        )}

                        {/* 启用 */}
                        <FormControlLabel
                            sx={{ mx: 0, justifyContent: 'space-between' }}
                            labelPlacement="start"
                            label={<Typography variant="body2">{t('network.enable')}</Typography>}
                            control={<Switch checked={editDialog.data?.enable !== false} onChange={(e) => updateDialogData('enable', e.target.checked)} />}
                        />

                        {/* Debug */}
                        <FormControlLabel
                            sx={{ mx: 0, justifyContent: 'space-between' }}
                            labelPlacement="start"
                            label={<Typography variant="body2">{t('network.debug')}</Typography>}
                            control={<Switch checked={editDialog.data?.debug === true} onChange={(e) => updateDialogData('debug', e.target.checked)} />}
                        />

                        {/* 名称 */}
                        <TextField fullWidth size="small" label={t('network.name')} placeholder={t('network.namePlaceholder')} value={editDialog.data?.name || ''} onChange={(e) => updateDialogData('name', e.target.value)} sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2 } }} />

                        {/* Client → URL / Server → Host + Port */}
                        {isClientConfig ? (
                            <TextField fullWidth size="small" label="URL" placeholder={editDialog.type === 'ws_client' ? 'ws://127.0.0.1:8080' : 'http://127.0.0.1:8080'} value={editDialog.data?.url || ''} onChange={(e) => updateDialogData('url', e.target.value)} sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2 } }} />
                        ) : (
                            <>
                                <TextField fullWidth size="small" label="Host" placeholder="0.0.0.0" value={editDialog.data?.host || ''} onChange={(e) => updateDialogData('host', e.target.value)} sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2 } }} />
                                <TextField fullWidth size="small" label="Port" type="number" placeholder="3000" value={editDialog.data?.port ?? ''} onChange={(e) => updateDialogData('port', parseInt(e.target.value) || 0)} sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2 } }} />
                            </>
                        )}

                        {/* 上报自身消息（除 HTTP Server 外） */}
                        {editDialog.type !== 'http' && (
                            <FormControlLabel
                                sx={{ mx: 0, justifyContent: 'space-between' }}
                                labelPlacement="start"
                                label={<Typography variant="body2">{t('network.reportSelfMessage')}</Typography>}
                                control={<Switch checked={editDialog.data?.reportSelfMessage === true} onChange={(e) => updateDialogData('reportSelfMessage', e.target.checked)} />}
                            />
                        )}

                        {/* 强制推送事件（仅 WS Server） */}
                        {editDialog.type === 'ws' && (
                            <FormControlLabel
                                sx={{ mx: 0, justifyContent: 'space-between' }}
                                labelPlacement="start"
                                label={<Typography variant="body2">{t('network.enableForcePushEvent')}</Typography>}
                                control={<Switch checked={editDialog.data?.enableForcePushEvent === true} onChange={(e) => updateDialogData('enableForcePushEvent', e.target.checked)} color={editDialog.data?.enableForcePushEvent ? 'error' : 'default'} />}
                            />
                        )}

                        {/* 启用CORS（HTTP Server / HTTP SSE Server） */}
                        {(editDialog.type === 'http' || editDialog.type === 'http_sse') && (
                            <FormControlLabel
                                sx={{ mx: 0, justifyContent: 'space-between' }}
                                labelPlacement="start"
                                label={<Typography variant="body2">{t('network.enableCors')}</Typography>}
                                control={<Switch checked={editDialog.data?.enableCors === true} onChange={(e) => updateDialogData('enableCors', e.target.checked)} color={editDialog.data?.enableCors ? 'error' : 'default'} />}
                            />
                        )}

                        {/* 启用Websocket（HTTP Server / HTTP SSE Server） */}
                        {(editDialog.type === 'http' || editDialog.type === 'http_sse') && (
                            <FormControlLabel
                                sx={{ mx: 0, justifyContent: 'space-between' }}
                                labelPlacement="start"
                                label={<Typography variant="body2">{t('network.enableWs')}</Typography>}
                                control={<Switch checked={editDialog.data?.enableWebsocket === true} onChange={(e) => updateDialogData('enableWebsocket', e.target.checked)} />}
                            />
                        )}

                        {/* 消息格式 */}
                        <FormControl fullWidth size="small">
                            <InputLabel>{t('network.messagePostFormat')}</InputLabel>
                            <Select value={String(editDialog.data?.messagePostFormat || 'array')} label={t('network.messagePostFormat')} onChange={(e) => updateDialogData('messagePostFormat', e.target.value)} sx={{ borderRadius: 2 }}>
                                <MenuItem value="array">Array</MenuItem>
                                <MenuItem value="string">String</MenuItem>
                            </Select>
                        </FormControl>

                        {/* Token */}
                        <TextField fullWidth size="small" label="Token" placeholder={t('network.tokenPlaceholder')} value={String(editDialog.data?.token || '')} onChange={(e) => updateDialogData('token', e.target.value)} sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2 } }} />

                        {/* 心跳间隔（WS Server / WS Client） */}
                        {(editDialog.type === 'ws' || editDialog.type === 'ws_client') && (
                            <TextField fullWidth size="small" label={t('network.heartInterval')} type="number" placeholder="30000" value={editDialog.data?.heartInterval ?? 30000} onChange={(e) => updateDialogData('heartInterval', parseInt(e.target.value) || 0)} sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2 } }} />
                        )}

                        {/* 重连间隔（仅 WS Client） */}
                        {editDialog.type === 'ws_client' && (
                            <TextField fullWidth size="small" label={t('network.reconnectInterval')} type="number" placeholder="30000" value={editDialog.data?.reconnectInterval ?? 30000} onChange={(e) => updateDialogData('reconnectInterval', parseInt(e.target.value) || 0)} sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2 } }} />
                        )}
                    </Box>
                </DialogContent>
                <DialogActions sx={{ p: 2, pt: 1.5, justifyContent: 'space-between', borderTop: `1px solid ${theme.palette.divider}` }}>
                    {!editDialog.isNew ? (
                        <Button color="error" startIcon={<DeleteOutlineIcon />} onClick={handleDeleteEndpoint} sx={{ borderRadius: 2, textTransform: 'none' }}>
                            {t('network.delete')}
                        </Button>
                    ) : <Box />}
                    <Box sx={{ display: 'flex', gap: 1 }}>
                        <Button onClick={() => setEditDialog({ ...editDialog, open: false })} color="inherit" sx={{ borderRadius: 2, textTransform: 'none' }}>
                            {t('network.cancel')}
                        </Button>
                        <Button onClick={handleSaveEndpoint} variant="contained" color="primary" sx={{ borderRadius: 2, textTransform: 'none', px: 3, boxShadow: '0 4px 10px rgba(59,130,246,0.2)' }}>
                            {t('network.save')}
                        </Button>
                    </Box>
                </DialogActions>
            </Dialog>
        </Box>
    );
};

