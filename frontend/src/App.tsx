import { useState, useMemo, createContext, useEffect, lazy, Suspense } from 'react';
import { createTheme, ThemeProvider, CssBaseline, useMediaQuery, CircularProgress, Box } from '@mui/material';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import LoginPage from './pages/Login';
import SetupPage from './pages/Setup';
import AdminLayout from './layouts/AdminLayout';
import { ToastProvider } from './components/Toast';
import { ErrorBoundary } from './components/ErrorBoundary';
import { setupApi } from './services/api';
import { AuthProvider, useAuth } from './contexts/AuthContext';

// 路由懒加载 — 首屏只加载 Login/Setup/AdminLayout
const Dashboard = lazy(() => import('./pages/Dashboard'));
const UserDashboard = lazy(() => import('./pages/UserDashboard'));
const ConfigEditor = lazy(() => import('./pages/ConfigEditor'));
const ClusterSettings = lazy(() => import('./pages/ClusterSettings'));
const Nodes = lazy(() => import('./pages/Nodes'));
const OperationLogs = lazy(() => import('./pages/OperationLogsPage'));
const Users = lazy(() => import('./pages/Users'));
const ImageManager = lazy(() => import('./pages/ImageManager'));
const AlertSettings = lazy(() => import('./pages/AlertSettings'));
const BackupRestore = lazy(() => import('./pages/BackupRestore'));
const ScheduledTasks = lazy(() => import('./pages/ScheduledTasks'));
const BotShepherd = lazy(() => import('./pages/BotShepherd'));
const BotRadar = lazy(() => import('./pages/BotRadar'));

/** 管理员路由守卫：未登录跳转 login，非管理员跳转首页 */
function RequireAdmin({ children }: { children: React.ReactNode }) {
  const { user, loading, isAdmin } = useAuth();
  if (loading) return <Box sx={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><CircularProgress /></Box>;
  if (!user) return <Navigate to="/login" replace />;
  if (!isAdmin) return <Navigate to="/" replace />;
  return <>{children}</>;
}

/** 认证路由守卫：未登录跳转 login */
function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <Box sx={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><CircularProgress /></Box>;
  if (!user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export const ThemeModeContext = createContext({ toggleTheme: () => { } });
export const LanguageContext = createContext({ language: 'zh', toggleLanguage: () => { } });

// We define exact standard colors that match Napcat native
const getDesignTokens = (mode: 'light' | 'dark') => ({
  palette: {
    mode,
    ...(mode === 'light'
      ? {
        primary: { main: '#3b82f6' },
        background: { default: '#f3f4f6', paper: '#ffffff' },
        text: { primary: '#1f2937', secondary: '#4b5563' },
      }
      : {
        primary: { main: '#3b82f6' },
        background: { default: '#1e1e1e', paper: '#252526' },
        text: { primary: '#e5e7eb', secondary: '#9ca3af' },
      }),
  },
  typography: {
    fontFamily: '"Outfit", "Inter", "Roboto", "Helvetica", "Arial", sans-serif',
    // caption / overline 用 Space Grotesk，适合 QQ号、状态数值、标签
    caption: { fontFamily: '"Space Grotesk", "Outfit", sans-serif' },
    overline: { fontFamily: '"Space Grotesk", "Outfit", sans-serif' },
    subtitle2: { fontFamily: '"Space Grotesk", "Outfit", sans-serif', fontWeight: 600 },
  },
  components: {

    MuiTab: { styleOverrides: { root: { textTransform: 'none', fontWeight: 600 } } },
    MuiChip: { styleOverrides: { root: { fontFamily: '"Space Grotesk", "Outfit", sans-serif', fontWeight: 500 } } },
  },
});

function App() {
  const prefersDarkMode = useMediaQuery('(prefers-color-scheme: dark)');
  const [mode, setMode] = useState<'light' | 'dark'>(prefersDarkMode ? 'dark' : 'light');
  const [language, setLanguage] = useState('zh');
  const [initialized, setInitialized] = useState<boolean | null>(null);

  useEffect(() => {
    const savedMode = localStorage.getItem('themeMode');
    if (savedMode === 'light' || savedMode === 'dark') setMode(savedMode);
    const savedLang = localStorage.getItem('appLang');
    if (savedLang) setLanguage(savedLang);

    // 检查系统是否已初始化
    setupApi.getStatus()
      .then(data => setInitialized(data.initialized))
      .catch(() => setInitialized(true)); // 出错时默认已初始化，走正常登录流程
  }, []);

  const colorMode = useMemo(
    () => ({
      toggleTheme: () => {
        setMode((prev) => {
          const next = prev === 'light' ? 'dark' : 'light';
          localStorage.setItem('themeMode', next);
          return next;
        });
      },
    }),
    [],
  );

  const langMode = useMemo(
    () => ({
      language,
      toggleLanguage: () => {
        setLanguage((prev) => {
          const next = prev === 'zh' ? 'en' : 'zh';
          localStorage.setItem('appLang', next);
          return next;
        });
      }
    }),
    [language]
  );

  const theme = useMemo(() => createTheme(getDesignTokens(mode)), [mode]);

  // 等待初始化状态检查完成
  if (initialized === null) {
    return (
      <ThemeModeContext.Provider value={colorMode}>
        <LanguageContext.Provider value={langMode}>
          <ThemeProvider theme={theme}>
            <CssBaseline />
            <Box sx={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', bgcolor: 'background.default' }}>
              <CircularProgress />
            </Box>
          </ThemeProvider>
        </LanguageContext.Provider>
      </ThemeModeContext.Provider>
    );
  }

  return (
    <ErrorBoundary>
    <ThemeModeContext.Provider value={colorMode}>
      <LanguageContext.Provider value={langMode}>
        <ThemeProvider theme={theme}>
          <CssBaseline />
          <ToastProvider>
            <AuthProvider>
            <BrowserRouter>
              <Suspense fallback={<Box sx={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><CircularProgress /></Box>}>
              <Routes>
                {/* 首次部署：未初始化时所有路由重定向到 /setup */}
                <Route path="/setup" element={initialized ? <Navigate to="/login" replace /> : <ErrorBoundary><SetupPage /></ErrorBoundary>} />
                <Route path="/" element={initialized ? <ErrorBoundary><UserDashboard /></ErrorBoundary> : <Navigate to="/setup" replace />} />
                <Route path="/login" element={initialized ? <ErrorBoundary><LoginPage /></ErrorBoundary> : <Navigate to="/setup" replace />} />
                <Route path="/admin" element={initialized ? <RequireAuth><AdminLayout /></RequireAuth> : <Navigate to="/setup" replace />}>
                  <Route index element={<ErrorBoundary><Dashboard /></ErrorBoundary>} />
                  <Route path="config/:node_id/:name" element={<ErrorBoundary><ConfigEditor /></ErrorBoundary>} />
                  <Route path="cluster-settings" element={<RequireAdmin><ErrorBoundary><ClusterSettings /></ErrorBoundary></RequireAdmin>} />
                  <Route path="nodes" element={<RequireAdmin><ErrorBoundary><Nodes /></ErrorBoundary></RequireAdmin>} />
                  <Route path="users" element={<RequireAdmin><ErrorBoundary><Users /></ErrorBoundary></RequireAdmin>} />
                  <Route path="images" element={<RequireAdmin><ErrorBoundary><ImageManager /></ErrorBoundary></RequireAdmin>} />
                  <Route path="alerts" element={<RequireAdmin><ErrorBoundary><AlertSettings /></ErrorBoundary></RequireAdmin>} />
                  <Route path="backup" element={<RequireAdmin><ErrorBoundary><BackupRestore /></ErrorBoundary></RequireAdmin>} />
                  <Route path="scheduler" element={<RequireAdmin><ErrorBoundary><ScheduledTasks /></ErrorBoundary></RequireAdmin>} />
                  <Route path="botshepherd" element={<RequireAdmin><ErrorBoundary><BotShepherd /></ErrorBoundary></RequireAdmin>} />
                  <Route path="bot-radar" element={<RequireAdmin><ErrorBoundary><BotRadar /></ErrorBoundary></RequireAdmin>} />
                  <Route path="operation-logs" element={<RequireAdmin><ErrorBoundary><OperationLogs /></ErrorBoundary></RequireAdmin>} />
                </Route>
                <Route path="*" element={<Navigate to={initialized ? "/" : "/setup"} replace />} />
              </Routes>
              </Suspense>
            </BrowserRouter>
            </AuthProvider>
          </ToastProvider>
        </ThemeProvider>
      </LanguageContext.Provider>
    </ThemeModeContext.Provider>
    </ErrorBoundary>
  );
}

export default App;
