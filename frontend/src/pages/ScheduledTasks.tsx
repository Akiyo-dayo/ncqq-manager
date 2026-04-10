import { useState, useEffect } from 'react';
import {
    Box, Typography, Button, TextField, Paper, IconButton, Switch,
    useTheme, CircularProgress, Dialog, DialogTitle, DialogContent,
    DialogActions, Select, MenuItem
} from '@mui/material';
import ScheduleIcon from '@mui/icons-material/Schedule';
import AddIcon from '@mui/icons-material/Add';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import { schedulerApi, type ScheduledTask } from '../services/api';
import { useTranslate } from '../i18n';

export default function ScheduledTasks() {
    const theme = useTheme();
    const t = useTranslate();
    const [tasks, setTasks] = useState<ScheduledTask[]>([]);
    const [loading, setLoading] = useState(true);
    const [createOpen, setCreateOpen] = useState(false);
    const [form, setForm] = useState({ name: '', type: 'backup_db', interval_hours: 24 });

    const fetchTasks = async () => {
        try {
            const data = await schedulerApi.list();
            setTasks(data.tasks || []);
        } catch (e) { console.error(e); }
        finally { setLoading(false); }
    };

    useEffect(() => { fetchTasks(); }, []);

    const handleCreate = async () => {
        if (!form.name) return;
        try {
            await schedulerApi.create({
                name: form.name,
                type: form.type,
                interval_seconds: form.interval_hours * 3600,
            });
            setCreateOpen(false);
            setForm({ name: '', type: 'backup_db', interval_hours: 24 });
            fetchTasks();
        } catch (e) { console.error(e); }
    };

    const handleToggle = async (task: ScheduledTask) => {
        await schedulerApi.update(task.id, { enabled: !task.enabled });
        fetchTasks();
    };

    const handleDelete = async (id: string) => {
        if (!confirm(t('scheduler.confirmDelete'))) return;
        await schedulerApi.delete(id);
        fetchTasks();
    };

    const taskTypes: Record<string, string> = {
        backup_db: t('scheduler.typeBackup'),
        restart_container: t('scheduler.typeRestart'),
        cleanup_logs: t('scheduler.typeCleanup'),
    };

    const formatInterval = (seconds: number) => {
        const hours = seconds / 3600;
        return hours >= 24 ? `${(hours / 24).toFixed(0)} ${t('scheduler.days')}` : `${hours.toFixed(0)} ${t('scheduler.hours')}`;
    };

    if (loading) return <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}><CircularProgress /></Box>;

    return (
        <Box sx={{ p: 3 }}>
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                    <Box sx={{ p: 1.5, borderRadius: 2, bgcolor: 'rgba(6,182,212,0.1)', display: 'flex' }}>
                        <ScheduleIcon sx={{ fontSize: 28, color: '#06b6d4' }} />
                    </Box>
                    <Box>
                        <Typography variant="h5" sx={{ fontWeight: 700 }}>{t('scheduler.title')}</Typography>
                        <Typography variant="body2" color="text.secondary">{t('scheduler.subtitle')}</Typography>
                    </Box>
                </Box>
                <Button variant="contained" startIcon={<AddIcon />} onClick={() => setCreateOpen(true)}
                    sx={{ borderRadius: 2, background: '#2563eb', boxShadow: 'none' }}>
                    {t('scheduler.addTask')}
                </Button>
            </Box>

            {tasks.length === 0 ? (
                <Paper sx={{ p: 4, textAlign: 'center', borderRadius: 3,
                    bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                    backdropFilter: 'blur(16px) saturate(1.2)',
                    WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                    border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}` }}>
                    <Typography color="text.secondary">{t('scheduler.noTasks')}</Typography>
                </Paper>
            ) : (
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
                    {tasks.map(task => (
                        <Paper key={task.id} elevation={0}
                            sx={{ p: 2, borderRadius: 3,
                                bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                                backdropFilter: 'blur(16px) saturate(1.2)',
                                WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                                border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
                                display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                            <Box>
                                <Typography variant="body1" sx={{ fontWeight: 600 }}>{task.name}</Typography>
                                <Typography variant="caption" color="text.secondary">
                                    {taskTypes[task.type] || task.type}
                                    {' · '}{t('scheduler.interval')}: {formatInterval(task.interval_seconds)}
                                    {task.last_run > 0 && ` · ${t('scheduler.lastRun')}: ${new Date(task.last_run * 1000).toLocaleString()}`}
                                </Typography>
                            </Box>
                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                                <Switch checked={task.enabled} onChange={() => handleToggle(task)} size="small" />
                                <IconButton size="small" onClick={() => handleDelete(task.id)} sx={{ color: 'error.main' }}>
                                    <DeleteOutlineIcon fontSize="small" />
                                </IconButton>
                            </Box>
                        </Paper>
                    ))}
                </Box>
            )}

            <Dialog open={createOpen} onClose={() => setCreateOpen(false)}
                PaperProps={{ sx: { borderRadius: 3, p: 1, minWidth: 420 } }}>
                <DialogTitle sx={{ fontWeight: 700 }}>{t('scheduler.addTask')}</DialogTitle>
                <DialogContent sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: '8px !important' }}>
                    <TextField size="small" label={t('scheduler.taskName')} value={form.name}
                        onChange={e => setForm({ ...form, name: e.target.value })}
                        sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2 } }} />
                    <Select size="small" value={form.type}
                        onChange={e => setForm({ ...form, type: e.target.value })}
                        sx={{ borderRadius: 2 }}>
                        {Object.entries(taskTypes).map(([k, v]) => (
                            <MenuItem key={k} value={k}>{v}</MenuItem>
                        ))}
                    </Select>
                    <TextField size="small" type="number" label={t('scheduler.intervalHours')}
                        value={form.interval_hours}
                        onChange={e => setForm({ ...form, interval_hours: parseInt(e.target.value) || 1 })}
                        sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2 } }} />
                </DialogContent>
                <DialogActions sx={{ p: 2, pt: 0 }}>
                    <Button onClick={() => setCreateOpen(false)} color="inherit" sx={{ borderRadius: 2 }}>{t('admin.cancelText')}</Button>
                    <Button onClick={handleCreate} disabled={!form.name} variant="contained" disableElevation
                        sx={{ borderRadius: 2, background: '#2563eb' }}>{t('scheduler.addTask')}</Button>
                </DialogActions>
            </Dialog>
        </Box>
    );
}

