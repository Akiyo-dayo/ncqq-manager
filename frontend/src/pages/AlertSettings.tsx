import { useState, useEffect } from 'react';
import {
    Box, Typography, Button, TextField, Paper, IconButton, Switch,
    useTheme, CircularProgress, Dialog, DialogTitle, DialogContent,
    DialogActions, Select, MenuItem, Chip, List, ListItem, ListItemText,
    FormControl, InputLabel, Card, CardContent, FormControlLabel, Stack,
    Checkbox, OutlinedInput, ListItemIcon,
} from '@mui/material';
import NotificationsActiveIcon from '@mui/icons-material/NotificationsActive';
import AddIcon from '@mui/icons-material/Add';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';

import { alertApi, botApi, type AlertRule, type AlertHistory, type BotStatusItem } from '../services/api';
import { useTranslate } from '../i18n';

interface QqBotTarget { msg_type: string; target_id: string; }

const EMPTY_FORM = {
    name: '',
    type: 'container_stop',
    webhook_url: '',
};

const EMPTY_SENTINEL = {
    selectedNames: [] as string[],   // 存容器名（与后端 napcat_ws_service._table key 一致）
    targets: [{ msg_type: 'private', target_id: '' }] as QqBotTarget[],
};

export default function AlertSettings() {
    const theme = useTheme();
    const t = useTranslate();
    const [rules, setRules] = useState<AlertRule[]>([]);
    const [history, setHistory] = useState<AlertHistory[]>([]);
    const [loading, setLoading] = useState(true);
    const [createOpen, setCreateOpen] = useState(false);
    const [form, setForm] = useState({ ...EMPTY_FORM });
    const [allowAllIp, setAllowAllIp] = useState(false);
    // 哨兵专用 Dialog
    const [sentinelOpen, setSentinelOpen] = useState(false);
    const [sentinelForm, setSentinelForm] = useState({ ...EMPTY_SENTINEL });
    const [onlineBots, setOnlineBots] = useState<BotStatusItem[]>([]);

    const fetchData = async () => {
        try {
            const [rulesData, histData, settingsData] = await Promise.all([
                alertApi.listRules(), alertApi.getHistory(20), alertApi.getSettings(),
            ]);
            setRules(rulesData.rules || []);
            setHistory(histData.history || []);
            setAllowAllIp(settingsData.allow_local_webhook ?? false);
        } catch (e) { console.error(e); }
        finally { setLoading(false); }
    };

    useEffect(() => { fetchData(); }, []);

    /** 打开哨兵 Dialog，同时加载 Bot 列表（WS 直连 + Docker 容器兜底，不强要求在线） */
    const openSentinelDialog = async () => {
        setSentinelForm({ ...EMPTY_SENTINEL });
        setSentinelOpen(true);
        try {
            const bots = await botApi.list();
            // 只要容器名存在即可配置为哨兵，不限制 connected 状态
            // 运行时实际发消息时再判断是否在线（_dispatch_qq_bot_rules 轮询在线哨兵）
            setOnlineBots(bots.filter(b => b.name));
        } catch (e) { console.error(e); }
    };

    /** Webhook 规则创建（type 不含 qq_bot） */
    const handleCreate = async () => {
        if (!form.name) return;
        try {
            await alertApi.createRule({ name: form.name, type: form.type, config: {}, webhook_url: form.webhook_url });
            setCreateOpen(false);
            setForm({ ...EMPTY_FORM });
            fetchData();
        } catch (e) { console.error(e); }
    };

    /** 哨兵规则创建（type 固定 qq_bot，sender_bots 存容器名） */
    const handleSentinelCreate = async () => {
        if (sentinelForm.selectedNames.length === 0) return;
        const config = {
            sender_bots: sentinelForm.selectedNames,   // 容器名，后端按 name 查 WS 表
            targets: sentinelForm.targets.filter(t => t.target_id),
        };
        const autoName = `qq_bot_${sentinelForm.selectedNames[0]}_${Date.now()}`;
        try {
            await alertApi.createRule({ name: autoName, type: 'qq_bot', config, webhook_url: '' });
            setSentinelOpen(false);
            setSentinelForm({ ...EMPTY_SENTINEL });
            fetchData();
        } catch (e) { console.error(e); }
    };

    const handleToggle = async (rule: AlertRule) => {
        await alertApi.updateRule(rule.id, { enabled: !rule.enabled });
        fetchData();
    };

    const handleDelete = async (id: string) => {
        if (!confirm(t('alerts.confirmDelete'))) return;
        await alertApi.deleteRule(id);
        fetchData();
    };

    const handleAllowAllIpToggle = async () => {
        const newVal = !allowAllIp;
        setAllowAllIp(newVal);
        try {
            await alertApi.updateSettings({ allow_local_webhook: newVal });
        } catch (e) {
            console.error(e);
            setAllowAllIp(!newVal);
        }
    };

    /** 更新哨兵 targets 某一行字段 */
    const setSentinelTarget = (idx: number, field: keyof QqBotTarget, val: string) => {
        const next = sentinelForm.targets.map((tgt, i) => i === idx ? { ...tgt, [field]: val } : tgt);
        setSentinelForm({ ...sentinelForm, targets: next });
    };

    const alertTypes: Record<string, string> = {
        container_stop: t('alerts.typeContainerStop'),
        high_cpu: t('alerts.typeHighCpu'),
        high_mem: t('alerts.typeHighMem'),
        login_failure: t('alerts.typeLoginFailure'),
    };

    const cardSx = {
        borderRadius: 3, mb: 3,
        bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
        backdropFilter: 'blur(16px) saturate(1.2)',
        WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
        border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
    };
    const rowSx = {
        p: 2, borderRadius: 2,
        bgcolor: theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.03)',
        border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)'}`,
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    };

    if (loading) return <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}><CircularProgress /></Box>;

    return (
        <Box sx={{ p: 3 }}>
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                    <Box sx={{ p: 1.5, borderRadius: 2, bgcolor: 'rgba(245,158,11,0.1)', display: 'flex' }}>
                        <NotificationsActiveIcon sx={{ fontSize: 28, color: '#f59e0b' }} />
                    </Box>
                    <Box>
                        <Typography variant="h5" sx={{ fontWeight: 700 }}>{t('alerts.title')}</Typography>
                        <Typography variant="body2" color="text.secondary">{t('alerts.subtitle')}</Typography>
                    </Box>
                </Box>
                <Button variant="contained" startIcon={<AddIcon />} onClick={() => setCreateOpen(true)}
                    sx={{ borderRadius: 2, background: '#2563eb', boxShadow: 'none' }}>
                    {t('alerts.addRule')}
                </Button>
            </Box>

            {/* ── Webhook 告警卡片 ── */}
            <Card elevation={0} sx={{
                borderRadius: 3, mb: 3,
                bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                backdropFilter: 'blur(16px) saturate(1.2)',
                WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
            }}>
                <CardContent>
                    {/* 卡片标题 + allowAllIp 开关 */}
                    <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 2 }}>
                        <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
                            {t('alerts.webhookSection')}
                        </Typography>
                        <FormControlLabel
                            control={<Switch checked={allowAllIp} onChange={handleAllowAllIpToggle} color="primary" />}
                            label={<Box sx={{ textAlign: 'right' }}>
                                <Typography variant="body2" sx={{ fontWeight: 600 }}>{t('alerts.allowAllIp')}</Typography>
                                <Typography variant="caption" color="text.secondary">{t('alerts.allowAllIpHint')}</Typography>
                            </Box>}
                            labelPlacement="start"
                            sx={{ mr: 0, alignItems: 'flex-start' }}
                        />
                    </Box>
                    {/* Webhook 类型规则列表 */}
                    {(() => {
                        const webhookRules = rules.filter(r => r.type !== 'qq_bot');
                        return webhookRules.length === 0 ? (
                            <Typography color="text.secondary" variant="body2" sx={{ textAlign: 'center', py: 2 }}>
                                {t('alerts.noRules')}
                            </Typography>
                        ) : (
                            <Stack spacing={1}>
                                {webhookRules.map(rule => (
                                    <Paper key={rule.id} elevation={0} sx={{
                                        p: 2, borderRadius: 2,
                                        bgcolor: theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.03)',
                                        border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)'}`,
                                        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                                    }}>
                                        <Box>
                                            <Typography variant="body2" sx={{ fontWeight: 600 }}>{rule.name}</Typography>
                                            <Typography variant="caption" color="text.secondary">
                                                {alertTypes[rule.type] || rule.type}
                                                {rule.webhook_url ? ` · ${rule.webhook_url.substring(0, 45)}...` : null}
                                            </Typography>
                                        </Box>
                                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                                            <Switch checked={rule.enabled} onChange={() => handleToggle(rule)} size="small" />
                                            <IconButton size="small" onClick={() => handleDelete(rule.id)} sx={{ color: 'error.main' }}>
                                                <DeleteOutlineIcon fontSize="small" />
                                            </IconButton>
                                        </Box>
                                    </Paper>
                                ))}
                            </Stack>
                        );
                    })()}
                </CardContent>
            </Card>

            {/* ── QQ Bot 哨兵卡片 ── */}
            <Card elevation={0} sx={cardSx}>
                <CardContent>
                    {/* 标题行 + 专属"添加哨兵"按钮 */}
                    <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 2 }}>
                        <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
                            {t('alerts.qqBotSection')}
                        </Typography>
                        <Button variant="outlined" size="small" startIcon={<AddIcon />}
                            onClick={openSentinelDialog}
                            sx={{ borderRadius: 2, borderColor: '#7c3aed', color: '#7c3aed', textTransform: 'none',
                                '&:hover': { borderColor: '#6d28d9', bgcolor: 'rgba(124,58,237,0.06)' } }}>
                            {t('alerts.addSentinel')}
                        </Button>
                    </Box>
                    {(() => {
                        const qqBotRules = rules.filter(r => r.type === 'qq_bot');
                        return qqBotRules.length === 0 ? (
                            <Typography color="text.secondary" variant="body2" sx={{ textAlign: 'center', py: 2 }}>
                                {t('alerts.noRules')}
                            </Typography>
                        ) : (
                            <Stack spacing={1}>
                                {qqBotRules.map(rule => {
                                    const cfg = rule.config as Record<string, unknown>;
                                    const bots = (cfg.sender_bots as string[] | undefined) ?? [];
                                    const tgts = (cfg.targets as unknown[] | undefined) ?? [];
                                    return (
                                        <Paper key={rule.id} elevation={0} sx={rowSx}>
                                            <Box>
                                                <Typography variant="body2" sx={{ fontWeight: 600 }}>{rule.name}</Typography>
                                                <Typography variant="caption" color="text.secondary">
                                                    {t('alerts.qqBotSummary')
                                                        .replace('{bots}', bots.join(', ') || '-')
                                                        .replace('{count}', String(tgts.length))}
                                                </Typography>
                                            </Box>
                                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                                                <Switch checked={rule.enabled} onChange={() => handleToggle(rule)} size="small" />
                                                <IconButton size="small" onClick={() => handleDelete(rule.id)} sx={{ color: 'error.main' }}>
                                                    <DeleteOutlineIcon fontSize="small" />
                                                </IconButton>
                                            </Box>
                                        </Paper>
                                    );
                                })}
                            </Stack>
                        );
                    })()}
                </CardContent>
            </Card>

            {/* 告警历史 */}
            <Typography variant="subtitle2" sx={{ mb: 1, mt: 3, fontWeight: 600 }}>{t('alerts.history')}</Typography>
            {history.length === 0 ? (
                <Paper sx={{ p: 4, textAlign: 'center', borderRadius: 3,
                    bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                    backdropFilter: 'blur(16px) saturate(1.2)',
                    WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                    border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}` }}>
                    <Typography color="text.secondary">{t('alerts.noHistory')}</Typography>
                </Paper>
            ) : (
                <Paper elevation={0} sx={{ borderRadius: 3,
                    bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                    backdropFilter: 'blur(16px) saturate(1.2)',
                    WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                    border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}` }}>
                    <List dense>
                        {history.map(h => (
                            <ListItem key={h.id} sx={{ borderBottom: `1px solid ${theme.palette.divider}` }}>
                                <ListItemText
                                    primary={h.message}
                                    secondary={new Date(h.created_at * 1000).toLocaleString()}
                                />
                                <Chip label={h.level} size="small" variant="outlined"
                                    color={h.level === 'error' ? 'error' : h.level === 'warning' ? 'warning' : 'info'} />
                            </ListItem>
                        ))}
                    </List>
                </Paper>
            )}

            {/* ── Webhook 创建规则对话框（仅 webhook 类型） ── */}
            <Dialog open={createOpen} onClose={() => setCreateOpen(false)}
                PaperProps={{ sx: { borderRadius: 3, p: 1, minWidth: 460 } }}>
                <DialogTitle sx={{ fontWeight: 700 }}>{t('alerts.addRule')}</DialogTitle>
                <DialogContent sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: '8px !important' }}>
                    <TextField size="small" label={t('alerts.ruleName')} value={form.name}
                        onChange={e => setForm({ ...form, name: e.target.value })}
                        sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2 } }} />
                    <FormControl size="small">
                        <InputLabel>{t('common.type') || 'Type'}</InputLabel>
                        <Select label={t('common.type') || 'Type'} value={form.type}
                            onChange={e => setForm({ ...form, type: e.target.value })}
                            sx={{ borderRadius: 2 }}>
                            {Object.entries(alertTypes).map(([k, v]) => (
                                <MenuItem key={k} value={k}>{v}</MenuItem>
                            ))}
                        </Select>
                    </FormControl>
                    <TextField size="small" label="Webhook URL" placeholder="http://<IP>:60071/common-webhook"
                        value={form.webhook_url}
                        onChange={e => setForm({ ...form, webhook_url: e.target.value })}
                        helperText={t('alerts.webhookHint')}
                        sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2 } }} />
                </DialogContent>
                <DialogActions sx={{ p: 2, pt: 0 }}>
                    <Button onClick={() => setCreateOpen(false)} color="inherit" sx={{ borderRadius: 2 }}>{t('admin.cancelText')}</Button>
                    <Button onClick={handleCreate} disabled={!form.name} variant="contained" disableElevation
                        sx={{ borderRadius: 2, background: '#2563eb' }}>{t('alerts.addRule')}</Button>
                </DialogActions>
            </Dialog>

            {/* ── QQ Bot 哨兵专用对话框 ── */}
            <Dialog open={sentinelOpen} onClose={() => setSentinelOpen(false)}
                PaperProps={{ sx: { borderRadius: 3, p: 1, minWidth: 480 } }}>
                <DialogTitle sx={{ fontWeight: 700 }}>{t('alerts.addSentinel')}</DialogTitle>
                <DialogContent sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: '8px !important' }}>
                    {/* 哨兵 Bot 多选（下拉勾选，按容器名识别，secondary 显示 QQ号） */}
                    <FormControl size="small">
                        <InputLabel>{t('alerts.senderBots')}</InputLabel>
                        <Select multiple value={sentinelForm.selectedNames}
                            onChange={e => setSentinelForm({ ...sentinelForm, selectedNames: e.target.value as string[] })}
                            input={<OutlinedInput label={t('alerts.senderBots')} sx={{ borderRadius: 2 }} />}
                            renderValue={selected => (selected as string[]).join(', ')}
                            sx={{ borderRadius: 2 }}>
                            {onlineBots.length === 0 ? (
                                <MenuItem disabled>
                                    <Typography variant="caption" color="text.secondary">{t('alerts.noOnlineBots')}</Typography>
                                </MenuItem>
                            ) : onlineBots.map(bot => (
                                <MenuItem key={bot.name} value={bot.name}>
                                    <ListItemIcon sx={{ minWidth: 36 }}>
                                        <Checkbox checked={sentinelForm.selectedNames.includes(bot.name)} size="small" />
                                    </ListItemIcon>
                                    <ListItemText
                                        primary={
                                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                                                <span>{bot.name}</span>
                                                <Box component="span" sx={{
                                                    display: 'inline-block', width: 6, height: 6, borderRadius: '50%',
                                                    bgcolor: bot.connected ? 'success.main' : 'text.disabled', ml: 0.5,
                                                }} />
                                            </Box>
                                        }
                                        secondary={bot.uin ? `QQ: ${bot.uin}${bot.nickname ? ` (${bot.nickname})` : ''}` : undefined}
                                    />
                                </MenuItem>
                            ))}
                        </Select>
                    </FormControl>

                    {/* 通知目标列表 */}
                    <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600, mt: 0.5 }}>
                        {t('alerts.targets')}
                    </Typography>
                    {sentinelForm.targets.map((tgt, idx) => (
                        <Box key={idx} sx={{ display: 'flex', gap: 1, alignItems: 'center' }}>
                            <FormControl size="small" sx={{ minWidth: 90 }}>
                                <Select value={tgt.msg_type}
                                    onChange={e => setSentinelTarget(idx, 'msg_type', e.target.value)}
                                    sx={{ borderRadius: 2 }}>
                                    <MenuItem value="private">{t('alerts.msgTypePrivate')}</MenuItem>
                                    <MenuItem value="group">{t('alerts.msgTypeGroup')}</MenuItem>
                                </Select>
                            </FormControl>
                            <TextField size="small" fullWidth
                                placeholder={t('alerts.targetId')}
                                value={tgt.target_id}
                                onChange={e => setSentinelTarget(idx, 'target_id', e.target.value)}
                                sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2 } }} />
                            <IconButton size="small" color="error"
                                disabled={sentinelForm.targets.length <= 1}
                                onClick={() => setSentinelForm({ ...sentinelForm, targets: sentinelForm.targets.filter((_, i) => i !== idx) })}>
                                <DeleteOutlineIcon fontSize="small" />
                            </IconButton>
                        </Box>
                    ))}
                    <Button size="small" startIcon={<AddIcon />}
                        onClick={() => setSentinelForm({ ...sentinelForm, targets: [...sentinelForm.targets, { msg_type: 'private', target_id: '' }] })}
                        sx={{ alignSelf: 'flex-start', textTransform: 'none' }}>
                        {t('alerts.addTarget')}
                    </Button>
                </DialogContent>
                <DialogActions sx={{ p: 2, pt: 0 }}>
                    <Button onClick={() => setSentinelOpen(false)} color="inherit" sx={{ borderRadius: 2 }}>{t('admin.cancelText')}</Button>
                    <Button onClick={handleSentinelCreate} disabled={sentinelForm.selectedNames.length === 0}
                        variant="contained" disableElevation
                        sx={{ borderRadius: 2, background: '#7c3aed' }}>{t('alerts.addSentinel')}</Button>
                </DialogActions>
            </Dialog>
        </Box>
    );
}
