import { useState, useEffect, useContext } from 'react';
import { useNavigate } from 'react-router-dom';
import {
    Box, Paper, TextField, Button, Typography, Alert, IconButton,
    useTheme, CircularProgress, Container, Fade, InputAdornment,
    ToggleButtonGroup, ToggleButton, Divider
} from '@mui/material';
import PersonIcon from '@mui/icons-material/Person';
import LockIcon from '@mui/icons-material/Lock';
import PublicIcon from '@mui/icons-material/Public';
import LaptopIcon from '@mui/icons-material/Laptop';
import LanIcon from '@mui/icons-material/Lan';
import FolderIcon from '@mui/icons-material/Folder';
import RocketLaunchIcon from '@mui/icons-material/RocketLaunch';
import Brightness4Icon from '@mui/icons-material/Brightness4';
import Brightness7Icon from '@mui/icons-material/Brightness7';
import TranslateIcon from '@mui/icons-material/Translate';
import { ThemeModeContext, LanguageContext } from '../App';
import { useTranslate } from '../i18n';
import { setupApi } from '../services/api';

export default function SetupPage() {
    const navigate = useNavigate();
    const theme = useTheme();
    const colorMode = useContext(ThemeModeContext);
    const { toggleLanguage } = useContext(LanguageContext);
    const t = useTranslate();

    const [username, setUsername] = useState('admin');
    const [password, setPassword] = useState('');
    const [confirmPwd, setConfirmPwd] = useState('');
    const [bindMode, setBindMode] = useState<'all' | 'local' | 'lan'>('all');
    const [localIp, setLocalIp] = useState('127.0.0.1');
    const [port, setPort] = useState(8000);
    const [dataDir, setDataDir] = useState('');
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(false);
    const [success, setSuccess] = useState(false);
    const [checking, setChecking] = useState(true);

    useEffect(() => {
        setupApi.getStatus().then(data => {
            if (data.initialized) {
                navigate('/login', { replace: true });
                return;
            }
            setLocalIp(data.local_ip || '127.0.0.1');
            setDataDir(data.default_data_dir || '');
            setPort(data.default_port || 8000);
            setChecking(false);
        }).catch(() => setChecking(false));
    }, [navigate]);

    const getHost = () => {
        if (bindMode === 'all') return '0.0.0.0';
        if (bindMode === 'local') return '127.0.0.1';
        return localIp;
    };

    const handleSubmit = async () => {
        setError('');
        if (!username.trim()) { setError(t('setup.required')); return; }
        if (password.length < 6) { setError(t('setup.passwordTooShort')); return; }
        if (password !== confirmPwd) { setError(t('setup.passwordMismatch')); return; }

        setLoading(true);
        try {
            await setupApi.init({
                admin_username: username.trim(),
                admin_password: password,
                host: getHost(),
                port,
                data_dir: dataDir || undefined,
            });
            setSuccess(true);
            setTimeout(() => navigate('/admin', { replace: true }), 1500);
        } catch (err) {
            setError((err as Error).message || t('setup.error'));
        } finally {
            setLoading(false);
        }
    };

    if (checking) {
        return (
            <Box sx={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', bgcolor: 'background.default' }}>
                <CircularProgress />
            </Box>
        );
    }

    const inputSx = { '& .MuiOutlinedInput-root': { borderRadius: 2, bgcolor: theme.palette.mode === 'dark' ? 'rgba(0,0,0,0.2)' : '#fff' } };

    return (
        <Box sx={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', bgcolor: 'background.default', position: 'relative', p: 2 }}>
            <Box sx={{ position: 'absolute', top: 16, right: 16, display: 'flex', gap: 1, zIndex: 10 }}>
                <IconButton onClick={toggleLanguage} size="small" color="inherit"><TranslateIcon fontSize="small" /></IconButton>
                <IconButton onClick={colorMode.toggleTheme} size="small" color="inherit">
                    {theme.palette.mode === 'dark' ? <Brightness7Icon fontSize="small" /> : <Brightness4Icon fontSize="small" />}
                </IconButton>
            </Box>

            <Container maxWidth="sm" sx={{ zIndex: 2 }}>
                <Fade in timeout={600}>
                    <Paper elevation={0} sx={{
                        p: { xs: 3, md: 5 }, width: '100%',
                        background: theme.palette.background.paper,
                        border: `1px solid ${theme.palette.divider}`,
                        borderRadius: '16px',
                        boxShadow: theme.palette.mode === 'dark' ? '0 12px 40px rgba(0,0,0,0.5)' : '0 12px 40px rgba(0,0,0,0.1)',
                    }}>
                        {/* Header */}
                        <Box sx={{ textAlign: 'center', mb: 4 }}>
                            <Box component="img" src="https://napneko.github.io/assets/newnewlogo.png"
                                sx={{ width: 80, height: 80, borderRadius: '50%', mb: 2, filter: theme.palette.mode === 'dark' ? 'drop-shadow(0 4px 12px rgba(59,130,246,0.3))' : 'drop-shadow(0 4px 12px rgba(59,130,246,0.2))' }} />
                            <Typography variant="h5" sx={{ fontWeight: 700, mb: 0.5 }}>{t('setup.title')}</Typography>
                            <Typography variant="body2" color="text.secondary">{t('setup.subtitle')}</Typography>
                        </Box>

                        {success && <Alert severity="success" sx={{ mb: 3, borderRadius: 2 }}>{t('setup.success')}</Alert>}
                        {error && <Alert severity="error" sx={{ mb: 3, borderRadius: 2 }}>{error}</Alert>}

                        {/* Admin Account */}
                        <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1.5, display: 'flex', alignItems: 'center', gap: 1 }}>
                            <PersonIcon fontSize="small" color="primary" /> {t('setup.adminAccount')}
                        </Typography>
                        <TextField fullWidth size="small" label={t('setup.adminUsername')} value={username}
                            onChange={e => setUsername(e.target.value)}
                            InputProps={{ startAdornment: <InputAdornment position="start"><PersonIcon color="disabled" /></InputAdornment> }}
                            sx={{ mb: 2, ...inputSx }} />
                        <TextField fullWidth size="small" label={t('setup.adminPassword')} type="password" value={password}
                            onChange={e => setPassword(e.target.value)}
                            InputProps={{ startAdornment: <InputAdornment position="start"><LockIcon color="disabled" /></InputAdornment> }}
                            sx={{ mb: 2, ...inputSx }} />
                        <TextField fullWidth size="small" label={t('setup.confirmPassword')} type="password" value={confirmPwd}
                            onChange={e => setConfirmPwd(e.target.value)}
                            InputProps={{ startAdornment: <InputAdornment position="start"><LockIcon color="disabled" /></InputAdornment> }}
                            sx={{ mb: 3, ...inputSx }} />

                        <Divider sx={{ my: 2 }} />

                        {/* Network Settings */}
                        <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1.5, display: 'flex', alignItems: 'center', gap: 1 }}>
                            <PublicIcon fontSize="small" color="primary" /> {t('setup.networkSettings')}
                        </Typography>
                        <Typography variant="caption" color="text.secondary" sx={{ mb: 1.5, display: 'block' }}>{t('setup.bindAddress')}</Typography>
                        <ToggleButtonGroup exclusive fullWidth size="small" value={bindMode}
                            onChange={(_, v) => { if (v) setBindMode(v); }}
                            sx={{ mb: 2, '& .MuiToggleButton-root': { borderRadius: 2, textTransform: 'none', fontWeight: 500, fontSize: '0.8rem', py: 1.2 } }}>
                            <ToggleButton value="all">
                                <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 0.3 }}>
                                    <PublicIcon fontSize="small" />
                                    <span>0.0.0.0</span>
                                    <Typography variant="caption" sx={{ fontSize: '0.65rem', color: 'text.secondary', lineHeight: 1.1 }}>{t('setup.bindAllDesc')}</Typography>
                                </Box>
                            </ToggleButton>
                            <ToggleButton value="local">
                                <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 0.3 }}>
                                    <LaptopIcon fontSize="small" />
                                    <span>127.0.0.1</span>
                                    <Typography variant="caption" sx={{ fontSize: '0.65rem', color: 'text.secondary', lineHeight: 1.1 }}>{t('setup.bindLocalDesc')}</Typography>
                                </Box>
                            </ToggleButton>
                            <ToggleButton value="lan">
                                <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 0.3 }}>
                                    <LanIcon fontSize="small" />
                                    <span>{localIp}</span>
                                    <Typography variant="caption" sx={{ fontSize: '0.65rem', color: 'text.secondary', lineHeight: 1.1 }}>{t('setup.bindCustomDesc')}</Typography>
                                </Box>
                            </ToggleButton>
                        </ToggleButtonGroup>

                        <TextField fullWidth size="small" label={t('setup.servicePort')} type="number" value={port}
                            onChange={e => setPort(parseInt(e.target.value) || 8000)}
                            sx={{ mb: 3, ...inputSx }} />

                        <Divider sx={{ my: 2 }} />

                        {/* Data Directory */}
                        <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1.5, display: 'flex', alignItems: 'center', gap: 1 }}>
                            <FolderIcon fontSize="small" color="primary" /> {t('setup.dataSettings')}
                        </Typography>
                        <TextField fullWidth size="small" label={t('setup.dataDir')} value={dataDir}
                            onChange={e => setDataDir(e.target.value)}
                            helperText={t('setup.dataDirHelp')}
                            InputProps={{ startAdornment: <InputAdornment position="start"><FolderIcon color="disabled" /></InputAdornment> }}
                            sx={{ mb: 4, ...inputSx }} />

                        {/* Submit */}
                        <Button fullWidth variant="contained" onClick={handleSubmit} disabled={loading || success}
                            startIcon={loading ? <CircularProgress size={20} color="inherit" /> : <RocketLaunchIcon />}
                            sx={{
                                py: 1.5, borderRadius: 2, fontWeight: 600, fontSize: '1.05rem',
                                background: 'linear-gradient(90deg, #2563eb, #3b82f6)',
                                '&:hover': { boxShadow: '0 4px 16px rgba(37,99,235,0.4)' }
                            }}>
                            {loading ? t('setup.submitting') : t('setup.submit')}
                        </Button>
                    </Paper>
                </Fade>
            </Container>
        </Box>
    );
}

