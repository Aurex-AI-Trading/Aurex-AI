"use client";

import { useEffect, useState } from "react";
import {
  LineChart, Line, BarChart, Bar, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
} from "recharts";
import { analyticsApi } from "@/lib/api";
import { formatCurrency, formatPercent } from "@/lib/utils";
import type { AnalyticsDashboard, AIWeightsResponse } from "@/types";

const NAVY = "#0A1628";
const ORANGE = "#F97316";
const GREEN = "#22C55E";
const RED = "#EF4444";
const GREY = "#E2E8F0";

const DAYS_OPTIONS = [
  { label: "7 days",  value: 7  },
  { label: "30 days", value: 30 },
  { label: "90 days", value: 90 },
  { label: "1 year",  value: 365 },
];

function SummaryGrid({ data }: { data: AnalyticsDashboard }) {
  const { summary } = data;
  const items = [
    { label: "Total Trades",   value: String(summary.total_trades)              },
    { label: "Win Rate",       value: `${summary.win_rate}%`                   },
    { label: "Profit Factor",  value: summary.profit_factor?.toFixed(2) ?? "—" },
    { label: "Net P&L",        value: formatCurrency(summary.net_pnl)          },
    { label: "Gross Profit",   value: formatCurrency(summary.gross_profit)     },
    { label: "Gross Loss",     value: formatCurrency(summary.gross_loss)       },
    { label: "Max Drawdown",   value: formatCurrency(summary.max_drawdown)     },
    { label: "Avg R:R",        value: summary.avg_rr?.toFixed(2) ?? "—"        },
    { label: "Best Trade",     value: summary.best_trade ? formatCurrency(summary.best_trade) : "—" },
    { label: "Worst Trade",    value: summary.worst_trade ? formatCurrency(summary.worst_trade) : "—" },
    { label: "Wins",           value: String(summary.wins)                     },
    { label: "Losses",         value: String(summary.losses)                   },
  ];

  return (
    <div className="card p-5 mb-6">
      <h2 className="text-sm font-semibold text-navy-900 mb-4">Performance Summary</h2>
      <div className="grid grid-cols-3 sm:grid-cols-4 lg:grid-cols-6 gap-4">
        {items.map((item) => (
          <div key={item.label}>
            <p className="text-xs text-slate-400 mb-0.5">{item.label}</p>
            <p className="text-sm font-bold text-navy-900">{item.value}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function EquityCurve({ data }: { data: AnalyticsDashboard }) {
  const chartData = data.equity_curve.map((p) => ({
    date: p.date,
    Balance: p.balance,
    Equity: p.equity,
  }));

  return (
    <div className="card p-5 mb-6">
      <h2 className="text-sm font-semibold text-navy-900 mb-4">Equity Curve</h2>
      {chartData.length === 0 ? (
        <p className="text-sm text-slate-400 py-10 text-center">No equity data yet</p>
      ) : (
        <ResponsiveContainer width="100%" height={260}>
          <LineChart data={chartData} margin={{ left: 0, right: 8, top: 4, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={GREY} />
            <XAxis dataKey="date" tick={{ fontSize: 11, fill: "#94A3B8" }} />
            <YAxis tick={{ fontSize: 11, fill: "#94A3B8" }} tickFormatter={(v) => `$${v.toLocaleString()}`} />
            <Tooltip formatter={(v: number) => formatCurrency(v)} />
            <Legend />
            <Line type="monotone" dataKey="Balance" stroke={NAVY} strokeWidth={2} dot={false} />
            <Line type="monotone" dataKey="Equity"  stroke={ORANGE} strokeWidth={1.5} dot={false} strokeDasharray="4 2" />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}

function MonthlyPnlChart({ data }: { data: AnalyticsDashboard }) {
  const chartData = data.monthly_pnl.map((m) => ({
    month: m.month,
    PnL: m.pnl,
    fill: m.pnl >= 0 ? GREEN : RED,
  }));

  return (
    <div className="card p-5 mb-6">
      <h2 className="text-sm font-semibold text-navy-900 mb-4">Monthly P&L</h2>
      {chartData.length === 0 ? (
        <p className="text-sm text-slate-400 py-10 text-center">No monthly data yet</p>
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={chartData} margin={{ left: 0, right: 8, top: 4, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={GREY} />
            <XAxis dataKey="month" tick={{ fontSize: 11, fill: "#94A3B8" }} />
            <YAxis tick={{ fontSize: 11, fill: "#94A3B8" }} tickFormatter={(v) => `$${v}`} />
            <Tooltip formatter={(v: number) => formatCurrency(v)} />
            <Bar dataKey="PnL" radius={[4, 4, 0, 0]}>
              {chartData.map((entry, index) => (
                <Cell key={`cell-${index}`} fill={entry.fill} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}

function SymbolBreakdown({ data }: { data: AnalyticsDashboard }) {
  const winRateData = data.symbol_breakdown.map((s) => ({ name: s.symbol, value: s.wins }));
  const COLORS = [NAVY, ORANGE, GREEN, "#6366F1", "#EC4899", "#14B8A6", "#F59E0B"];

  return (
    <div className="grid md:grid-cols-2 gap-4 mb-6">
      {/* Table */}
      <div className="card">
        <div className="px-5 py-3.5 border-b border-surface-100">
          <h2 className="text-sm font-semibold text-navy-900">Symbol Breakdown</h2>
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Trades</th>
              <th>Win %</th>
              <th>Net P&L</th>
            </tr>
          </thead>
          <tbody>
            {data.symbol_breakdown.length === 0 ? (
              <tr><td colSpan={4} className="text-center text-slate-400 py-8">No data</td></tr>
            ) : data.symbol_breakdown.map((s) => (
              <tr key={s.symbol}>
                <td className="font-semibold text-navy-900">{s.symbol}</td>
                <td>{s.trades}</td>
                <td>{s.win_rate}%</td>
                <td className={`font-semibold ${s.net_pnl >= 0 ? "text-success-600" : "text-danger-600"}`}>
                  {formatCurrency(s.net_pnl)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pie */}
      <div className="card p-5 flex flex-col">
        <h2 className="text-sm font-semibold text-navy-900 mb-4">Win Distribution</h2>
        {winRateData.length === 0 ? (
          <p className="text-sm text-slate-400 flex-1 flex items-center justify-center">No data</p>
        ) : (
          <ResponsiveContainer width="100%" height={200}>
            <PieChart>
              <Pie
                data={winRateData}
                cx="50%" cy="50%"
                innerRadius={55} outerRadius={85}
                paddingAngle={3}
                dataKey="value"
              >
                {winRateData.map((_, index) => (
                  <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                ))}
              </Pie>
              <Tooltip />
              <Legend iconType="circle" iconSize={8} />
            </PieChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}

function AIWeightsPanel() {
  const [weights, setWeights] = useState<AIWeightsResponse | null>(null);

  useEffect(() => {
    analyticsApi.weights().then((r) => setWeights(r.data)).catch(() => {});
  }, []);

  if (!weights) return null;

  const LABELS: Record<string, string> = {
    trend: "Trend", liquidity: "Liquidity", fvg: "FVG",
    fibonacci: "Fibonacci", ema: "EMA", confirmation: "Confirmation",
    order_block: "Order Block",
  };

  const core = ["trend", "liquidity", "fvg", "fibonacci", "ema", "confirmation"];
  const maxW = 35;

  return (
    <div className="card p-5 mb-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-semibold text-navy-900">AI Confluence Weights</h2>
        {weights.last_updated && (
          <span className="text-xs text-slate-400">
            Updated {new Date(weights.last_updated).toLocaleDateString()}
          </span>
        )}
      </div>
      <div className="grid sm:grid-cols-2 gap-4">
        {core.concat(["order_block"]).map((key) => {
          const val = (weights.weights as unknown as Record<string, number>)[key] ?? 0;
          const pct = (val / maxW) * 100;
          const isBonus = key === "order_block";
          return (
            <div key={key}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-medium text-slate-600">{LABELS[key]}</span>
                <div className="flex items-center gap-2">
                  <span className="text-xs font-bold text-navy-900">{val}</span>
                  {isBonus && (
                    <span className="text-[10px] text-slate-400 font-medium">bonus</span>
                  )}
                </div>
              </div>
              <div className="h-1.5 bg-surface-100 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all duration-500 ${
                    isBonus ? "bg-accent-400" : "bg-navy-600"
                  }`}
                  style={{ width: `${pct}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>
      <p className="text-xs text-slate-400 mt-4">
        Weights are automatically adjusted by the AI learning engine based on component score
        performance. Core weights always sum to 100. Order Block is an additive bonus.
      </p>
    </div>
  );
}

export default function AnalyticsPage() {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<AnalyticsDashboard | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    analyticsApi.dashboard(days).then((res) => {
      setData(res.data);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, [days]);

  return (
    <div className="p-6 max-w-6xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-navy-900">Analytics</h1>
          <p className="text-sm text-slate-500">Your trading performance breakdown</p>
        </div>
        <div className="flex gap-2">
          {DAYS_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => setDays(opt.value)}
              className={`btn btn-sm ${days === opt.value ? "btn-primary" : "btn-outline"}`}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-60">
          <div className="w-8 h-8 border-2 border-navy-900/20 border-t-navy-900 rounded-full animate-spin" />
        </div>
      ) : data ? (
        <>
          <SummaryGrid data={data} />
          <EquityCurve data={data} />
          <MonthlyPnlChart data={data} />
          <SymbolBreakdown data={data} />
          <AIWeightsPanel />
        </>
      ) : (
        <p className="text-slate-400">No analytics data available.</p>
      )}
    </div>
  );
}
