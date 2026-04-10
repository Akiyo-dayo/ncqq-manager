import { Box, Button, FormControl, MenuItem, Select, TextField } from '@mui/material';
import { Download as DownloadIcon, Refresh as RefreshIcon } from '@mui/icons-material';

interface OperationLogsToolbarProps {
    limit: number;
    level: 'info' | 'warning' | 'error' | '';
    operator: string;
    type: string;
    loading: boolean;
    onLimitChange: (value: number) => void;
    onOperatorChange: (value: string) => void;
    onTypeChange: (value: string) => void;
    onLevelChange: (value: 'info' | 'warning' | 'error' | '') => void;
    onRefresh: () => void;
    onExport: () => void;
    t: (key: string) => string;
}

export function OperationLogsToolbar({
    limit,
    level,
    operator,
    type,
    loading,
    onLimitChange,
    onOperatorChange,
    onTypeChange,
    onLevelChange,
    onRefresh,
    onExport,
    t,
}: OperationLogsToolbarProps) {
    return (
        <Box sx={{ display: 'flex', gap: 2, alignItems: 'center', flexWrap: 'wrap' }}>
            <FormControl size="small" sx={{ minWidth: 120 }}>
                <Select value={limit} onChange={(e) => onLimitChange(Number(e.target.value))}>
                    <MenuItem value={20}>20 {t('opLogs.records')}</MenuItem>
                    <MenuItem value={50}>50 {t('opLogs.records')}</MenuItem>
                    <MenuItem value={100}>100 {t('opLogs.records')}</MenuItem>
                    <MenuItem value={200}>200 {t('opLogs.records')}</MenuItem>
                </Select>
            </FormControl>
            <TextField size="small" label={t('opLogs.operator')} value={operator} onChange={(e) => onOperatorChange(e.target.value)} />
            <TextField size="small" label={t('opLogs.type')} value={type} onChange={(e) => onTypeChange(e.target.value)} />
            <FormControl size="small" sx={{ minWidth: 120 }}>
                <Select displayEmpty value={level} onChange={(e) => onLevelChange(e.target.value as 'info' | 'warning' | 'error' | '')}>
                    <MenuItem value="">{t('opLogs.allLevels')}</MenuItem>
                    <MenuItem value="info">info</MenuItem>
                    <MenuItem value="warning">warning</MenuItem>
                    <MenuItem value="error">error</MenuItem>
                </Select>
            </FormControl>
            <Button variant="outlined" startIcon={<RefreshIcon />} onClick={onRefresh} disabled={loading}>
                {t('admin.refresh')}
            </Button>
            <Button variant="outlined" startIcon={<DownloadIcon />} onClick={onExport}>
                {t('config.exportLogs')}
            </Button>
        </Box>
    );
}

