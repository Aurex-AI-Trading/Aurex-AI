import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatCurrency(value: number, decimals = 2): string {
  const formatted = Math.abs(value).toFixed(decimals);
  return (value < 0 ? "-$" : "$") + Number(formatted).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function formatPercent(value: number, decimals = 1): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(decimals)}%`;
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", {
    year: "numeric", month: "short", day: "numeric",
  });
}

export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString("en-US", {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export function pnlColor(value: number | null): string {
  if (value === null) return "text-slate-500";
  return value >= 0 ? "text-success-600" : "text-danger-600";
}

export function directionBadge(direction: "BUY" | "SELL"): string {
  return direction === "BUY"
    ? "bg-success-50 text-success-700 border border-success-200"
    : "bg-danger-50 text-danger-700 border border-danger-200";
}

export function statusBadge(status: string): string {
  switch (status) {
    case "open":      return "bg-blue-50 text-blue-700 border border-blue-200";
    case "closed":    return "bg-slate-100 text-slate-700 border border-slate-200";
    case "connected": return "bg-success-50 text-success-700 border border-success-200";
    case "error":     return "bg-danger-50 text-danger-700 border border-danger-200";
    default:          return "bg-slate-50 text-slate-600 border border-slate-200";
  }
}
