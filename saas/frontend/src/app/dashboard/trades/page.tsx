"use client";

import { useEffect, useState, useCallback } from "react";
import { X, Shield, RefreshCw, AlertTriangle, Loader2 } from "lucide-react";
import { mt5Api, api } from "@/lib/api";
import { formatCurrency, formatDateTime, directionBadge, cn } from "@/lib/utils";
import type { MT5Position } from "@/types";

function ConfirmModal({
  message,
  onConfirm,
  onCancel,
  loading,
}: {
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
  loading: boolean;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="card p-6 w-full max-w-sm mx-4 shadow-card-lg">
        <div className="flex items-start gap-3 mb-5">
          <AlertTriangle size={20} className="text-accent-500 mt-0.5 flex-shrink-0" />
          <div>
            <h3 className="text-sm font-semibold text-navy-900 mb-1">Confirm action</h3>
            <p className="text-sm text-slate-500">{message}</p>
          </div>
        </div>
        <div className="flex gap-3 justify-end">
          <button onClick={onCancel} className="btn-outline btn-sm" disabled={loading}>
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={loading}
            className="btn-danger btn-sm gap-1.5"
          >
            {loading && <Loader2 size={13} className="animate-spin" />}
            Confirm
          </button>
        </div>
      </div>
    </div>
  );
}

export default function LiveTradesPage() {
  const [positions, setPositions]   = useState<MT5Position[]>([]);
  const [loading, setLoading]       = useState(true);
  const [actionTicket, setActionTicket] = useState<number | null>(null);
  const [actionType, setActionType] = useState<"close" | "be" | null>(null);
  const [actionLoading, setActionLoading] = useState(false);
  const [toast, setToast]           = useState<{ msg: string; ok: boolean } | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await mt5Api.positions();
      setPositions(res.data);
    } catch {
      setPositions([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  function showToast(msg: string, ok: boolean) {
    setToast({ msg, ok });
    setTimeout(() => setToast(null), 4000);
  }

  async function executeAction() {
    if (!actionTicket || !actionType) return;
    setActionLoading(true);
    try {
      if (actionType === "close") {
        await mt5Api.closePosition(actionTicket);
        showToast(`Position #${actionTicket} closed successfully.`, true);
      } else {
        await mt5Api.breakevenPosition(actionTicket);
        showToast(`Stop-loss moved to breakeven for #${actionTicket}.`, true);
      }
      setActionTicket(null);
      setActionType(null);
      await refresh();
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Action failed.";
      showToast(detail, false);
    } finally {
      setActionLoading(false);
    }
  }

  return (
    <div className="p-6 max-w-6xl">
      {/* Confirm modal */}
      {actionTicket && actionType && (
        <ConfirmModal
          message={
            actionType === "close"
              ? `Market-close position #${actionTicket}? This cannot be undone.`
              : `Move stop-loss to entry (breakeven) for position #${actionTicket}?`
          }
          onConfirm={executeAction}
          onCancel={() => { setActionTicket(null); setActionType(null); }}
          loading={actionLoading}
        />
      )}

      {/* Toast */}
      {toast && (
        <div className={cn(
          "fixed bottom-6 right-6 z-40 flex items-center gap-2.5 px-4 py-3 rounded-xl",
          "shadow-card-lg text-sm font-medium border",
          toast.ok
            ? "bg-success-50 text-success-700 border-success-200"
            : "bg-danger-50 text-danger-700 border-danger-200"
        )}>
          {toast.msg}
        </div>
      )}

      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-navy-900">Live Trades</h1>
          <p className="text-sm text-slate-500">
            {loading ? "Loading…" : `${positions.length} open position${positions.length !== 1 ? "s" : ""}`}
          </p>
        </div>
        <button onClick={refresh} disabled={loading} className="btn-outline btn-sm gap-1.5">
          <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
          Refresh
        </button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-60">
          <div className="w-8 h-8 border-2 border-navy-900/20 border-t-navy-900 rounded-full animate-spin" />
        </div>
      ) : positions.length === 0 ? (
        <div className="card flex flex-col items-center justify-center py-20 text-center">
          <div className="w-12 h-12 rounded-full bg-surface-100 flex items-center justify-center mb-4">
            <Shield size={22} className="text-slate-400" />
          </div>
          <p className="text-sm font-medium text-slate-600 mb-1">No open positions</p>
          <p className="text-xs text-slate-400">
            Aurex AI will open trades when high-confluence setups are detected.
          </p>
        </div>
      ) : (
        <div className="table-wrapper">
          <table className="table">
            <thead>
              <tr>
                <th>Ticket</th>
                <th>Symbol</th>
                <th>Direction</th>
                <th>Lots</th>
                <th>Entry</th>
                <th>Current</th>
                <th>SL</th>
                <th>TP</th>
                <th>Profit</th>
                <th>Opened</th>
                <th className="text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((pos) => (
                <tr key={pos.ticket}>
                  <td className="font-mono text-xs text-slate-500">{pos.ticket}</td>
                  <td className="font-semibold text-navy-900">{pos.symbol}</td>
                  <td>
                    <span className={`badge ${directionBadge(pos.type as "BUY" | "SELL")}`}>
                      {pos.type}
                    </span>
                  </td>
                  <td>{pos.volume?.toFixed(2) ?? "—"}</td>
                  <td className="font-mono text-xs">{pos.price_open?.toFixed(5) ?? "—"}</td>
                  <td className="font-mono text-xs">{pos.price_current?.toFixed(5) ?? "—"}</td>
                  <td className="font-mono text-xs text-danger-600">
                    {pos.sl ? pos.sl.toFixed(5) : "—"}
                  </td>
                  <td className="font-mono text-xs text-success-600">
                    {pos.tp ? pos.tp.toFixed(5) : "—"}
                  </td>
                  <td className={`font-bold ${pos.profit >= 0 ? "text-success-600" : "text-danger-600"}`}>
                    {formatCurrency(pos.profit)}
                  </td>
                  <td className="text-xs text-slate-400">
                    {pos.time ? formatDateTime(new Date(pos.time * 1000).toISOString()) : "—"}
                  </td>
                  <td className="text-right">
                    <div className="flex items-center justify-end gap-1.5">
                      <button
                        onClick={() => { setActionTicket(pos.ticket); setActionType("be"); }}
                        title="Move to breakeven"
                        className="btn-outline btn-sm gap-1 text-xs"
                      >
                        <Shield size={12} />
                        BE
                      </button>
                      <button
                        onClick={() => { setActionTicket(pos.ticket); setActionType("close"); }}
                        title="Close position"
                        className="btn-sm btn bg-danger-50 border border-danger-200 text-danger-700 hover:bg-danger-100 gap-1 text-xs"
                      >
                        <X size={12} />
                        Close
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-xs text-slate-400 mt-4">
        Actions are executed immediately via MT5. Closing a position cannot be reversed.
      </p>
    </div>
  );
}
