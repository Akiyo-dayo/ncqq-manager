export interface OperationLog {
    id: string;
    type: string;
    level: 'info' | 'warning' | 'error';
    time: string;
    timestamp: number;
    operator?: string;
    operator_ip?: string;
    target?: string;
    [key: string]: unknown;
}

export interface OperationLogsQuery {
    limit?: number;
    page?: number;
    operator?: string;
    type?: string;
    level?: 'info' | 'warning' | 'error' | '';
    start_time?: number;
    end_time?: number;
}

export interface OperationLogsResponse {
    status: string;
    logs: OperationLog[];
    pagination: {
        page: number;
        limit: number;
        total: number;
        pages: number;
    };
    filters: {
        operator: string;
        type: string;
        level: string;
        start_time: number | null;
        end_time: number | null;
    };
}

const API_BASE = '/api';

function buildQueryString(query: OperationLogsQuery = {}): string {
    const params = new URLSearchParams();
    if (query.limit) params.set('limit', String(query.limit));
    if (query.page) params.set('page', String(query.page));
    if (query.operator) params.set('operator', query.operator);
    if (query.type) params.set('type', query.type);
    if (query.level) params.set('level', query.level);
    if (typeof query.start_time === 'number') params.set('start_time', String(query.start_time));
    if (typeof query.end_time === 'number') params.set('end_time', String(query.end_time));
    const queryString = params.toString();
    return queryString ? `?${queryString}` : '';
}

async function requestOperationLogs<T>(path: string): Promise<T> {
    const response = await fetch(`${API_BASE}${path}`, {
        credentials: 'include',
        headers: {
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
        },
    });

    if (response.status === 401) {
        window.dispatchEvent(new CustomEvent('auth:unauthorized'));
        throw new Error('Unauthorized');
    }

    if (!response.ok) {
        const error = await response.json().catch(() => ({ message: 'Request failed' }));
        throw new Error(error.message || `HTTP ${response.status}`);
    }

    return response.json() as Promise<T>;
}

export const operationLogsApi = {
    list: (query: OperationLogsQuery = {}) =>
        requestOperationLogs<OperationLogsResponse>(`/operation_logs${buildQueryString(query)}`),
};

export function buildOperationLogsDownloadUrl(query: OperationLogsQuery = {}): string {
    return `${API_BASE}/operation_logs/download${buildQueryString(query)}`;
}

