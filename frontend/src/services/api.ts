/**
 * 统一 API 客户端
 * 封装所有后端 API 调用，提供类型安全和错误处理
 */

// ============ 类型定义 ============

export interface Container {
    id: string;
    name: string;
    status: string;
    image: string;
    created: string;
    node_id: string;
    uin?: string;
    bot_online?: boolean;
    bot_heartbeat_ts?: number;
}

export interface ContainerStats {
    status: string;
    created: string;
    cpu_percent: number;
    mem_usage: number;
    mem_limit: number;
    uin: string;
    version: string;
    webui_token: string;
    webui_port: number;
    platform: string;
    uptime_formatted: string;
    network_endpoints: {
        http: number;
        ws: number;
        http_client: number;
        ws_client: number;
    };
}

export interface Node {
    id: string;
    name: string;
    address: string;
    api_key?: string; // 服务端不再下发，前端仅供编辑表单暂存
    status?: string;
    container_count?: number;
    ping?: number;
    system?: {
        cpu_percent: number;
        mem_percent: number;
        platform: string;
        python_version: string;
        app_version?: string;
    };
    instances?: {
        total: number;
        running: number;
    };
    chart?: {
        cpu: number[];
        mem: number[];
    };
}

export interface InstanceRef {
    node_id: string;
    container_name: string;
}

export interface User {
    uuid: string;
    userName: string;
    permission: number;
    hasApiKey?: boolean; // 列表接口仅返回是否已配置，不返回明文
    instances?: InstanceRef[];
}

export interface AuthUser {
    uuid: string;
    userName: string;
    permission: number;
}

export type {
    OperationLog,
    OperationLogsQuery,
    OperationLogsResponse,
} from './operationLogs';

export interface FileItem {
    name: string;
    size: number;
    mtime: number;
}

export interface FolderItem {
    name: string;
}

export interface ClusterConfig {
    base_port: number;
    docker_image: string;
    data_dir: string;
    api_key: string;
}

export interface CreateContainerRequest {
    name: string;
    node_id?: string;
    docker_image?: string;
    webui_port?: number;
    http_port?: number;
    ws_port?: number;
    memory_limit?: number;
    restart_policy?: string;
    network_mode?: string;
    env_vars?: string[];
}

export interface UserEditPayload {
    userName?: string;
    passWord?: string;
    permission?: number;
}

export interface DockerImage {
    id: string;
    tags: string[];
    size: number;
    created: string;
}

export interface AlertRule {
    id: string;
    name: string;
    type: string;
    enabled: boolean;
    config: Record<string, unknown>;
    webhook_url: string;
    created_at: number;
}

export interface AlertHistory {
    id: number;
    rule_id: string;
    message: string;
    level: string;
    created_at: number;
}

const API_BASE = '/api';

// 认证错误类 — 供上层区分 401 和其他异常
export class AuthError extends Error {
    constructor(message: string = 'Unauthorized') {
        super(message);
        this.name = 'AuthError';
    }
}

// 通用请求封装
async function request<T>(
    url: string,
    options: RequestInit = {}
): Promise<T> {
    const response = await fetch(`${API_BASE}${url}`, {
        ...options,
        credentials: 'include',
        headers: {
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
            ...options.headers,
        },
    });

    if (response.status === 401) {
        // 触发全局事件，让路由层决定如何处理
        window.dispatchEvent(new CustomEvent('auth:unauthorized'));
        throw new AuthError();
    }

    if (!response.ok) {
        const error = await response.json().catch(() => ({ message: 'Request failed' }));
        throw new Error(error.message || `HTTP ${response.status}`);
    }

    return response.json();
}

// ============ 公开 API（无需认证） ============

export const publicApi = {
    // 公开容器列表 - 返回基本状态与登录信息
    containers: async (): Promise<{ status: string; containers: Container[] }> => {
        const response = await fetch(`${API_BASE}/public/containers`, {
            credentials: 'include',
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        return response.json();
    },

    // 获取二维码（不走 request 封装，避免 401 事件）— 单实例管理页面用
    getQR: async (name: string, nodeId: string = 'local'): Promise<{ status: string; url?: string; type?: string; uin?: string; generated_at?: number; expires_in?: number; expires_at?: number }> => {
        const response = await fetch(`${API_BASE}/containers/${name}/qrcode?node_id=${nodeId}`, {
            credentials: 'include',
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        return response.json();
    },

    // 批量获取所有容器的 QR 状态（用户面板用，一次请求替代 N 个独立请求）
    batchQR: async (): Promise<{ status: string; items: Record<string, { status: string; url?: string; type?: string; uin?: string }> }> => {
        const response = await fetch(`${API_BASE}/public/qr/batch`, {
            credentials: 'include',
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        return response.json();
    },

    // 刷新登录状态（不走 request 封装，用户面板无需认证）
    refreshLogin: async (name: string, nodeId: string = 'local'): Promise<{
        status: string; logged_in: boolean; uin?: string; nickname?: string; method?: string;
    }> => {
        const response = await fetch(`${API_BASE}/containers/${name}/refresh-login?node_id=${nodeId}`, {
            method: 'POST',
            credentials: 'include',
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        return response.json();
    },
};

// ============ 容器相关 API ============

export const containerApi = {
    // 获取容器列表
    list: (nodeId?: string) =>
        request<{ status: string; containers: Container[] }>(
            nodeId ? `/containers?node_id=${nodeId}` : '/containers'
        ),

    // 获取容器统计信息
    getStats: (name: string, nodeId: string = 'local') =>
        request<ContainerStats>(`/containers/${name}/stats?node_id=${nodeId}`),

    // 获取容器日志
    getLogs: (name: string, lines: number = 200, nodeId: string = 'local') =>
        request<{ status: string; logs: string }>(`/containers/${name}/logs?lines=${lines}&node_id=${nodeId}`),

    // 容器操作 (start/stop/restart/delete)
    action: (name: string, action: string, nodeId: string = 'local', deleteData: boolean = false) =>
        request<{ status: string }>(`/containers/${name}/action?action=${action}&node_id=${nodeId}&delete_data=${deleteData}`, {
            method: 'POST',
        }),

    // 创建容器
    create: (data: CreateContainerRequest) =>
        request<{ status: string; container_id: string }>('/containers', {
            method: 'POST',
            body: JSON.stringify(data),
        }),

    // 获取二维码
    getQR: (name: string, nodeId: string = 'local') =>
        request<{ status: string; url?: string; type?: string; uin?: string; generated_at?: number; expires_in?: number; expires_at?: number }>(`/containers/${name}/qrcode?node_id=${nodeId}`),

    // 刷新登录状态（管理员用，走 request 封装含 401 处理）
    refreshLogin: (name: string, nodeId: string = 'local') =>
        request<{ status: string; logged_in: boolean; uin?: string; nickname?: string; method?: string }>(
            `/containers/${name}/refresh-login?node_id=${nodeId}`, { method: 'POST' }
        ),

    // 获取配置文件
    getConfig: (name: string, filename: string, nodeId: string = 'local') =>
        request<{ status: string; content: string }>(`/containers/${name}/config/${filename}?node_id=${nodeId}`),

    // 保存配置文件
    saveConfig: (name: string, filename: string, content: string, nodeId: string = 'local') =>
        request<{ status: string }>(`/containers/${name}/config/${filename}?node_id=${nodeId}`, {
            method: 'POST',
            body: JSON.stringify({ content }),
        }),

    // 获取文件列表
    listFiles: (name: string, path: string = '', nodeId: string = 'local') =>
        request<{ status: string; files: FileItem[]; folders: FolderItem[]; current_path: string }>(
            `/containers/${name}/files?path=${encodeURIComponent(path)}&node_id=${nodeId}`
        ),

    // 删除文件或文件夹
    deleteFile: (name: string, path: string, nodeId: string = 'local') =>
        request<{ status: string }>(
            `/containers/${name}/files?path=${encodeURIComponent(path)}&node_id=${nodeId}`,
            { method: 'DELETE' }
        ),
};

// ============ 节点相关 API ============

export const nodeApi = {
    // 获取节点列表（quick=true 跳过远程健康检查，首屏快速渲染）
    list: (quick?: boolean) => request<{ status: string; nodes: Node[] }>(quick ? '/nodes?quick=true' : '/nodes'),

    // 添加节点
    add: (name: string, address: string, apiKey: string) =>
        request<{ status: string }>('/nodes', {
            method: 'POST',
            body: JSON.stringify({ name, address, api_key: apiKey }),
        }),

    // 编辑节点
    edit: (nodeId: string, name: string, address: string, apiKey: string) =>
        request<{ status: string }>(`/nodes/${nodeId}`, {
            method: 'PUT',
            body: JSON.stringify({ name, address, api_key: apiKey }),
        }),

    // 删除节点
    delete: (nodeId: string) =>
        request<{ status: string }>(`/nodes/${nodeId}`, {
            method: 'DELETE',
        }),

    // 获取集群配置
    getClusterConfig: () => request<ClusterConfig>('/cluster/config'),

    // 保存集群配置
    saveClusterConfig: (config: Partial<ClusterConfig>) =>
        request<{ status: string }>('/cluster/config', {
            method: 'POST',
            body: JSON.stringify(config),
        }),

    // 获取节点程序日志
    getLogs: (nodeId: string = 'local', lines: number = 500) =>
        request<{ status: string; logs: string }>(`/node/logs?node_id=${nodeId}&lines=${lines}`),

    // 获取主机监控数据
    getMonitor: () =>
        request<{
            status: string; cpu: number[]; mem: number[];
            current_cpu: number; current_mem: number;
            instances: { total: number; running: number };
        }>('/node/monitor'),
};

// ============ 用户相关 API ============

export const userApi = {
    // 获取用户列表
    list: (page: number = 1, pageSize: number = 20, search: string = '') =>
        request<{ status: string; data: User[] }>(`/users?page=${page}&page_size=${pageSize}&search=${encodeURIComponent(search)}`),

    // 创建用户
    create: (username: string, password: string, permission: number = 1) =>
        request<{ status: string; uuid: string; userName: string }>('/users', {
            method: 'POST',
            body: JSON.stringify({ username, password, permission }),
        }),

    // 编辑用户
    edit: (uuid: string, data: UserEditPayload) =>
        request<{ status: string }>(`/users/${uuid}`, {
            method: 'PUT',
            body: JSON.stringify(data),
        }),

    // 删除用户
    delete: (uuid: string) =>
        request<{ status: string }>(`/users/${uuid}`, {
            method: 'DELETE',
        }),

    // 分配实例
    assignInstances: (uuid: string, instances: InstanceRef[]) =>
        request<{ status: string }>(`/users/${uuid}/instances`, {
            method: 'PUT',
            body: JSON.stringify({ instances }),
        }),

    // 重新生成 API Key
    regenerateApiKey: (uuid: string) =>
        request<{ status: string; apiKey: string }>(`/users/${uuid}/apikey`, {
            method: 'PUT',
        }),
};

// ============ 操作日志 API ============

export {
    buildOperationLogsDownloadUrl,
    operationLogsApi,
} from './operationLogs';

// ============ 镜像管理 API ============

export const imageApi = {
    list: () => request<{ status: string; images: DockerImage[] }>('/images'),

    pull: (image: string) =>
        request<{ status: string }>('/images/pull', {
            method: 'POST',
            body: JSON.stringify({ image }),
        }),

    delete: (imageId: string, force: boolean = false) =>
        request<{ status: string }>(`/images/${imageId}?force=${force}`, {
            method: 'DELETE',
        }),
};

// ============ 告警管理 API ============

export const alertApi = {
    listRules: () =>
        request<{ status: string; rules: AlertRule[] }>('/alerts/rules'),

    createRule: (data: { name: string; type: string; config: Record<string, unknown>; webhook_url: string }) =>
        request<{ status: string; rule_id: string }>('/alerts/rules', {
            method: 'POST',
            body: JSON.stringify(data),
        }),

    updateRule: (ruleId: string, data: Partial<{ name: string; enabled: boolean; config: Record<string, unknown>; webhook_url: string }>) =>
        request<{ status: string }>(`/alerts/rules/${ruleId}`, {
            method: 'PUT',
            body: JSON.stringify(data),
        }),

    deleteRule: (ruleId: string) =>
        request<{ status: string }>(`/alerts/rules/${ruleId}`, { method: 'DELETE' }),

    getHistory: (limit: number = 50) =>
        request<{ status: string; history: AlertHistory[] }>(`/alerts/history?limit=${limit}`),

    getSettings: () =>
        request<{ status: string; allow_local_webhook: boolean }>('/alerts/settings'),

    updateSettings: (data: { allow_local_webhook: boolean }) =>
        request<{ status: string }>('/alerts/settings', {
            method: 'PUT',
            body: JSON.stringify(data),
        }),
};

// ============ 备份管理 API ============

export const backupApi = {
    getInfo: () =>
        request<{ status: string; info: { exists: boolean; size: number; modified: string; path: string } }>('/backup/info'),

    download: () => {
        window.open('/api/backup/download', '_blank');
    },

    upload: async (file: File) => {
        const formData = new FormData();
        formData.append('file', file);
        const resp = await fetch(`${API_BASE}/backup/upload`, {
            method: 'POST',
            credentials: 'include',
            headers: { 'X-Requested-With': 'XMLHttpRequest' },
            body: formData,
        });
        if (!resp.ok) throw new Error('Upload failed');
        return resp.json() as Promise<{ status: string; message: string }>;
    },
};

// ============ 定时任务 API ============

export interface ScheduledTask {
    id: string;
    name: string;
    type: string;
    enabled: boolean;
    interval_seconds: number;
    config: Record<string, unknown>;
    last_run: number;
    created_at: number;
}

export const schedulerApi = {
    list: () =>
        request<{ status: string; tasks: ScheduledTask[] }>('/scheduler/tasks'),

    create: (data: { name: string; type: string; interval_seconds: number; config?: Record<string, unknown> }) =>
        request<{ status: string; task_id: string }>('/scheduler/tasks', {
            method: 'POST',
            body: JSON.stringify(data),
        }),

    update: (taskId: string, data: Partial<{ name: string; enabled: boolean; interval_seconds: number; config: Record<string, unknown> }>) =>
        request<{ status: string }>(`/scheduler/tasks/${taskId}`, {
            method: 'PUT',
            body: JSON.stringify(data),
        }),

    delete: (taskId: string) =>
        request<{ status: string }>(`/scheduler/tasks/${taskId}`, { method: 'DELETE' }),
};

// ============ 认证相关 API ============

export const authApi = {
    // 登录
    login: (username: string, password: string) =>
        request<{ status: string; message?: string; user: AuthUser }>('/login', {
            method: 'POST',
            body: JSON.stringify({ username, password }),
        }),

    // 登出
    logout: () =>
        request<{ status: string }>('/logout', {
            method: 'POST',
        }),

    // 获取认证状态
    getStatus: () => request<{ status: string; user: AuthUser }>('/auth/status'),
};

// ============ 首次初始化 API ============

export interface SetupStatus {
    status: string;
    initialized: boolean;
    local_ip: string;
    default_data_dir: string;
    default_port: number;
}

export interface SetupRequest {
    admin_username: string;
    admin_password: string;
    host: string;
    port: number;
    data_dir?: string;
}

export const setupApi = {
    // 获取初始化状态（不需要认证）
    getStatus: () =>
        fetch(`${API_BASE}/setup/status`, { credentials: 'include' })
            .then(r => r.json()) as Promise<SetupStatus>,

    // 执行首次初始化（不需要认证）
    init: (data: SetupRequest) =>
        fetch(`${API_BASE}/setup/init`, {
            method: 'POST',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: JSON.stringify(data),
        }).then(async r => {
            const json = await r.json();
            if (!r.ok) throw new Error(json.message || 'Setup failed');
            return json as { status: string; message: string; user: AuthUser };
        }),
};

// ============ BotShepherd API ============

export interface BSActivationConnection {
    id: string;
    name: string;
    enabled: boolean;
    client_status: string;
    ws_alive: boolean;
    has_manager_endpoint: boolean;
    ws_registered: boolean;
    self_id: number | string | null;
    last_seen: number;
}

export interface BSActivationStatus {
    enabled: boolean;
    running: boolean;
    status: string;
    connected: boolean;
    source: string;
    last_error: string;
    last_check_at: number;
    total_connections: number;
    managed_connections: number;
    active_connections: number;
    injected_connections: number;
    missing_endpoints: string[];
    connections: BSActivationConnection[];
}




export interface BotShepherdStatus {
    installed: boolean;
    initialized: boolean;
    running: boolean;
    port: number;
    pid: number | null;
    auto_start: boolean;
    dir: string;
    webui_port: number | null;
    activation?: BSActivationStatus;
}

export interface BSConnectionStatus {
    enabled: boolean;
    client_status: 'disabled' | 'starting' | 'listening' | 'connected' | 'error';
    client_endpoint: string;
    target_statuses: Record<string, unknown>;
    error: string | null;
    client_address?: string;
    self_id?: number | null;
}

export interface BSConnection {
    name?: string;
    description?: string;
    enabled?: boolean;
    client_endpoint?: string;
    target_endpoints?: string[];
    group?: string;
    status?: BSConnectionStatus;
}

export interface BSConnectionsResponse {
    source: 'api' | 'file';
    connections: Record<string, BSConnection>;
}

export interface BSAccount {
    name?: string;
    description?: string;
    enabled?: boolean;
    aliases?: Record<string, string[]>;
    last_receive_time?: string;
    last_send_time?: string;
}

export interface BSAccountsResponse {
    source: 'api' | 'file';
    accounts: Record<string, BSAccount>;
}

/** Bot 雷达端点库条目 */
export interface RadarEndpoint {
    alias: string;       // 别名（唯一标识，用于 inject-by-alias 调用）
    url: string;         // ws:// 地址
    token: string;       // 可选 Bearer token
}

export const botshepherdApi = {
    status: () => request<BotShepherdStatus>('/botshepherd/status'),
    setup: () => request<{ status: string; message: string }>('/botshepherd/setup', { method: 'POST' }),
    start: () => request<{ status: string; message: string }>('/botshepherd/start', { method: 'POST' }),
    stop: () => request<{ status: string; message: string }>('/botshepherd/stop', { method: 'POST' }),
    logs: (lines: number = 100) => request<{ status: string; logs: string[] }>(`/botshepherd/logs?lines=${lines}`),
    // 连接管理
    connections: () => request<BSConnectionsResponse>('/botshepherd/connections'),
    updateConnection: (id: string, data: Partial<BSConnection>) =>
        request<{ success: boolean }>(`/botshepherd/connections/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
    copyConnection: (id: string, newId: string, newName?: string) =>
        request<{ success: boolean; message?: string }>(`/botshepherd/connections/${id}/copy`, {
            method: 'POST', body: JSON.stringify({ new_id: newId, new_name: newName ?? '' }),
        }),
    deleteConnection: (id: string) =>
        request<{ success: boolean }>(`/botshepherd/connections/${id}`, { method: 'DELETE' }),
    // 账号管理
    accounts: () => request<BSAccountsResponse>('/botshepherd/accounts'),
    updateAccount: (id: string, data: Partial<BSAccount>) =>
        request<{ success: boolean }>(`/botshepherd/accounts/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
    deleteAccount: (id: string) =>
        request<{ success: boolean }>(`/botshepherd/accounts/${id}`, { method: 'DELETE' }),
    accountOnline: (id: string) =>
        request<{ online: boolean }>(`/botshepherd/accounts/${id}/online-status`),
    // Bot 框架端点探测
    botsHeartbeat: () =>
        request<{ status: string; bots: Record<string, unknown> }>('/botshepherd/bots/heartbeat'),
    probeTarget: (url: string, token?: string) =>
        request<{ online: boolean; latency_ms: number | null; note?: string; status_code?: number }>(
            '/botshepherd/probe-target',
            { method: 'POST', body: JSON.stringify({ url, token: token ?? '' }) },
        ),
    // Bot 雷达端点库（持久化）
    radarEndpoints: () =>
        request<{ status: string; endpoints: RadarEndpoint[] }>('/botshepherd/radar/endpoints'),
    saveRadarEndpoints: (endpoints: RadarEndpoint[]) =>
        request<{ status: string; count: number }>(
            '/botshepherd/radar/endpoints',
            { method: 'POST', body: JSON.stringify({ endpoints }) },
        ),
    injectByAlias: (params: { alias: string; target: 'bs' | 'nc'; conn_id?: string; container_name?: string; uin?: string }) =>
        request<{ success: boolean; message?: string; error?: string }>(
            '/botshepherd/radar/inject-by-alias',
            { method: 'POST', body: JSON.stringify(params) },
        ),
    // 连接健康监控（生命周期跟随 BS，无需手动调用）
};

// ============ 网络配置注入 ============

/** 单个网络端点的完整字段（所有字段均可选，后端按需合并） */
export interface NetworkEndpointConfig {
    name?: string;
    enable?: boolean;
    // HTTP 服务器 / SSE 服务器
    host?: string;
    port?: number;
    // HTTP 客户端 / WS 客户端
    url?: string;
    token?: string;
    // 通用
    reportSelfMessage?: boolean;
    messagePostFormat?: 'string' | 'array';
    debug?: boolean;
    heartInterval?: number;
    reconnectInterval?: number;
    // HTTP 服务器额外字段
    enableCors?: boolean;
    enableWebsocket?: boolean;
}

export interface InjectNetworkConfigRequest {
    uin?: string;
    node_id?: string;
    network: {
        httpServers?:       NetworkEndpointConfig[];
        httpClients?:       NetworkEndpointConfig[];
        httpSseServers?:    NetworkEndpointConfig[];
        websocketServers?:  NetworkEndpointConfig[];
        websocketClients?:  NetworkEndpointConfig[];
    };
}

export const instanceNetworkApi = {
    injectNetworkConfig: (containerName: string, req: InjectNetworkConfigRequest) =>
        request<{ status: string; message: string; keys_updated: string[] }>(
            `/containers/${containerName}/inject-network-config`,
            { method: 'POST', body: JSON.stringify(req) },
        ),
};

// ============ Bot 在线状态 API ============

export interface BotStatusItem {
    name: string;
    uin: string;
    nickname: string;
    connected: boolean;
    last_seen: number;
}

export const botApi = {
    /** 列出所有已知 Bot（含已断线历史），connected=true 表示当前在线 */
    list: () => request<BotStatusItem[]>('/bots'),
};
