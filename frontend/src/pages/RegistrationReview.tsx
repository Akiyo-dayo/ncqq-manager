import { useEffect, useState, useCallback } from 'react';
import {
    Box, Typography, Button, Chip, IconButton,
    Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Paper,
    Dialog, DialogTitle, DialogContent, DialogActions, TextField,
    Pagination, ToggleButtonGroup, ToggleButton, Tooltip, useTheme,
    CircularProgress
} from '@mui/material';
import CheckIcon from '@mui/icons-material/Check';
import CloseIcon from '@mui/icons-material/Close';
import DeleteIcon from '@mui/icons-material/Delete';
import { useTranslate } from '../i18n';
import { registrationApi, type RegistrationRequest } from '../services/api';

export default function RegistrationReviewPage() {
    const t = useTranslate();
    const theme = useTheme();
    const [requests, setRequests] = useState<RegistrationRequest[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(1);
    const [statusFilter, setStatusFilter] = useState<string>('pending');
    const [loading, setLoading] = useState(true);

    // Reject dialog
    const [rejectId, setRejectId] = useState<string | null>(null);
    const [rejectReason, setRejectReason] = useState('');
    const [actionLoading, setActionLoading] = useState<string | null>(null);

    const pageSize = 20;

    const fetchRequests = useCallback(async () => {
        setLoading(true);
        try {
            const data = await registrationApi.list(page, pageSize, statusFilter || undefined);
            setRequests(data.data || []);
            setTotal(data.total || 0);
        } catch {
            setRequests([]);
        } finally {
            setLoading(false);
        }
    }, [page, statusFilter]);

    useEffect(() => { fetchRequests(); }, [fetchRequests]);

    const handleApprove = async (id: string) => {
        if (!window.confirm(t('registration.confirmApprove'))) return;
        setActionLoading(id);
        try {
            await registrationApi.approve(id);
            fetchRequests();
        } finally {
            setActionLoading(null);
        }
    };

    const handleRejectConfirm = async () => {
        if (!rejectId) return;
        setActionLoading(rejectId);
        try {
            await registrationApi.reject(rejectId, rejectReason);
            setRejectId(null);
            setRejectReason('');
            fetchRequests();
        } finally {
            setActionLoading(null);
        }
    };

    const handleDelete = async (id: string) => {
        if (!window.confirm(t('registration.confirmReject'))) return;
        setActionLoading(id);
        try {
            await registrationApi.delete(id);
            fetchRequests();
        } finally {
            setActionLoading(null);
        }
    };

    const formatTime = (ts: number) => {
        if (!ts) return '-';
        return new Date(ts * 1000).toLocaleString();
    };

    const statusChip = (status: string) => {
        const map: Record<string, { color: 'warning' | 'success' | 'error'; label: string }> = {
            pending: { color: 'warning', label: t('registration.pending') },
            approved: { color: 'success', label: t('registration.approved') },
            rejected: { color: 'error', label: t('registration.rejected') },
        };
        const cfg = map[status] || { color: 'warning' as const, label: status };
        return <Chip label={cfg.label} color={cfg.color} size="small" />;
    };

    const totalPages = Math.ceil(total / pageSize);

    return (
        <Box sx={{ p: 3 }}>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                {t('registration.breadcrumb')}
            </Typography>
            <Typography variant="h5" sx={{ fontWeight: 700, mb: 3 }}>
                {t('registration.title')}
            </Typography>

            <Box sx={{ mb: 2 }}>
                <ToggleButtonGroup
                    value={statusFilter}
                    exclusive
                    onChange={(_, v) => { if (v !== null) { setStatusFilter(v); setPage(1); } }}
                    size="small"
                >
                    <ToggleButton value="pending">{t('registration.pending')}</ToggleButton>
                    <ToggleButton value="approved">{t('registration.approved')}</ToggleButton>
                    <ToggleButton value="rejected">{t('registration.rejected')}</ToggleButton>
                    <ToggleButton value="">{t('registration.all')}</ToggleButton>
                </ToggleButtonGroup>
            </Box>

            <TableContainer component={Paper} variant="outlined" sx={{ borderRadius: 2 }}>
                <Table size="small">
                    <TableHead>
                        <TableRow>
                            <TableCell sx={{ fontWeight: 600 }}>{t('login.username')}</TableCell>
                            <TableCell sx={{ fontWeight: 600 }}>Status</TableCell>
                            <TableCell sx={{ fontWeight: 600 }}>{t('registration.requestedAt')}</TableCell>
                            <TableCell sx={{ fontWeight: 600 }}>{t('registration.reviewedAt')}</TableCell>
                            <TableCell sx={{ fontWeight: 600 }} align="right">Actions</TableCell>
                        </TableRow>
                    </TableHead>
                    <TableBody>
                        {loading ? (
                            <TableRow>
                                <TableCell colSpan={5} sx={{ textAlign: 'center', py: 4 }}>
                                    <CircularProgress size={24} />
                                </TableCell>
                            </TableRow>
                        ) : requests.length === 0 ? (
                            <TableRow>
                                <TableCell colSpan={5} sx={{ textAlign: 'center', py: 4, color: 'text.secondary' }}>
                                    {t('registration.noData')}
                                </TableCell>
                            </TableRow>
                        ) : requests.map(req => (
                            <TableRow key={req.id}>
                                <TableCell>
                                    <Typography variant="body2" sx={{ fontWeight: 600 }}>{req.userName}</Typography>
                                </TableCell>
                                <TableCell>{statusChip(req.status)}</TableCell>
                                <TableCell>
                                    <Typography variant="caption">{formatTime(req.requested_at)}</Typography>
                                </TableCell>
                                <TableCell>
                                    <Typography variant="caption">{formatTime(req.reviewed_at)}</Typography>
                                    {req.review_reason && (
                                        <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                                            {req.review_reason}
                                        </Typography>
                                    )}
                                </TableCell>
                                <TableCell align="right">
                                    {req.status === 'pending' && (
                                        <>
                                            <Tooltip title={t('registration.approve')}>
                                                <IconButton
                                                    size="small"
                                                    color="success"
                                                    disabled={actionLoading === req.id}
                                                    onClick={() => handleApprove(req.id)}
                                                >
                                                    {actionLoading === req.id ? <CircularProgress size={16} /> : <CheckIcon />}
                                                </IconButton>
                                            </Tooltip>
                                            <Tooltip title={t('registration.reject')}>
                                                <IconButton
                                                    size="small"
                                                    color="error"
                                                    disabled={actionLoading === req.id}
                                                    onClick={() => { setRejectId(req.id); setRejectReason(''); }}
                                                >
                                                    <CloseIcon />
                                                </IconButton>
                                            </Tooltip>
                                        </>
                                    )}
                                    <Tooltip title={t('registration.delete')}>
                                        <IconButton
                                            size="small"
                                            disabled={actionLoading === req.id}
                                            onClick={() => handleDelete(req.id)}
                                        >
                                            <DeleteIcon />
                                        </IconButton>
                                    </Tooltip>
                                </TableCell>
                            </TableRow>
                        ))}
                    </TableBody>
                </Table>
            </TableContainer>

            {totalPages > 1 && (
                <Box sx={{ display: 'flex', justifyContent: 'center', mt: 2 }}>
                    <Pagination
                        count={totalPages}
                        page={page}
                        onChange={(_, v) => setPage(v)}
                        color="primary"
                        shape="rounded"
                    />
                </Box>
            )}

            {/* 拒绝弹窗 */}
            <Dialog open={!!rejectId} onClose={() => setRejectId(null)} maxWidth="xs" fullWidth>
                <DialogTitle>{t('registration.reject')}</DialogTitle>
                <DialogContent>
                    <TextField
                        fullWidth
                        multiline
                        rows={2}
                        label={t('registration.rejectReason')}
                        value={rejectReason}
                        onChange={e => setRejectReason(e.target.value)}
                        sx={{ mt: 1 }}
                    />
                </DialogContent>
                <DialogActions>
                    <Button onClick={() => setRejectId(null)}>{t('userMgmt.cancel')}</Button>
                    <Button color="error" variant="contained" onClick={handleRejectConfirm}>{t('registration.reject')}</Button>
                </DialogActions>
            </Dialog>
        </Box>
    );
}
