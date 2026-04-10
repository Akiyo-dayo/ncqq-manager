import { useState, useEffect } from 'react';
import {
    Box, Typography, Button, TextField, Paper, IconButton,
    useTheme, CircularProgress, Chip, Dialog, DialogTitle,
    DialogContent, DialogActions, LinearProgress, Tooltip
} from '@mui/material';
import RefreshIcon from '@mui/icons-material/Refresh';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import DownloadIcon from '@mui/icons-material/Download';
import ImageIcon from '@mui/icons-material/Image';
import { imageApi, type DockerImage } from '../services/api';
import { useTranslate } from '../i18n';
import { useToast } from '../components/Toast';

export default function ImageManager() {
    const theme = useTheme();
    const t = useTranslate();
    const toast = useToast();
    const [images, setImages] = useState<DockerImage[]>([]);
    const [loading, setLoading] = useState(true);
    const [pullDialog, setPullDialog] = useState(false);
    const [pullImage, setPullImage] = useState('');
    const [pulling, setPulling] = useState(false);

    const fetchImages = async () => {
        setLoading(true);
        try {
            const data = await imageApi.list();
            setImages(data.images || []);
        } catch (e) { console.error(e); }
        finally { setLoading(false); }
    };

    useEffect(() => { fetchImages(); }, []);

    const handlePull = async () => {
        if (!pullImage.trim()) return;
        setPulling(true);
        try {
            await imageApi.pull(pullImage.trim());
            toast.success(`${pullImage.trim()} pull ✓`);
            setPullDialog(false);
            setPullImage('');
            fetchImages();
        } catch (e) { toast.error(`Pull ✗: ${e}`); }
        finally { setPulling(false); }
    };

    const handleDelete = async (id: string) => {
        if (!confirm(t('imageManager.confirmDelete'))) return;
        try {
            await imageApi.delete(id, false);
            toast.success(`${t('admin.deleteText')} ✓`);
            fetchImages();
        } catch (e) { toast.error(`${t('admin.deleteText')} ✗`); }
    };

    const formatSize = (mb: number) => mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;

    return (
        <Box sx={{ p: 3 }}>
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                    <Box sx={{ p: 1.5, borderRadius: 2, bgcolor: 'rgba(59,130,246,0.1)', display: 'flex' }}>
                        <ImageIcon sx={{ fontSize: 28, color: '#3b82f6' }} />
                    </Box>
                    <Box>
                        <Typography variant="h5" sx={{ fontWeight: 700 }}>{t('imageManager.title')}</Typography>
                        <Typography variant="body2" color="text.secondary">{t('imageManager.subtitle')}</Typography>
                    </Box>
                </Box>
                <Box sx={{ display: 'flex', gap: 1 }}>
                    <IconButton onClick={fetchImages} sx={{ border: `1px solid ${theme.palette.divider}`, borderRadius: 2 }}>
                        <RefreshIcon fontSize="small" />
                    </IconButton>
                    <Button variant="contained" startIcon={<DownloadIcon />}
                        onClick={() => setPullDialog(true)}
                        sx={{ borderRadius: 2, background: '#2563eb', boxShadow: 'none', '&:hover': { background: '#1d4ed8' } }}>
                        {t('imageManager.pullImage')}
                    </Button>
                </Box>
            </Box>

            {loading ? (
                <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}><CircularProgress /></Box>
            ) : images.length === 0 ? (
                <Paper sx={{ p: 6, textAlign: 'center', borderRadius: 3 }}>
                    <Typography color="text.secondary">{t('imageManager.noImages')}</Typography>
                </Paper>
            ) : (
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
                    {images.map((img) => (
                        <Paper key={img.id} elevation={0}
                            sx={{ p: 2, borderRadius: 3, border: `1px solid ${theme.palette.divider}`,
                                display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 2 }}>
                            <Box sx={{ flex: 1, minWidth: 0 }}>
                                <Typography variant="body1" sx={{ fontWeight: 600, wordBreak: 'break-all' }}>
                                    {img.tags.length > 0 ? img.tags[0] : `<${t('imageManager.untagged')}>`}
                                </Typography>
                                {img.tags.length > 1 && (
                                    <Box sx={{ display: 'flex', gap: 0.5, mt: 0.5, flexWrap: 'wrap' }}>
                                        {img.tags.slice(1).map(tag => (
                                            <Chip key={tag} label={tag} size="small" variant="outlined" />
                                        ))}
                                    </Box>
                                )}
                                <Typography variant="caption" color="text.secondary">
                                    ID: {img.id} · {formatSize(img.size)}
                                </Typography>
                            </Box>
                            <Tooltip title={t('admin.deleteText')}>
                                <IconButton onClick={() => handleDelete(img.id)} size="small"
                                    sx={{ color: 'error.main' }}>
                                    <DeleteOutlineIcon fontSize="small" />
                                </IconButton>
                            </Tooltip>
                        </Paper>
                    ))}
                </Box>
            )}

            <Dialog open={pullDialog} onClose={() => !pulling && setPullDialog(false)}
                PaperProps={{ sx: { borderRadius: 3, p: 1, minWidth: 420 } }}>
                <DialogTitle sx={{ fontWeight: 700 }}>{t('imageManager.pullImage')}</DialogTitle>
                <DialogContent>
                    <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                        {t('imageManager.pullHint')}
                    </Typography>
                    <TextField autoFocus fullWidth size="small"
                        label={t('imageManager.imageName')}
                        placeholder="mlikiowa/napcat-docker:latest"
                        value={pullImage}
                        onChange={e => setPullImage(e.target.value)}
                        disabled={pulling}
                        sx={{ '& .MuiOutlinedInput-root': { borderRadius: 2 } }} />
                    {pulling && <LinearProgress sx={{ mt: 2, borderRadius: 1 }} />}
                </DialogContent>
                <DialogActions sx={{ p: 2, pt: 0 }}>
                    <Button onClick={() => setPullDialog(false)} disabled={pulling}
                        color="inherit" sx={{ borderRadius: 2 }}>{t('admin.cancelText')}</Button>
                    <Button onClick={handlePull} disabled={pulling || !pullImage.trim()}
                        variant="contained" disableElevation
                        sx={{ borderRadius: 2, background: '#2563eb' }}>
                        {pulling ? t('imageManager.pulling') : t('imageManager.pullImage')}
                    </Button>
                </DialogActions>
            </Dialog>
        </Box>
    );
}

