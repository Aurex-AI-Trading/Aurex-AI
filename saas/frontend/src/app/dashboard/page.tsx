"use client";

import { useEffect, useState } from "react";
import {
  TrendingUp, TrendingDown, Activity, DollarSign, RefreshCw, Wifi, WifiOff,
} from "lucide-react";
import { mt5Api, analyticsApi } from "@/lib/api";
import { formatCurrency, formatDateTime, pnlColor, directionBadge, cn } from "@/lib/utils";
import type { MT5Status, PerformanceSummary, MT5Position } from "@/types";
import { useAuthStore } from "@/store/auth";
import { useWebSocket } from "@/hooks/useWebSocket";

function StatCard({
  label, value, sub, positive, icon: Icon,
}: {
  label: string; value: string; sub?: string;
  positive?: boolean; icon: React.ElementType;
}) {
  return (
    <div className="stat-card">
      <div className="flex items-center justify-between mb-2">
        <p className="stat-label">{label}</p>
        <div className="w-8 h-8 rounded-lg bg-surface-100 flex items-center justify-center">
          <Icon size={15} className="text-slate-500" />
        </div>
      </div>
      <p className={cn(
        "stat-value",
        positive === true  ? "text-success-600" :
        positive === false ? "text-danger-600"  : ""
      )}>
        {value}
      </p>
      {sub && <p className="text-xs text-slate-400 mt-0.5">{sub}</p>}
    </div>
  );
}

function MT5StatusCard({
  status, wsBalance, wsEquity, wsConnected,
}: {
  status: MT5Status | null;
  wsBalance: number | null;
  wsEquity: number | null;
  wsConnected: boolean;
}) {
  const connected = wsConnected ? true : status?.connected;
  const balance   = wsBalance  ?? status?.balance;
  const equity    = wsEquity   ?? status?.equity;

  return (
    <div className="card p-5">
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs font-semibold text-slate-500 uppercase tracking-wider">
          MT5 Connection
        </span>
        <div className="flex items-center gap-2">
          {wsConnected ? (
            <span className="flex items-center gap-1 text-xs text-success-600 font-medium">
              <Wifi size={12} /> Live
            </span>
          ) : (
            <span className="flex items-center gap-1 text-xs text-slate-400 font-medium">
              <WifiOff size={12} /> Polling
            </span>
          )}
          <span className={cn(
            "badge",
            connected
              ? "bg-success-50 text-success-700 border border-success-200"
              : "bg-danger-50 text-danger-700 border border-danger-200"
          )}>
            <span className={cn(
              "w-1.5 h-1.5 rounded-full",
              connected ? "bg-success-500 animate-pulse-dot" : "bg-danger-500"
            )} />
            {connected ? "Connected" : "Disconnected"}
          </span>
        </div>
      </div>

      {connected ? (
        <div className="grid grid-cols-2 gap-4">
          <div>
            <p className="text-xs text-slate-400 mb-0.5">Balance</p>
            <p className="text-lg font-bold text-navy-900">
              {balance != null ? formatCurrency(balance) : "—"}
            </p>
          </div>
          <div>
            <p className="text-xs text-slate-400 mb-0.5">Equity</p>
            <p className="text-lg font-bold text-navy-900">
              {equity != null ? formatCurrency(equity) : "—"}
            </p>
          </div>
          <div>
            <p className="text-xs text-slate-400 mb-0.5">Account</p>
            <p className="text-sm font-semibold text-slate-700">
              {status?.account_number ?? "—"}
            </p>
          </div>
          <div>
            <p className="text-xs text-slate-400 mb-0.5">Server</p>
            <p className="text-sm font-semibold text-slate-700 truncate">
              {status?.server ?? "—"}
            </p>
          </div>
        </div>
      ) : (
        <div>
          <p className="text-sm text-slate-500 mb-3">
            Connect your MT5 account to start trading.
          </p>
          <a href="/dashboard/settings" className="btn-primary btn-sm">
            Connect MT5
          </a>
        </div>
      )}
    </div>
  );
}

export default function DashboardOverview() {
  const { user }       = useAuthStore();
  const { status: wsStatus, wsLive } = useWebSocket();

  const [mt5Status, setMt5Status]   = useState<MT5Status | null>(null);
  const [summary, setSummary]       = useState<PerformanceSummary | null>(null);
  const [positions, setPositions]   = useState<MT5Position[]>([]);
  const [loading, setLoading]       = useState(true);

  async function fetchAll() {
    setLoading(true);
    try {
      const [statusRes, summaryRes, posRes] = await Promise.allSettled([
        mt5Api.status(),
        analyticsApi.summary(30),
        mt5Api.positions(),
      ]);
      if (statusRes.status  === "fulfilled") setMt5Status(statusRes.value.data);
      if (summaryRes.status === "fulfilled") setSummary(summaryRes.value.data);
      if (posRes.status     === "fulfilled") setPositions(posRes.value.data);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { fetchAll(); }, []);

  const openCount = wsStatus?.open_trades ?? positions.length;

  const hour = new Date().getHours();
  const greeting = hour < 12 ? "morning" : hour < 17 ? "afternoon" : "evening";

  return (
    <div className="p-6 max-w-6xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-navy-900">
            Good {greeting}, {user?.full_name.split(" ")[0]}
          </h1>
          <p className="text-sm text-slate-500">Here&apos;s your trading overview</p>
        </div>
        <button onClick={fetchAll} className="btn-outline btn-sm gap-1.5">
          <RefreshCw size={13} />
          Refresh
        </button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-40">
          <div className="w-8 h-8 border-2 border-navy-900/20 border-t-navy-900 rounded-full animate-spin" />
        </div>
      ) : (
        <>
          <div className="grid md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
            <StatCard
              label="Net P&L (30d)"
              value={summary ? formatCurrency(summary.net_pnl) : "—"}
              positive={summary ? summary.net_pnl >= 0 : undefined}
              icon={DollarSign}
            />
            <StatCard
              label="Win Rate"
              value={summary ? `${summary.win_rate}%` : "—"}
              sub={summary ? `${summary.wins}W / ${summary.losses}L` : undefined}
              positive={summary ? summary.win_rate >= 50 : undefined}
              icon={TrendingUp}
            />
            <StatCard
              label="Profit Factor"
              value={summary?.profit_factor ? summary.profit_factor.toFixed(2) : "—"}
              positive={summary?.profit_factor ? summary.profit_factor >= 1 : undefined}
              icon={TrendingDown}
            />
            <StatCard
              label="Open Positions"
              value={String(openCount)}
              sub={`Max drawdown: ${summary ? formatCurrency(summary.max_drawdown) : "—"}`}
              icon={Activity}
            />
          </div>

          <div className="grid lg:grid-cols-3 gap-4">
            <MT5StatusCard
              status={mt5Status}
              wsBalance={wsStatus?.balance ?? null}
              wsEquity={wsStatus?.equity ?? null}
              wsConnected={wsLive && (wsStatus?.connected ?? false)}
            />

            {/* Open positions */}
            <div className="lg:col-span-2 card">
              <div className="px-5 py-4 border-b border-surface-100 flex items-center justify-between">
                <h2 className="text-sm font-semibold text-navy-900">Open Positions</h2>
                <a
                  href="/dashboard/trades"
                  className="text-xs font-medium text-accent-500 hover:text-accent-600"
                >
                  Manage
                </a>
              </div>
              {positions.length === 0 ? (
                <div className="px-5 py-10 text-center text-sm text-slate-400">
                  No open positions
                </div>
              ) : (
                <table className="table">
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      <th>Dir</th>
                      <th>Lots</th>
                      <th>Entry</th>
                      <th>P&L</th>
                      <th>Opened</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positions.slice(0, 5).map((pos) => (
                      <tr key={pos.ticket}>
                        <td className="font-semibold text-navy-900">{pos.symbol}</td>
                        <td>
                          <span className={`badge ${directionBadge(pos.type as "BUY" | "SELL")}`}>
                            {pos.type}
                          </span>
                        </td>
                        <td>{pos.volume?.toFixed(2) ?? "—"}</td>
                        <td className="font-mono text-xs">{pos.price_open?.toFixed(5) ?? "—"}</td>
                        <td className={`font-semibold ${pnlColor(pos.profit)}`}>
                          {formatCurrency(pos.profit)}
                        </td>
                        <td className="text-slate-400 text-xs">
                          {pos.time
                            ? formatDateTime(new Date(pos.time * 1000).toISOString())
                            : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
