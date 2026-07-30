import React, { createContext, useContext, useState, useRef, useCallback } from 'react';
import { Snackbar, Alert } from '@mui/material';
import type { AlertColor } from '@mui/material';

interface ToastMessage {
    id: number;
    message: string;
    severity: AlertColor;
}

interface ToastContextType {
    showToast: (message: string, severity?: AlertColor) => void;
    success: (message: string) => void;
    error: (message: string) => void;
    warning: (message: string) => void;
    info: (message: string) => void;
}

const ToastContext = createContext<ToastContextType | undefined>(undefined);

export const useToast = () => {
    const context = useContext(ToastContext);
    if (!context) {
        throw new Error('useToast must be used within ToastProvider');
    }
    return context;
};

export const ToastProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const [toasts, setToasts] = useState<ToastMessage[]>([]);
    // 批量操作会在同一毫秒里连发多条，Date.now() 当 id 会撞车：
    // 关掉其中一条时 filter 会把同 id 的另一条一起删掉。
    const nextIdRef = useRef(0);

    const showToast = useCallback((message: string, severity: AlertColor = 'info') => {
        nextIdRef.current += 1;
        setToasts(prev => [...prev, { id: nextIdRef.current, message, severity }]);
    }, []);

    const success = useCallback((message: string) => showToast(message, 'success'), [showToast]);
    const error = useCallback((message: string) => showToast(message, 'error'), [showToast]);
    const warning = useCallback((message: string) => showToast(message, 'warning'), [showToast]);
    const info = useCallback((message: string) => showToast(message, 'info'), [showToast]);

    const handleClose = (id: number) => {
        setToasts(prev => prev.filter(toast => toast.id !== id));
    };

    return (
        <ToastContext.Provider value={{ showToast, success, error, warning, info }}>
            {children}
            {toasts.map((toast, index) => (
                <Snackbar
                    key={toast.id}
                    open={true}
                    autoHideDuration={4000}
                    onClose={() => handleClose(toast.id)}
                    anchorOrigin={{ vertical: 'top', horizontal: 'right' }}
                    sx={{ mt: index * 7 }}
                >
                    <Alert 
                        onClose={() => handleClose(toast.id)} 
                        severity={toast.severity}
                        variant="filled"
                        sx={{ width: '100%' }}
                    >
                        {toast.message}
                    </Alert>
                </Snackbar>
            ))}
        </ToastContext.Provider>
    );
};

