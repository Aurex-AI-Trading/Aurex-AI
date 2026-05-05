"use client";

import { useEffect, useState } from "react";
import { Users, Activity, TrendingUp, Wifi } from "lucide-react";
import { adminApi } from "@/lib/api";
import type { PlatformAnalytics } from "@/types";
import { formatDateTime } from "@/lib/utils";

export default function AdminOverview() {
  const [analytics, setAnalytics] = useState<PlatformAnalytics | null>(null);
  const [sessions, setSessions] = useState<Record<string, { connected: boolean; account_number?: number; server?: string; last_heartbeat?: string; balance?: number }>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.allSettled([adminApi.analytics(), adminApi.mt5Sessions()]).then(([analyticsRes, sessionsRes]) => {
      if (analyticsRes.status === "fulfilled") setAnalytics(analyticsRes.value.data);
      if (sessionsRes.status === "fulfilled") setSessions(sessionsRes.value.data);
      setLoading(false);
    });
  }, []);

  const stats = analytics ? [
    { label: "Total Users",   value: String(analytics.total_users),  icon: Users,     sub: `${analytics.active_users} active` },
    { label: "Total Trades",  value: String(analytics.total_trades), icon: Activity,  sub: `${analytics.open_trades} open`    },
    { label: "MT5 Sessions",  value: String(Object.values(sessions).filter(s => s.connected).length), icon: Wifi, sub: "connected" },
  ] : [];

  return (
    <div className="p-6 max-w-6xl">
      <div className="mb-6">
        <h1 className="text-xl font-bold text-navy-900">Admin Overview</h1>
        <p className="text-sm text-slate-500">Platform-wide metrics and MT5 session status</p>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-40">
          <div className="w-8 h-8 border-2 border-navy-900/20 border-t-navy-900 rounded-full animate-spin" />
        </div>
      ) : (
        <>
          <div className="grid sm:grid-cols-3 gap-4 mb-6">
            {stats.map((s) => (
              <div key={s.label} className="stat-card">
                <div className="flex items-center justify-between mb-2">
                  <p className="stat-label">{s.label}</p>
                  <s.icon size={16} className="text-slate-400" />
                </div>
                <p className="stat-value">{s.value}</p>
                <p className="text-xs text-slate-400 mt-0.5">{s.sub}</p>
              </div>
            ))}
          </div>

          {/* MT5 sessions */}
          <div className="card">
            <div className="px-5 py-4 border-b border-surface-100">
              <h2 className="text-sm font-semibold text-navy-900">Active MT5 Sessions</h2>
            </div>
            {Object.keys(sessions).length === 0 ? (
              <p className="px-5 py-10 text-sm text-slate-400 text-center">No active MT5 sessions</p>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>User ID</th>
                    <th>Account</th>
                    <th>Server</th>
                    <th>Balance</th>
                    <th>Status</th>
                    <th>Last Heartbeat</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(sessions).map(([uid, s]) => (
                    <tr key={uid}>
                      <td className="font-mono text-xs text-slate-500">{uid.slice(0, 8)}…</td>
                      <td className="font-semibold">{s.account_number ?? "—"}</td>
                      <td>{s.server ?? "—"}</td>
                      <td>{s.balance != null ? `$${s.balance.toLocaleString()}` : "—"}</td>
                      <td>
                        <span className={`badge ${s.connected ? "bg-success-50 text-success-700 border border-success-200" : "bg-danger-50 text-danger-700 border border-danger-200"}`}>
                          <span className={`w-1.5 h-1.5 rounded-full ${s.connected ? "bg-success-500" : "bg-danger-500"}`} />
                          {s.connected ? "Connected" : "Disconnected"}
                        </span>
                      </td>
                      <td className="text-xs text-slate-400">
                        {s.last_heartbeat ? formatDateTime(s.last_heartbeat) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}
    </div>
  );
}
