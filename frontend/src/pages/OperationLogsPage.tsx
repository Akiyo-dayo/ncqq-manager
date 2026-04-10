import React, { useState } from 'react';
import { Box, Button, Paper, Typography, useTheme } from '@mui/material';

import { OperationLogsList } from '../components/OperationLogsList';
import { OperationLogsToolbar } from '../components/OperationLogsToolbar';
import { useToast } from '../components/Toast';
import { useOperationLogsFeed } from '../hooks/useOperationLogsFeed';
import { useTranslate } from '../i18n';
import { buildOperationLogsDownloadUrl } from '../services/operationLogs';

const OperationLogsPage: React.FC = () => {
    const [limit, setLimit] = useState(50);
    const [page, setPage] = useState(1);
    const [operator, setOperator] = useState('');
    const [type, setType] = useState('');
    const [level, setLevel] = useState<'info' | 'warning' | 'error' | ''>('');
    const theme = useTheme();
    const toast = useToast();
    const t = useTranslate();

    const { logs, loading, totalPages, pendingNewCount, highlightedLogIds, listRef, fetchLogs, scrollToTop } = useOperationLogsFeed(
        { limit, page, operator, type, level },
        (message) => toast.error(message),
    );

    const handleExport = () => {
        window.open(buildOperationLogsDownloadUrl({
            limit,
            page,
            operator: operator.trim(),
            type: type.trim(),
            level,
        }), '_blank');
    };

    return (
        <Box sx={{ p: 3 }}>
            <Paper sx={{ p: 3,
                bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
                backdropFilter: 'blur(16px) saturate(1.2)',
                WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
                border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
                boxShadow: 'none', borderRadius: 3 }}>
                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3, gap: 2, flexWrap: 'wrap' }}>
                    <Typography variant="h5">{t('opLogs.title')}</Typography>
                    <OperationLogsToolbar
                        limit={limit}
                        page={page}
                        level={level}
                        operator={operator}
                        type={type}
                        totalPages={totalPages}
                        loading={loading}
                        onLimitChange={(value) => { setLimit(value); setPage(1); }}
                        onPageChange={setPage}
                        onOperatorChange={(value) => { setOperator(value); setPage(1); }}
                        onTypeChange={(value) => { setType(value); setPage(1); }}
                        onLevelChange={(value) => { setLevel(value); setPage(1); }}
                        onRefresh={() => { void fetchLogs('manual'); }}
                        onExport={handleExport}
                        t={t}
                    />
                </Box>

                {pendingNewCount > 0 && (
                    <Box sx={{ position: 'sticky', top: 0, zIndex: 1, display: 'flex', justifyContent: 'center', py: 1, mb: 2, backgroundColor: 'background.paper' }}>
                        <Button size="small" variant="contained" onClick={scrollToTop} sx={{ textTransform: 'none' }}>
                            {t('opLogs.newLogsNotice').replace('{count}', String(pendingNewCount))}
                        </Button>
                    </Box>
                )}

                <OperationLogsList
                    logs={logs}
                    loading={loading}
                    highlightedLogIds={highlightedLogIds}
                    listRef={listRef}
                    t={t}
                />
            </Paper>
        </Box>
    );
};

export default OperationLogsPage;

