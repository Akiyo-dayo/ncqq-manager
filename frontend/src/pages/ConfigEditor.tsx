import { useState } from 'react';
import {
    Box, Typography, IconButton, Tabs, Tab, useTheme
} from '@mui/material';
import { useParams, useNavigate } from 'react-router-dom';
import ArrowBackIcon from '@mui/icons-material/ArrowBack';
import InfoRoundedIcon from '@mui/icons-material/InfoRounded';
import HubRoundedIcon from '@mui/icons-material/HubRounded';
import FolderRoundedIcon from '@mui/icons-material/FolderRounded';
import TerminalRoundedIcon from '@mui/icons-material/TerminalRounded';
import { useTranslate } from '../i18n';
import { useAuth } from '../contexts/AuthContext';
import { BasicInfo } from '../components/BasicInfo';
import { NetworkConfig } from '../components/NetworkConfig';
import FileManager from '../components/FileManager';
import { NapcatLogs } from '../components/NapcatLogs';

export default function ConfigEditor() {
    const { name, node_id } = useParams();
    const navigate = useNavigate();
    const [activeTab, setActiveTab] = useState(0);
    const t = useTranslate();
    const theme = useTheme();
    const { isAdmin } = useAuth();

    // 普通用户只允许看基本信息和日志，管理员可见全部
    const tabs = [
        { key: 'basicInfo', icon: <InfoRoundedIcon fontSize="small" />, label: t('config.basicInfo'), adminOnly: false },
        { key: 'networkConfig', icon: <HubRoundedIcon fontSize="small" />, label: t('config.networkConfig'), adminOnly: true },
        { key: 'fileManager', icon: <FolderRoundedIcon fontSize="small" />, label: t('config.fileManager'), adminOnly: true },
        { key: 'napcatLogs', icon: <TerminalRoundedIcon fontSize="small" />, label: t('config.napcatLogs'), adminOnly: false },
    ].filter(tab => !tab.adminOnly || isAdmin);

    return (
        <Box sx={{ display: 'flex', flexDirection: 'column', minHeight: '100%' }}>
            {/* 顶部悬浮导航栏 */}
            <Box sx={{
                width: '100%',
                bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,30,0.85)' : 'rgba(255,255,255,0.85)',
                backdropFilter: 'blur(10px)',
                borderBottom: 1,
                borderColor: 'divider',
                position: 'sticky',
                top: 0,
                zIndex: 100,
                px: { xs: 2, md: 4 },
                display: 'flex',
                alignItems: 'center',
                gap: 3
            }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, minWidth: 'fit-content', py: 2 }}>
                    <IconButton size="small" onClick={() => { const sp = new URLSearchParams(window.location.search); const nodeParam = sp.get('node'); navigate(nodeParam ? `/admin?node=${nodeParam}` : '/admin'); }} sx={{ border: '1px solid', borderColor: 'divider', bgcolor: theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.05)' : '#f8fafc' }}>
                        <ArrowBackIcon fontSize="small" />
                    </IconButton>
                    <Typography variant="subtitle1" sx={{ fontWeight: 700, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {name}
                    </Typography>
                </Box>

                <Tabs
                    orientation="horizontal"
                    value={activeTab}
                    onChange={(e, v) => setActiveTab(v)}
                    variant="scrollable"
                    scrollButtons="auto"
                    sx={{
                        minHeight: 64,
                        '& .MuiTabs-flexContainer': {
                            height: '100%',
                            alignItems: 'center',
                            gap: 1
                        },
                        '& .MuiTab-root': {
                            textTransform: 'none',
                            fontSize: '0.95rem',
                            minHeight: 40,
                            height: 40,
                            borderRadius: 2,
                            px: 2,
                            color: 'text.secondary',
                            display: 'flex',
                            flexDirection: 'row',
                            alignItems: 'center',
                            gap: 1
                        },
                        '& .Mui-selected': {
                            bgcolor: theme.palette.mode === 'dark' ? 'rgba(59, 130, 246, 0.15)' : '#eff6ff',
                            color: '#3b82f6',
                            fontWeight: 600
                        },
                        '& .MuiTabs-indicator': { display: 'none' }
                    }}
                >
                    <Tab icon={<InfoRoundedIcon fontSize="small" />} iconPosition="start" label={t('config.basicInfo')} />
                    {isAdmin && <Tab icon={<HubRoundedIcon fontSize="small" />} iconPosition="start" label={t('config.networkConfig')} />}
                    {isAdmin && <Tab icon={<FolderRoundedIcon fontSize="small" />} iconPosition="start" label={t('config.fileManager')} />}
                    <Tab icon={<TerminalRoundedIcon fontSize="small" />} iconPosition="start" label={t('config.napcatLogs')} />
                </Tabs>
            </Box>

            {/* 主内容区 */}
            <Box sx={{ flex: 1, p: { xs: 2, md: 4 }, overflowY: 'auto' }}>
                <Box sx={{ width: '100%', maxWidth: 1600, mx: 'auto' }}>
                    {tabs[activeTab]?.key === 'basicInfo' && <BasicInfo name={name as string} node_id={node_id as string} />}
                    {tabs[activeTab]?.key === 'networkConfig' && <NetworkConfig name={name as string} node_id={node_id as string} />}
                    {tabs[activeTab]?.key === 'fileManager' && <FileManager name={name as string} node_id={node_id as string} />}
                    {tabs[activeTab]?.key === 'napcatLogs' && <NapcatLogs name={name as string} node_id={node_id as string} />}
                </Box>
            </Box>
        </Box>
    );
}

