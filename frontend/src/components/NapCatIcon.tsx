import React from 'react';
import { Box } from '@mui/material';
import type { SxProps, Theme } from '@mui/material/styles';

export default function NapCatIcon({ sx, fontSize = 'medium' }: { sx?: SxProps<Theme>, fontSize?: 'small' | 'medium' | 'large' | string }) {
    const sizeMap: Record<string, number> = { small: 20, medium: 24, large: 32 };
    const size = sizeMap[fontSize] || 24;
    return (
        <Box
            component="img"
            src="https://napneko.github.io/assets/newnewlogo.png"
            alt="NapCat"
            sx={{
                width: size,
                height: size,
                borderRadius: '50%',
                objectFit: 'cover',
                ...sx
            }}
        />
    );
}
