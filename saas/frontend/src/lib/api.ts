import axios, { AxiosError, InternalAxiosRequestConfig } from "axios";
import Cookies from "js-cookie";

const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export const api = axios.create({
  baseURL: BASE_URL,
  headers: { "Content-Type": "application/json" },
  timeout: 15_000,
});

// ── Request interceptor: attach access token ──────────────────────────────────
api.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = Cookies.get("access_token");
  if (token && config.headers) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// ── Response interceptor: token refresh on 401 ───────────────────────────────
let _isRefreshing = false;
let _refreshQueue: Array<(token: string) => void> = [];

api.interceptors.response.use(
  (res) => res,
  async (error: AxiosError) => {
    const original = error.config as InternalAxiosRequestConfig & { _retry?: boolean };

    if (error.response?.status === 401 && !original._retry) {
      original._retry = true;
      const refreshToken = Cookies.get("refresh_token");
      if (!refreshToken) {
        clearAuth();
        window.location.href = "/login";
        return Promise.reject(error);
      }

      if (_isRefreshing) {
        return new Promise((resolve) => {
          _refreshQueue.push((newToken: string) => {
            original.headers.Authorization = `Bearer ${newToken}`;
            resolve(api(original));
          });
        });
      }

      _isRefreshing = true;
      try {
        const { data } = await axios.post(`${BASE_URL}/auth/refresh`, {
          refresh_token: refreshToken,
        });
        setAuth(data.access_token, data.refresh_token);
        _refreshQueue.forEach((cb) => cb(data.access_token));
        _refreshQueue = [];
        original.headers.Authorization = `Bearer ${data.access_token}`;
        return api(original);
      } catch {
        clearAuth();
        window.location.href = "/login";
        return Promise.reject(error);
      } finally {
        _isRefreshing = false;
      }
    }

    return Promise.reject(error);
  }
);

// ── Auth helpers ──────────────────────────────────────────────────────────────

export function setAuth(accessToken: string, refreshToken: string) {
  Cookies.set("access_token", accessToken, { expires: 1, sameSite: "Strict" });
  Cookies.set("refresh_token", refreshToken, { expires: 30, sameSite: "Strict" });
}

export function clearAuth() {
  Cookies.remove("access_token");
  Cookies.remove("refresh_token");
}

export function isAuthenticated(): boolean {
  return !!Cookies.get("access_token");
}

// ── Typed API methods ─────────────────────────────────────────────────────────

export const authApi = {
  login: (email: string, password: string) =>
    api.post("/auth/login", { email, password }),
  register: (email: string, password: string, full_name: string) =>
    api.post("/auth/register", { email, password, full_name }),
  me: () => api.get("/auth/me"),
};

export const mt5Api = {
  connect: (account_number: number, password: string, server: string) =>
    api.post("/mt5/connect", { account_number, password, server }),
  disconnect:         () => api.post("/mt5/disconnect"),
  status:             () => api.get("/mt5/status"),
  account:            () => api.get("/mt5/account"),
  positions:          () => api.get("/mt5/positions"),
  closePosition:      (ticket: number) => api.post(`/mt5/positions/${ticket}/close`),
  breakevenPosition:  (ticket: number) => api.post(`/mt5/positions/${ticket}/breakeven`),
};

export const tradesApi = {
  list: (params?: Record<string, unknown>) => api.get("/trades", { params }),
  open: () => api.get("/trades/open"),
  get: (id: string) => api.get(`/trades/${id}`),
};

export const analyticsApi = {
  dashboard: (days = 30) => api.get("/analytics/dashboard", { params: { days } }),
  equity:    (days = 30) => api.get("/analytics/equity",    { params: { days } }),
  summary:   (days = 30) => api.get("/analytics/summary",   { params: { days } }),
  weights:   ()           => api.get("/analytics/weights"),
};

export const adminApi = {
  users: (params?: Record<string, unknown>) => api.get("/admin/users", { params }),
  getUser: (id: string) => api.get(`/admin/users/${id}`),
  updateUser: (id: string, data: Record<string, unknown>) =>
    api.put(`/admin/users/${id}`, data),
  deleteUser: (id: string) => api.delete(`/admin/users/${id}`),
  allTrades: (params?: Record<string, unknown>) => api.get("/admin/trades", { params }),
  analytics: () => api.get("/admin/analytics"),
  mt5Sessions: () => api.get("/admin/mt5/sessions"),
};

export const userApi = {
  updateProfile: (data: Record<string, unknown>) => api.put("/users/me", data),
  changePassword: (current_password: string, new_password: string) =>
    api.post("/users/me/change-password", { current_password, new_password }),
};
