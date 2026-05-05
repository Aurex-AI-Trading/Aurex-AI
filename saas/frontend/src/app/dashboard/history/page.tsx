"use client";

import { useEffect, useState } from "react";
import { tradesApi } from "@/lib/api";
import {
  formatCurrency, formatDateTime, directionBadge, statusBadge, pnlColor, cn,
} from "@/lib/utils";
import type { TradeList, TradeStatus } from "@/types";

const PERIOD_OPTS = [
  { label: "7d",   days: 7   },
  { label: "30d",  days: 30  },
  { label: "90d",  days: 90  },
  { label: "1yr",  days: 365 },
  { label: "All",  days: 0   },
];

function SummaryBar({ trades }: { trades: TradeList }) {
  const closed = trades.trades.filter((t) => t.status === "closed");
  const wins   = closed.filter((t) => (t.pnl ?? 0) > 0);
  const losses = closed.filter((t) => (t.pnl ?? 0) <= 0);
  const netPnl = closed.reduce((s, t) => s + (t.pnl ?? 0), 0);
  const wr     = closed.length ? (wins.length / closed.length * 100) : 0;
  const grossP = wins.reduce((s, t) => s + (t.pnl ?? 0), 0);
  const grossL = Math.abs(losses.reduce((s, t) => s + (t.pnl ?? 0), 0));
  const pf     = grossL > 0 ? grossP / grossL : null;

  const items = [
    { label: "Closed Trades", value: String(closed.length) },
    { label: "Win Rate",      value: wr.toFixed(1) + "%", positive: wr >= 50 },
    { label: "Net P&L",       value: formatCurrency(netPnl), positive: netPnl >= 0 },
    { label: "Profit Factor", value: pf ? pf.toFixed(2) : "—", positive: pf ? pf >= 1 : undefined },
    { label: "Wins",          value: String(wins.length),   positive: true  },
    { label: "Losses",        value: String(losses.length), positive: losses.length === 0 },
  ];

  return (
    <div className="grid grid-cols-3 sm:grid-cols-6 gap-3 mb-6">
      {items.map((item) => (
        <div key={item.label} className="card p-4">
          <p className="text-xs text-slate-400 mb-1">{item.label}</p>
          <p className={cn(
            "text-base font-bold",
            item.positive === true  ? "text-success-600" :
            item.positive === false ? "text-danger-600"  : "text-navy-900"
          )}>
            {item.value}
          </p>
        </div>
      ))}
    </div>
  );
}

export default function HistoryPage() {
  const [data, setData]         = useState<TradeList | null>(null);
  const [loading, setLoading]   = useState(true);
  const [days, setDays]         = useState(30);
  const [page, setPage]         = useState(1);

  useEffect(() => {
    setLoading(true);
    tradesApi.list({
      status:   "closed",
      page,
      per_page: 50,
      days:     days || undefined,
    })
      .then((r) => { setData(r.data); setLoading(false); })
      .catch(() => setLoading(false));
  }, [days, page]);

  const totalPages = data ? Math.ceil(data.total / data.per_page) : 1;

  return (
    <div className="p-6 max-w-6xl">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-navy-900">Trade History</h1>
          <p className="text-sm text-slate-500">
            {data ? `${data.total} closed trades` : "Loading…"}
          </p>
        </div>

        {/* Period selector */}
        <div className="flex gap-1.5">
          {PERIOD_OPTS.map((opt) => (
            <button
              key={opt.days}
              onClick={() => { setDays(opt.days); setPage(1); }}
              className={cn(
                "btn btn-sm",
                days === opt.days ? "btn-primary" : "btn-outline"
              )}
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
      ) : (
        <>
          {data && <SummaryBar trades={data} />}

          <div className="table-wrapper mb-4">
            <table className="table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Direction</th>
                  <th>Lots</th>
                  <th>Entry</th>
                  <th>Close</th>
                  <th>SL</th>
                  <th>TP</th>
                  <th>Score</th>
                  <th>R:R</th>
                  <th>P&L</th>
                  <th>Opened</th>
                  <th>Closed</th>
                </tr>
              </thead>
              <tbody>
                {data?.trades.length === 0 ? (
                  <tr>
                    <td colSpan={12} className="text-center text-slate-400 py-12">
                      No closed trades in this period
                    </td>
                  </tr>
                ) : data?.trades.map((t) => (
                  <tr key={t.id}>
                    <td className="font-semibold text-navy-900">{t.symbol}</td>
                    <td>
                      <span className={`badge ${directionBadge(t.direction)}`}>
                        {t.direction}
                      </span>
                    </td>
                    <td>{t.lot_size.toFixed(2)}</td>
                    <td className="font-mono text-xs">{t.entry_price.toFixed(5)}</td>
                    <td className="font-mono text-xs">
                      {t.close_price ? t.close_price.toFixed(5) : "—"}
                    </td>
                    <td className="font-mono text-xs text-danger-600">
                      {t.stop_loss?.toFixed(5) ?? "—"}
                    </td>
                    <td className="font-mono text-xs text-success-600">
                      {t.take_profit?.toFixed(5) ?? "—"}
                    </td>
                    <td>{t.score?.toFixed(0) ?? "—"}</td>
                    <td>{t.rr_ratio?.toFixed(2) ?? "—"}</td>
                    <td className={`font-bold ${pnlColor(t.pnl)}`}>
                      {t.pnl != null ? formatCurrency(t.pnl) : "—"}
                    </td>
                    <td className="text-xs text-slate-400">{formatDateTime(t.opened_at)}</td>
                    <td className="text-xs text-slate-400">
                      {t.closed_at ? formatDateTime(t.closed_at) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {totalPages > 1 && (
            <div className="flex items-center justify-between">
              <p className="text-xs text-slate-400">
                Page {page} of {totalPages} · {data?.total} total
              </p>
              <div className="flex gap-2">
                <button
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page === 1}
                  className="btn-outline btn-sm"
                >
                  Previous
                </button>
                <button
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={page === totalPages}
                  className="btn-outline btn-sm"
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
