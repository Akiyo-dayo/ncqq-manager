import type { MutableRefObject } from 'react';

import { Box, Chip, CircularProgress, List, ListItem, ListItemIcon, ListItemText, Typography } from '@mui/material';
import { FiberManualRecord as DotIcon } from '@mui/icons-material';

import { type OperationLog } from '../services/operationLogs';

export function getOperationLogLevelColor(level: string): 'info' | 'warning' | 'error' | 'default' {
    switch (level) {
        case 'info': return 'info';
        case 'warning': return 'warning';
        case 'error': return 'error';
        default: return 'default';
    }
}

export function formatOperationLogText(log: OperationLog, t: (key: string) => string): string {
    const operator = log.operator || log.id;
    const target = log.target || '';
    switch (log.type) {
        case 'container_start': return t('opLogs.containerStart').replace('{operator}', operator).replace('{target}', target);
        case 'container_stop': return t('opLogs.containerStop').replace('{operator}', operator).replace('{target}', target);
        case 'container_restart': return t('opLogs.containerRestart').replace('{operator}', operator).replace('{target}', target);
        case 'container_create': return t('opLogs.containerCreate').replace('{operator}', operator).replace('{target}', target);
        case 'container_delete': return t('opLogs.containerDelete').replace('{operator}', operator).replace('{target}', target);
        case 'user_login': return t('opLogs.userLogin').replace('{operator}', operator).replace('{ip}', log.operator_ip || 'unknown');
        case 'user_create': return t('opLogs.userCreate').replace('{operator}', operator).replace('{target}', target);
        case 'user_delete': return t('opLogs.userDelete').replace('{operator}', operator).replace('{target}', target);
        case 'config_change': return t('opLogs.configChange').replace('{operator}', operator);
        case 'node_create': return t('opLogs.nodeCreate').replace('{operator}', operator).replace('{target}', target);
        case 'node_delete': return t('opLogs.nodeDelete').replace('{operator}', operator).replace('{target}', target);
        default: return t('opLogs.unknownAction').replace('{operator}', operator).replace('{type}', log.type);
    }
}

interface OperationLogsListProps {
    logs: OperationLog[];
    loading: boolean;
    highlightedLogIds: string[];
    listRef: MutableRefObject<HTMLUListElement | null>;
    t: (key: string) => string;
}

export function OperationLogsList({ logs, loading, highlightedLogIds, listRef, t }: OperationLogsListProps) {
    if (loading) return <Box sx={{ display: 'flex', justifyContent: 'center', py: 5 }}><CircularProgress /></Box>;
    if (logs.length === 0) {
        return (
            <Box sx={{ textAlign: 'center', py: 5, color: 'text.secondary' }}>
                <Typography variant="body1">{t('opLogs.noLogs')}</Typography>
                <Typography variant="body2" sx={{ mt: 1 }}>{t('opLogs.noLogsHint')}</Typography>
            </Box>
        );
    }
    return (
        <List ref={listRef} dense sx={{ maxHeight: '70vh', overflowY: 'auto' }}>
            {logs.map((log) => {
                const color = getOperationLogLevelColor(log.level);
                const iconColor = color === 'info' ? 'info.main' : color === 'warning' ? 'warning.main' : color === 'error' ? 'error.main' : 'grey.500';
                return (
                    <ListItem key={log.id || log.timestamp} sx={{ borderBottom: '1px solid', borderColor: 'divider', backgroundColor: highlightedLogIds.includes(log.id) ? 'action.hover' : 'transparent', transition: 'background-color 0.6s ease' }}>
                        <ListItemIcon sx={{ minWidth: 32 }}><DotIcon sx={{ fontSize: 12, color: iconColor }} /></ListItemIcon>
                        <ListItemText primary={formatOperationLogText(log, t)} secondary={log.time} />
                        <Chip label={log.level} size="small" color={color} variant="outlined" />
                    </ListItem>
                );
            })}
        </List>
    );
}

