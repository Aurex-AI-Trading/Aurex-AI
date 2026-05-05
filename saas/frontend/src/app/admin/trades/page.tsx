"use client";

import { useEffect, useState } from "react";
import { adminApi } from "@/lib/api";
import { formatCurrency, formatDateTime, directionBadge, statusBadge, pnlColor } from "@/lib/utils";
import type { TradeList } from "@/types";

export default function AdminTradesPage() {
  const [data, setData] = useState<TradeList | null>(null);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);

  useEffect(() => {
    setLoading(true);
    adminApi.allTrades({ page, per_page: 50 })
      .then((r) => { setData(r.data); setLoading(false); })
      .catch(() => setLoading(false));
  }, [page]);

  const totalPages = data ? Math.ceil(data.total / data.per_page) : 1;

  return (
    <div className="p-6 max-w-6xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-navy-900">All Trades</h1>
          <p className="text-sm text-slate-500">
            {data ? `${data.total} trades across all users` : "Loading…"}
          </p>
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-60">
          <div className="w-8 h-8 border-2 border-navy-900/20 border-t-navy-900 rounded-full animate-spin" />
        </div>
      ) : (
        <>
          <div className="table-wrapper mb-4">
            <table className="table">
              <thead>
                <tr>
                  <th>User</th>
                  <th>Symbol</th>
                  <th>Direction</th>
                  <th>Lots</th>
                  <th>Entry</th>
                  <th>Score</th>
                  <th>R:R</th>
                  <th>P&L</th>
                  <th>Status</th>
                  <th>Opened</th>
                </tr>
              </thead>
              <tbody>
                {data?.trades.length === 0 ? (
                  <tr>
                    <td colSpan={10} className="text-center text-slate-400 py-12">
                      No trades found
                    </td>
                  </tr>
                ) : data?.trades.map((t) => (
                  <tr key={t.id}>
                    <td className="font-mono text-xs text-slate-500">{t.user_id.slice(0, 8)}…</td>
                    <td className="font-semibold text-navy-900">{t.symbol}</td>
                    <td>
                      <span className={`badge ${directionBadge(t.direction)}`}>
                        {t.direction}
                      </span>
                    </td>
                    <td>{t.lot_size.toFixed(2)}</td>
                    <td className="font-mono text-xs">{t.entry_price.toFixed(5)}</td>
                    <td>{t.score?.toFixed(0) ?? "—"}</td>
                    <td>{t.rr_ratio?.toFixed(2) ?? "—"}</td>
                    <td className={`font-semibold ${pnlColor(t.pnl)}`}>
                      {t.pnl != null ? formatCurrency(t.pnl) : "—"}
                    </td>
                    <td>
                      <span className={`badge ${statusBadge(t.status)}`}>{t.status}</span>
                    </td>
                    <td className="text-xs text-slate-400">{formatDateTime(t.opened_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {totalPages > 1 && (
            <div className="flex items-center justify-between">
              <p className="text-xs text-slate-400">
                Page {page} of {totalPages}
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
