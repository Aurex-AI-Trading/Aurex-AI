"use client";

import { useEffect, useState } from "react";
import { Search, Trash2, Edit2, ChevronDown } from "lucide-react";
import { adminApi } from "@/lib/api";
import { formatDateTime } from "@/lib/utils";
import type { UserList, User } from "@/types";

export default function AdminUsersPage() {
  const [data, setData] = useState<UserList | null>(null);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  async function fetchUsers() {
    setLoading(true);
    try {
      const res = await adminApi.users({ page, per_page: 20, search: search || undefined });
      setData(res.data);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { fetchUsers(); }, [page]);

  async function handleDelete(userId: string) {
    if (!confirm("Delete this user? This action cannot be undone.")) return;
    setDeletingId(userId);
    try {
      await adminApi.deleteUser(userId);
      fetchUsers();
    } finally {
      setDeletingId(null);
    }
  }

  async function handleToggleActive(user: User) {
    await adminApi.updateUser(user.id, { is_active: !user.is_active });
    fetchUsers();
  }

  const totalPages = data ? Math.ceil(data.total / data.per_page) : 1;

  return (
    <div className="p-6 max-w-6xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-navy-900">Users</h1>
          <p className="text-sm text-slate-500">{data ? `${data.total} total users` : ""}</p>
        </div>
      </div>

      {/* Search */}
      <div className="flex gap-3 mb-4">
        <div className="relative flex-1 max-w-sm">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
          <input
            type="text"
            placeholder="Search by name or email…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") { setPage(1); fetchUsers(); } }}
            className="input pl-9"
          />
        </div>
        <button onClick={() => { setPage(1); fetchUsers(); }} className="btn-outline btn-sm">
          Search
        </button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-40">
          <div className="w-8 h-8 border-2 border-navy-900/20 border-t-navy-900 rounded-full animate-spin" />
        </div>
      ) : (
        <>
          <div className="table-wrapper mb-4">
            <table className="table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Email</th>
                  <th>Role</th>
                  <th>Status</th>
                  <th>Joined</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {data?.users.length === 0 ? (
                  <tr><td colSpan={6} className="text-center text-slate-400 py-12">No users found</td></tr>
                ) : data?.users.map((u) => (
                  <tr key={u.id}>
                    <td className="font-semibold text-navy-900">{u.full_name}</td>
                    <td className="text-slate-500">{u.email}</td>
                    <td>
                      <span className={`badge ${u.role === "admin" ? "bg-accent-50 text-accent-700 border border-accent-200" : "bg-navy-50 text-navy-700 border border-navy-200"}`}>
                        {u.role}
                      </span>
                    </td>
                    <td>
                      <span className={`badge ${u.is_active ? "bg-success-50 text-success-700 border border-success-200" : "bg-slate-100 text-slate-500 border border-slate-200"}`}>
                        {u.is_active ? "Active" : "Suspended"}
                      </span>
                    </td>
                    <td className="text-xs text-slate-400">{formatDateTime(u.created_at)}</td>
                    <td>
                      <div className="flex items-center gap-1">
                        <button
                          onClick={() => handleToggleActive(u)}
                          className="btn-ghost btn-sm text-xs"
                          title={u.is_active ? "Suspend" : "Activate"}
                        >
                          {u.is_active ? "Suspend" : "Activate"}
                        </button>
                        <button
                          onClick={() => handleDelete(u.id)}
                          disabled={deletingId === u.id}
                          className="btn-ghost btn-sm text-danger-500 hover:bg-danger-50"
                          title="Delete user"
                        >
                          <Trash2 size={13} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {totalPages > 1 && (
            <div className="flex items-center justify-between">
              <p className="text-xs text-slate-400">Page {page} of {totalPages}</p>
              <div className="flex gap-2">
                <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page === 1} className="btn-outline btn-sm">Previous</button>
                <button onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={page === totalPages} className="btn-outline btn-sm">Next</button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
