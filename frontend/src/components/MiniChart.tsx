/**
 * MiniChart - 轻量 SVG 折线图（无第三方依赖）
 * 用于显示 CPU / 内存监控历史数据
 */
import { Box, Typography, useTheme } from '@mui/material';

interface MiniChartProps {
    data: number[];
    label: string;
    color: string;
    unit?: string;
    height?: number;
}

export default function MiniChart({ data, label, color, unit = '%', height = 80 }: MiniChartProps) {
    const theme = useTheme();
    const width = 280;
    const padding = { top: 4, right: 4, bottom: 4, left: 4 };
    const chartW = width - padding.left - padding.right;
    const chartH = height - padding.top - padding.bottom;

    const maxVal = Math.max(...data, 1);
    const current = data.length > 0 ? data[data.length - 1] : 0;

    const points = data.map((v, i) => {
        const x = padding.left + (i / Math.max(data.length - 1, 1)) * chartW;
        const y = padding.top + chartH - (v / maxVal) * chartH;
        return `${x},${y}`;
    });

    const areaPoints = data.length > 0
        ? `${padding.left},${padding.top + chartH} ${points.join(' ')} ${padding.left + chartW},${padding.top + chartH}`
        : '';

    return (
        <Box sx={{
            p: 2, borderRadius: 3, flex: 1, minWidth: 200,
            bgcolor: theme.palette.mode === 'dark' ? 'rgba(30,30,32,0.35)' : 'rgba(255,255,255,0.25)',
            backdropFilter: 'blur(16px) saturate(1.2)',
            WebkitBackdropFilter: 'blur(16px) saturate(1.2)',
            border: `1px solid ${theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)'}`,
        }}>
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', mb: 1 }}>
                <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>{label}</Typography>
                <Typography variant="h6" sx={{ fontWeight: 700, color, lineHeight: 1 }}>
                    {current.toFixed(1)}{unit}
                </Typography>
            </Box>
            <svg width="100%" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
                {areaPoints && (
                    <polygon points={areaPoints} fill={color} opacity={0.1} />
                )}
                {points.length > 1 && (
                    <polyline
                        points={points.join(' ')}
                        fill="none"
                        stroke={color}
                        strokeWidth="2"
                        strokeLinejoin="round"
                        strokeLinecap="round"
                    />
                )}
            </svg>
        </Box>
    );
}

