"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard, TrendingUp, History, BarChart2,
  Settings, LogOut, Activity,
} from "lucide-react";
import { useAuthStore } from "@/store/auth";
import { useRouter } from "next/navigation";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/dashboard",           label: "Overview",     icon: LayoutDashboard },
  { href: "/dashboard/trades",    label: "Live Trades",  icon: Activity },
  { href: "/dashboard/history",   label: "History",      icon: History },
  { href: "/dashboard/analytics", label: "Analytics",    icon: BarChart2 },
  { href: "/dashboard/settings",  label: "Settings",     icon: Settings },
];

export function DashboardSidebar() {
  const pathname = usePathname();
  const { user, logout } = useAuthStore();
  const router = useRouter();

  function handleLogout() {
    logout();
    router.push("/login");
  }

  return (
    <aside className="w-60 flex-shrink-0 bg-white border-r border-surface-200 flex flex-col h-full">
      {/* Logo */}
      <div className="px-5 py-4 border-b border-surface-200">
        <Link href="/dashboard">
          <img src="/logo.svg" alt="Aurex AI" className="h-8 w-auto" />
        </Link>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-3 py-4 space-y-0.5 overflow-y-auto">
        {NAV.map((item) => {
          const active = pathname === item.href ||
            (item.href !== "/dashboard" && pathname.startsWith(item.href));
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors duration-100",
                active
                  ? "bg-navy-900 text-white"
                  : "text-slate-600 hover:bg-surface-100 hover:text-navy-900"
              )}
            >
              <item.icon size={17} className="flex-shrink-0" />
              {item.label}
            </Link>
          );
        })}

        {user?.role === "admin" && (
          <>
            <div className="px-3 pt-4 pb-1">
              <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider">
                Admin
              </p>
            </div>
            <Link
              href="/admin"
              className={cn(
                "flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors duration-100",
                pathname.startsWith("/admin")
                  ? "bg-navy-900 text-white"
                  : "text-slate-600 hover:bg-surface-100 hover:text-navy-900"
              )}
            >
              <TrendingUp size={17} />
              Admin Panel
            </Link>
          </>
        )}
      </nav>

      {/* User */}
      <div className="px-3 py-4 border-t border-surface-200">
        <div className="flex items-center gap-3 px-3 py-2 rounded-lg">
          <div className="w-8 h-8 rounded-full bg-navy-900 text-white flex items-center justify-center text-xs font-bold flex-shrink-0">
            {user?.full_name.charAt(0).toUpperCase()}
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-slate-900 truncate">{user?.full_name}</p>
            <p className="text-xs text-slate-400 truncate">{user?.email}</p>
          </div>
        </div>
        <button
          onClick={handleLogout}
          className="mt-1 w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium text-slate-500 hover:bg-danger-50 hover:text-danger-600 transition-colors"
        >
          <LogOut size={16} />
          Sign out
        </button>
      </div>
    </aside>
  );
}
