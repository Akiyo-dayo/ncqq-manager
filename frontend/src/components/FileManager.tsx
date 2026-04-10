import React, { useState, useEffect, useMemo } from 'react';
import {
    Box,
    Typography,
    Button,
    CircularProgress,
    Dialog,
    DialogTitle,
    DialogContent,
    DialogActions,
    TextField,
    IconButton,
    useTheme,
} from '@mui/material';
import InsertDriveFileIcon from '@mui/icons-material/InsertDriveFile';
import RefreshIcon from '@mui/icons-material/Refresh';
import FolderIcon from '@mui/icons-material/Folder';
import ArrowBackIcon from '@mui/icons-material/ArrowBack';
import EditIcon from '@mui/icons-material/Edit';
import DeleteIcon from '@mui/icons-material/Delete';
import DataObjectIcon from '@mui/icons-material/DataObject';
import DescriptionIcon from '@mui/icons-material/Description';
import SettingsIcon from '@mui/icons-material/Settings';
import ImageIcon from '@mui/icons-material/Image';
import CodeIcon from '@mui/icons-material/Code';
import StorageIcon from '@mui/icons-material/Storage';
import HomeIcon from '@mui/icons-material/Home';
import NavigateNextIcon from '@mui/icons-material/NavigateNext';
import { useTranslate } from '../i18n';
import { useToast } from './Toast';
import { containerApi, type FileItem, type FolderItem } from '../services/api';

interface FileManagerProps {
    name: string;
    node_id: string;
}

// 文件类型图标映射
const getFileIcon = (filename: string) => {
    const ext = filename.split('.').pop()?.toLowerCase() || '';
    const iconMap: Record<string, { icon: React.ReactElement; color: string }> = {
        json: { icon: <DataObjectIcon fontSize="small" />, color: '#f59e0b' },
        jsonl: { icon: <DataObjectIcon fontSize="small" />, color: '#f59e0b' },
        js: { icon: <CodeIcon fontSize="small" />, color: '#eab308' },
        mjs: { icon: <CodeIcon fontSize="small" />, color: '#eab308' },
        ts: { icon: <CodeIcon fontSize="small" />, color: '#3b82f6' },
        py: { icon: <CodeIcon fontSize="small" />, color: '#22c55e' },
        md: { icon: <DescriptionIcon fontSize="small" />, color: '#6366f1' },
        txt: { icon: <DescriptionIcon fontSize="small" />, color: '#64748b' },
        log: { icon: <DescriptionIcon fontSize="small" />, color: '#94a3b8' },
        yml: { icon: <SettingsIcon fontSize="small" />, color: '#ec4899' },
        yaml: { icon: <SettingsIcon fontSize="small" />, color: '#ec4899' },
        toml: { icon: <SettingsIcon fontSize="small" />, color: '#ec4899' },
        ini: { icon: <SettingsIcon fontSize="small" />, color: '#ec4899' },
        png: { icon: <ImageIcon fontSize="small" />, color: '#06b6d4' },
        jpg: { icon: <ImageIcon fontSize="small" />, color: '#06b6d4' },
        jpeg: { icon: <ImageIcon fontSize="small" />, color: '#06b6d4' },
        gif: { icon: <ImageIcon fontSize="small" />, color: '#06b6d4' },
        svg: { icon: <ImageIcon fontSize="small" />, color: '#06b6d4' },
        db: { icon: <StorageIcon fontSize="small" />, color: '#8b5cf6' },
        sqlite: { icon: <StorageIcon fontSize="small" />, color: '#8b5cf6' },
    };
    return iconMap[ext] || { icon: <InsertDriveFileIcon fontSize="small" />, color: '#94a3b8' };
};

// 文件大小格式化
const formatSize = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

// Grid 列定义
const GRID_COLS = '1fr 100px 160px 60px';

const FileManager: React.FC<FileManagerProps> = ({ name, node_id }) => {
    const [files, setFiles] = useState<FileItem[]>([]);
    const [folders, setFolders] = useState<FolderItem[]>([]);
    const [currentPath, setCurrentPath] = useState('');
    const [loading, setLoading] = useState(true);
    const [editingFile, setEditingFile] = useState<string | null>(null);
    const [editContent, setEditContent] = useState('');
    const [savingFile, setSavingFile] = useState(false);
    const [deleteConfirm, setDeleteConfirm] = useState<{ open: boolean; path: string; displayName: string; isDir: boolean }>({
        open: false, path: '', displayName: '', isDir: false,
    });
    const [deleting, setDeleting] = useState(false);
    const t = useTranslate();
    const theme = useTheme();
    const toast = useToast();

    const loadFiles = async (pathStr = currentPath) => {
        setLoading(true);
        try {
            const data = await containerApi.listFiles(name, pathStr, node_id);
            setFiles(data.files || []);
            setFolders(data.folders || []);
            setCurrentPath(data.current_path || '');
        } catch (e) {
            console.error(e);
        } finally {
            setLoading(false);
        }
    };

    const handleEdit = async (filename: string) => {
        const fullPath = currentPath ? `${currentPath}/${filename}` : filename;
        setLoading(true);
        try {
            const data = await containerApi.getConfig(name, fullPath, node_id);
            setEditContent(data.content || '');
            setEditingFile(fullPath);
        } catch (e) {
            console.error(e);
        } finally {
            setLoading(false);
        }
    };

    const handleSaveFile = async () => {
        if (!editingFile) return;
        setSavingFile(true);
        try {
            await containerApi.saveConfig(name, editingFile, editContent, node_id);
            setEditingFile(null);
            loadFiles(currentPath);
            toast.success(t('config.saveSuccess'));
        } catch (e) {
            console.error(e);
            toast.error(t('config.saveFailed'));
        } finally {
            setSavingFile(false);
        }
    };

    const handleFolderClick = (folderName: string) => {
        const newPath = currentPath ? `${currentPath}/${folderName}` : folderName;
        loadFiles(newPath);
    };

    const handleUpFolder = () => {
        if (!currentPath) return;
        const parts = currentPath.split('/');
        parts.pop();
        loadFiles(parts.join('/'));
    };

    const openDeleteConfirm = (itemName: string, isDir: boolean) => {
        const fullPath = currentPath ? `${currentPath}/${itemName}` : itemName;
        setDeleteConfirm({ open: true, path: fullPath, displayName: itemName, isDir });
    };

    const handleConfirmDelete = async () => {
        setDeleting(true);
        try {
            await containerApi.deleteFile(name, deleteConfirm.path, node_id);
            toast.success(t('config.deleteSuccess'));
            setDeleteConfirm({ open: false, path: '', displayName: '', isDir: false });
            loadFiles(currentPath);
        } catch {
            toast.error(t('config.deleteFailed'));
        } finally {
            setDeleting(false);
        }
    };

    useEffect(() => {
        loadFiles();
    }, [name]);

    // 排序：文件夹按名称排序，文件按名称排序
    const sortedFolders = useMemo(() => [...folders].sort((a, b) => a.name.localeCompare(b.name)), [folders]);
    const sortedFiles = useMemo(() => [...files].sort((a, b) => a.name.localeCompare(b.name)), [files]);

    // 面包屑路径段
    const breadcrumbs = useMemo(() => {
        if (!currentPath) return [];
        return currentPath.split('/').filter(Boolean);
    }, [currentPath]);

    const navigateToBreadcrumb = (index: number) => {
        if (index < 0) { loadFiles(''); return; }
        const target = breadcrumbs.slice(0, index + 1).join('/');
        loadFiles(target);
    };

    const isDark = theme.palette.mode === 'dark';
    const hoverBg = isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.02)';
    const itemCount = sortedFolders.length + sortedFiles.length;

    return (
        <Box sx={{ mt: 1, height: '100%', display: 'flex', flexDirection: 'column' }}>
            {/* ── 顶部工具栏：面包屑 + 刷新 ── */}
            <Box sx={{
                display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2,
                px: 2, py: 1.5, borderRadius: 2,
                background: isDark ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                backdropFilter: 'blur(16px) saturate(1.2)',
                WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                border: `1px solid ${isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
            }}>
                {/* 面包屑导航 */}
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, minWidth: 0, overflow: 'hidden' }}>
                    <IconButton size="small" onClick={() => navigateToBreadcrumb(-1)} sx={{ color: '#6366f1', flexShrink: 0 }}>
                        <HomeIcon fontSize="small" />
                    </IconButton>
                    {breadcrumbs.map((seg, i) => (
                        <React.Fragment key={i}>
                            <NavigateNextIcon sx={{ fontSize: 16, color: 'text.disabled', flexShrink: 0 }} />
                            <Typography
                                variant="body2"
                                onClick={() => navigateToBreadcrumb(i)}
                                noWrap
                                sx={{
                                    cursor: 'pointer', fontWeight: i === breadcrumbs.length - 1 ? 700 : 400,
                                    color: i === breadcrumbs.length - 1 ? 'text.primary' : 'text.secondary',
                                    '&:hover': { color: '#6366f1', textDecoration: 'underline' },
                                    maxWidth: 160,
                                }}
                            >
                                {seg}
                            </Typography>
                        </React.Fragment>
                    ))}
                    {breadcrumbs.length === 0 && (
                        <Typography variant="body2" color="text.secondary" sx={{ ml: 0.5 }}>{t('config.rootDir')}</Typography>
                    )}
                </Box>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexShrink: 0 }}>
                    {!loading && <Typography variant="caption" color="text.disabled">{itemCount} {t('config.items')}</Typography>}
                    <IconButton size="small" onClick={() => loadFiles(currentPath)} disabled={loading} sx={{ color: 'text.secondary' }}>
                        {loading ? <CircularProgress size={18} thickness={4} /> : <RefreshIcon fontSize="small" />}
                    </IconButton>
                </Box>
            </Box>

            {/* ── 文件列表容器 ── */}
            <Box sx={{
                flex: 1, borderRadius: 2, border: `1px solid ${theme.palette.divider}`,
                bgcolor: isDark ? 'rgba(255,255,255,0.01)' : '#fff', overflow: 'hidden',
                display: 'flex', flexDirection: 'column',
            }}>
                {/* 列头 */}
                <Box sx={{
                    display: 'grid', gridTemplateColumns: GRID_COLS, alignItems: 'center',
                    px: 2, py: 1, bgcolor: isDark ? 'rgba(0,0,0,0.2)' : '#f8fafc',
                    borderBottom: `1px solid ${theme.palette.divider}`,
                }}>
                    <Typography variant="caption" fontWeight={600} color="text.secondary">{t('config.fileName')}</Typography>
                    <Typography variant="caption" fontWeight={600} color="text.secondary">{t('config.fileSize')}</Typography>
                    <Typography variant="caption" fontWeight={600} color="text.secondary">{t('config.lastModified')}</Typography>
                    <Box />
                </Box>

                {/* 内容区 */}
                <Box sx={{ flex: 1, overflowY: 'auto' }}>
                    {loading ? (
                        <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}>
                            <CircularProgress size={28} thickness={4} />
                        </Box>
                    ) : itemCount === 0 && !currentPath ? (
                        <Box sx={{ textAlign: 'center', py: 8 }}>
                            <InsertDriveFileIcon sx={{ fontSize: 48, color: 'text.disabled', mb: 1 }} />
                            <Typography variant="body2" color="text.secondary">{t('config.noFiles')}</Typography>
                        </Box>
                    ) : (
                        <>
                            {/* 返回上级 */}
                            {currentPath && (
                                <Box
                                    onClick={handleUpFolder}
                                    sx={{
                                        display: 'grid', gridTemplateColumns: GRID_COLS, alignItems: 'center',
                                        px: 2, py: 1.2, cursor: 'pointer', transition: 'background 0.15s',
                                        borderBottom: `1px solid ${theme.palette.divider}`,
                                        '&:hover': { bgcolor: hoverBg },
                                    }}
                                >
                                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
                                        <ArrowBackIcon fontSize="small" sx={{ color: 'text.secondary' }} />
                                        <Typography variant="body2" color="text.secondary">..</Typography>
                                    </Box>
                                    <Box /><Box /><Box />
                                </Box>
                            )}

                            {/* 文件夹列表 */}
                            {sortedFolders.map(f => (
                                <Box
                                    key={`d-${f.name}`}
                                    sx={{
                                        display: 'grid', gridTemplateColumns: GRID_COLS, alignItems: 'center',
                                        px: 2, py: 1.2, transition: 'background 0.15s',
                                        borderBottom: `1px solid ${theme.palette.divider}`,
                                        '&:hover': { bgcolor: hoverBg },
                                    }}
                                >
                                    <Box
                                        onClick={() => handleFolderClick(f.name)}
                                        sx={{ display: 'flex', alignItems: 'center', gap: 1.5, minWidth: 0, cursor: 'pointer' }}
                                    >
                                        <FolderIcon fontSize="small" sx={{ color: '#f59e0b', flexShrink: 0 }} />
                                        <Typography variant="body2" fontWeight={600} noWrap>{f.name}</Typography>
                                    </Box>
                                    <Typography variant="caption" color="text.disabled">—</Typography>
                                    <Typography variant="caption" color="text.disabled">—</Typography>
                                    <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
                                        <IconButton
                                            size="small"
                                            onClick={() => openDeleteConfirm(f.name, true)}
                                            sx={{ color: 'text.disabled', '&:hover': { color: '#ef4444' } }}
                                            title={t('config.deleteFile')}
                                        >
                                            <DeleteIcon fontSize="small" />
                                        </IconButton>
                                    </Box>
                                </Box>
                            ))}

                            {/* 文件列表 */}
                            {sortedFiles.map(f => {
                                const fi = getFileIcon(f.name);
                                return (
                                    <Box
                                        key={`f-${f.name}`}
                                        sx={{
                                            display: 'grid', gridTemplateColumns: GRID_COLS, alignItems: 'center',
                                            px: 2, py: 1.2, transition: 'background 0.15s',
                                            borderBottom: `1px solid ${theme.palette.divider}`,
                                            '&:hover': { bgcolor: hoverBg },
                                            '&:last-child': { borderBottom: 'none' },
                                        }}
                                    >
                                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, minWidth: 0 }}>
                                            <Box sx={{ color: fi.color, display: 'flex', flexShrink: 0 }}>{fi.icon}</Box>
                                            <Typography variant="body2" noWrap sx={{ fontFamily: 'monospace' }}>{f.name}</Typography>
                                        </Box>
                                        <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace' }}>
                                            {formatSize(f.size)}
                                        </Typography>
                                        <Typography variant="caption" color="text.secondary">
                                            {new Date(f.mtime * 1000).toLocaleString()}
                                        </Typography>
                                        <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
                                            <IconButton
                                                size="small"
                                                onClick={() => handleEdit(f.name)}
                                                sx={{ color: 'text.secondary', '&:hover': { color: '#3b82f6' } }}
                                                title={t('config.edit')}
                                            >
                                                <EditIcon fontSize="small" />
                                            </IconButton>
                                            <IconButton
                                                size="small"
                                                onClick={() => openDeleteConfirm(f.name, false)}
                                                sx={{ color: 'text.disabled', '&:hover': { color: '#ef4444' } }}
                                                title={t('config.deleteFile')}
                                            >
                                                <DeleteIcon fontSize="small" />
                                            </IconButton>
                                        </Box>
                                    </Box>
                                );
                            })}
                        </>
                    )}
                </Box>
            </Box>

            <Dialog
                open={!!editingFile}
                onClose={() => setEditingFile(null)}
                maxWidth="lg"
                fullWidth
                PaperProps={{
                    sx: {
                        borderRadius: 3,
                        bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,30,0.98)' : 'rgba(255,255,255,0.98)',
                        backdropFilter: 'blur(20px)',
                        boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.25)',
                        border: `1px solid ${theme.palette.divider}`
                    }
                }}
            >
                <DialogTitle sx={{ fontWeight: 700, display: 'flex', alignItems: 'center', gap: 1.5, py: 2.5, borderBottom: `1px solid ${theme.palette.divider}` }}>
                    <Box sx={{ p: 0.5, borderRadius: 1.5, bgcolor: 'rgba(99,102,241,0.1)', display: 'flex' }}>
                        <InsertDriveFileIcon fontSize="small" sx={{ color: '#6366f1' }} />
                    </Box>
                    {t('config.editFile')} / {editingFile}
                </DialogTitle>
                <DialogContent sx={{ p: 0 }}>
                    <TextField
                        multiline
                        fullWidth
                        minRows={24}
                        maxRows={30}
                        value={editContent}
                        onChange={e => setEditContent(e.target.value)}
                        sx={{
                            '& .MuiOutlinedInput-root': {
                                fontFamily: '"Fira Code", Consolas, monospace',
                                fontSize: '0.85rem',
                                bgcolor: theme.palette.mode === 'dark' ? 'rgba(0,0,0,0.2)' : '#f8fafc',
                                borderRadius: 0,
                                '& fieldset': { border: 'none' },
                                p: 2,
                                lineHeight: 1.6
                            }
                        }}
                    />
                </DialogContent>
                <DialogActions sx={{ p: 2, borderTop: `1px solid ${theme.palette.divider}` }}>
                    <Button onClick={() => setEditingFile(null)} color="inherit" sx={{ borderRadius: 2, fontWeight: 600 }}>{t('config.cancel')}</Button>
                    <Button
                        onClick={handleSaveFile}
                        disabled={savingFile}
                        variant="contained"
                        sx={{ px: 4, borderRadius: 2, fontWeight: 600, background: 'linear-gradient(135deg, #3b82f6 0%, #2563eb 100%)', boxShadow: '0 4px 14px rgba(59,130,246,0.3)', textTransform: 'none' }}
                    >
                        {savingFile ? t('config.saving') : t('config.saveChanges')}
                    </Button>
                </DialogActions>
            </Dialog>

            {/* 删除确认对话框 */}
            <Dialog
                open={deleteConfirm.open}
                onClose={() => !deleting && setDeleteConfirm(prev => ({ ...prev, open: false }))}
                maxWidth="xs"
                fullWidth
                PaperProps={{
                    sx: {
                        borderRadius: 3,
                        border: `1px solid ${theme.palette.divider}`,
                    }
                }}
            >
                <DialogTitle sx={{ fontWeight: 700, display: 'flex', alignItems: 'center', gap: 1.5 }}>
                    <Box sx={{ p: 0.5, borderRadius: 1.5, bgcolor: 'rgba(239,68,68,0.1)', display: 'flex' }}>
                        <DeleteIcon fontSize="small" sx={{ color: '#ef4444' }} />
                    </Box>
                    {t('config.deleteConfirmTitle')}
                </DialogTitle>
                <DialogContent>
                    <Typography variant="body2" color="text.secondary">
                        {t('config.deleteConfirmMsg').replace('{name}', deleteConfirm.displayName)}
                    </Typography>
                </DialogContent>
                <DialogActions sx={{ p: 2 }}>
                    <Button
                        onClick={() => setDeleteConfirm(prev => ({ ...prev, open: false }))}
                        disabled={deleting}
                        color="inherit"
                        sx={{ borderRadius: 2, fontWeight: 600 }}
                    >
                        {t('config.cancel')}
                    </Button>
                    <Button
                        onClick={handleConfirmDelete}
                        disabled={deleting}
                        variant="contained"
                        sx={{
                            px: 3, borderRadius: 2, fontWeight: 600,
                            bgcolor: '#ef4444', '&:hover': { bgcolor: '#dc2626' },
                            textTransform: 'none',
                        }}
                    >
                        {deleting ? <CircularProgress size={16} sx={{ color: '#fff' }} /> : t('config.deleteFile')}
                    </Button>
                </DialogActions>
            </Dialog>
        </Box>
    );
};

export default FileManager;

