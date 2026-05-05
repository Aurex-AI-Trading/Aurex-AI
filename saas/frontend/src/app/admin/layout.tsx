"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuthStore } from "@/store/auth";
import { isAuthenticated } from "@/lib/api";
import { LayoutDashboard, Users, Activity, BarChart2, LogOut } from "lucide-react";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/admin",         label: "Overview",     icon: LayoutDashboard, exact: true },
  { href: "/admin/users",   label: "Users",        icon: Users },
  { href: "/admin/trades",  label: "All Trades",   icon: Activity },
];

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { user, logout } = useAuthStore();
  const pathname = usePathname();

  useEffect(() => {
    if (!isAuthenticated() || (user && user.role !== "admin")) {
      router.push("/dashboard");
    }
  }, [user, router]);

  if (!user || user.role !== "admin") {
    return (
      <div className="min-h-screen flex items-center justify-center bg-surface-50">
        <div className="w-8 h-8 border-2 border-navy-900/20 border-t-navy-900 rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="flex h-screen bg-surface-50 overflow-hidden">
      <aside className="w-60 flex-shrink-0 bg-white border-r border-surface-200 flex flex-col h-full">
        <div className="px-5 py-4 border-b border-surface-200">
          <Link href="/admin">
            <img src="/logo.svg" alt="Aurex AI" className="h-8 w-auto" />
          </Link>
          <p className="text-[10px] font-semibold text-accent-500 uppercase tracking-widest mt-1">
            Admin Panel
          </p>
        </div>
        <nav className="flex-1 px-3 py-4 space-y-0.5">
          {NAV.map((item) => {
            const active = item.exact ? pathname === item.href : pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  "flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors",
                  active ? "bg-navy-900 text-white" : "text-slate-600 hover:bg-surface-100 hover:text-navy-900"
                )}
              >
                <item.icon size={17} className="flex-shrink-0" />
                {item.label}
              </Link>
            );
          })}
          <Link
            href="/dashboard"
            className="flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium text-slate-400 hover:text-navy-900 mt-4"
          >
            ← Back to Dashboard
          </Link>
        </nav>
        <div className="px-3 py-4 border-t border-surface-200">
          <button
            onClick={() => { logout(); router.push("/login"); }}
            className="w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-slate-500 hover:bg-danger-50 hover:text-danger-600 transition-colors"
          >
            <LogOut size={16} /> Sign out
          </button>
        </div>
      </aside>
      <main className="flex-1 overflow-y-auto">{children}</main>
    </div>
  );
}
