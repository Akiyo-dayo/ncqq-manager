import React from 'react';
import { Box, Typography, Button } from '@mui/material';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';

interface ErrorBoundaryProps {
    children: React.ReactNode;
}

interface ErrorBoundaryState {
    hasError: boolean;
    error: Error | null;
}

export class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
    constructor(props: ErrorBoundaryProps) {
        super(props);
        this.state = { hasError: false, error: null };
    }

    static getDerivedStateFromError(error: Error): ErrorBoundaryState {
        return { hasError: true, error };
    }

    componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
        console.error('[ErrorBoundary]', error, errorInfo);
    }

    handleReset = () => {
        this.setState({ hasError: false, error: null });
    };

    render() {
        if (this.state.hasError) {
            return (
                <Box sx={{
                    display: 'flex', flexDirection: 'column', alignItems: 'center',
                    justifyContent: 'center', minHeight: '100vh', gap: 3, p: 4,
                    textAlign: 'center',
                }}>
                    <ErrorOutlineIcon sx={{ fontSize: 64, color: 'error.main' }} />
                    <Typography variant="h5" fontWeight={700}>
                        页面发生错误 / Something went wrong
                    </Typography>
                    <Typography variant="body2" color="text.secondary" sx={{ maxWidth: 500, wordBreak: 'break-word' }}>
                        {this.state.error?.message || 'Unknown error'}
                    </Typography>
                    <Box sx={{ display: 'flex', gap: 2 }}>
                        <Button variant="contained" onClick={this.handleReset}>
                            重试 / Retry
                        </Button>
                        <Button variant="outlined" onClick={() => window.location.href = '/'}>
                            返回首页 / Home
                        </Button>
                    </Box>
                </Box>
            );
        }
        return this.props.children;
    }
}

