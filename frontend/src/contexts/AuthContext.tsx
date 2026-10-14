import { createContext, useContext, useState, useEffect, useRef, useCallback, type ReactNode } from 'react';
import { authApi, type AuthUser } from '../services/api';

interface AuthState {
    user: AuthUser | null;
    loading: boolean;
    isAdmin: boolean;
    refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthState>({
    user: null,
    loading: true,
    isAdmin: false,
    refresh: async () => { },
});

export function AuthProvider({ children }: { children: ReactNode }) {
    const [user, setUser] = useState<AuthUser | null>(null);
    const [loading, setLoading] = useState(true);

    // 只有确认登录过的人掉线才值得强制跳转：游客访问首页时 /auth/status 也会 401，
    // 不能把他们从公开面板一脚踢到登录页。
    const hadSessionRef = useRef(false);

    const refresh = useCallback(async () => {
        try {
            const data = await authApi.getStatus();
            if (data.status === 'ok' && data.user) {
                hadSessionRef.current = true;
                setUser(data.user);
            } else {
                setUser(null);
            }
        } catch {
            setUser(null);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { refresh(); }, [refresh]);

    // api.ts 的 401 拦截和 WS 的 4001 都会发这个事件，此前全仓没人接，
    // 于是会话过期后界面继续用空数据渲染，用户只能自己发现不对再手动刷新。
    useEffect(() => {
        const handleUnauthorized = () => {
            setUser(null);
            setLoading(false);
            if (!hadSessionRef.current) return;
            hadSessionRef.current = false;
            if (window.location.pathname === '/login') return;
            window.location.href = '/login';
        };
        window.addEventListener('auth:unauthorized', handleUnauthorized);
        return () => window.removeEventListener('auth:unauthorized', handleUnauthorized);
    }, []);

    const isAdmin = (user?.permission ?? 0) >= 10;

    return (
        <AuthContext.Provider value={{ user, loading, isAdmin, refresh }}>
            {children}
        </AuthContext.Provider>
    );
}

export function useAuth() {
    return useContext(AuthContext);
}
