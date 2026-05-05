"use client";

import { useState, useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { CheckCircle, AlertCircle, Plug, PlugZap, Loader2 } from "lucide-react";
import { mt5Api, userApi } from "@/lib/api";
import { useAuthStore } from "@/store/auth";
import type { MT5Status } from "@/types";

// ── MT5 Connect Form ──────────────────────────────────────────────────────────

const mt5Schema = z.object({
  account_number: z.coerce.number().int().positive("Must be a positive number"),
  password:       z.string().min(1, "Password is required"),
  server:         z.string().min(1, "Server name is required"),
});
type MT5Form = z.infer<typeof mt5Schema>;

function MT5Card() {
  const [status, setStatus] = useState<MT5Status | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const { register, handleSubmit, formState: { errors } } = useForm<MT5Form>({
    resolver: zodResolver(mt5Schema),
  });

  useEffect(() => {
    mt5Api.status().then((r) => setStatus(r.data)).catch(() => {});
  }, []);

  async function onConnect(data: MT5Form) {
    setConnecting(true);
    setErrorMsg(null); setSuccessMsg(null);
    try {
      await mt5Api.connect(data.account_number, data.password, data.server);
      const statusRes = await mt5Api.status();
      setStatus(statusRes.data);
      setSuccessMsg("MT5 connected successfully!");
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Connection failed. Check your credentials.";
      setErrorMsg(msg);
    } finally {
      setConnecting(false);
    }
  }

  async function onDisconnect() {
    setDisconnecting(true);
    try {
      await mt5Api.disconnect();
      setStatus((prev) => prev ? { ...prev, connected: false } : null);
      setSuccessMsg("Disconnected successfully.");
    } finally {
      setDisconnecting(false);
    }
  }

  return (
    <div className="card p-6">
      <div className="flex items-center gap-3 mb-5 pb-5 border-b border-surface-100">
        <div className="w-10 h-10 rounded-lg bg-navy-50 flex items-center justify-center">
          <PlugZap size={20} className="text-navy-700" />
        </div>
        <div>
          <h2 className="text-base font-semibold text-navy-900">MetaTrader 5 Account</h2>
          <p className="text-xs text-slate-500">Connect your broker account to enable automated trading</p>
        </div>
        {status?.connected && (
          <span className="ml-auto badge bg-success-50 text-success-700 border border-success-200">
            <span className="w-1.5 h-1.5 rounded-full bg-success-500 animate-pulse-dot" />
            Active
          </span>
        )}
      </div>

      {status?.connected ? (
        <div>
          <div className="grid grid-cols-2 gap-4 mb-5">
            {[
              { label: "Account",  value: String(status.account_number ?? "—") },
              { label: "Server",   value: status.server ?? "—"                 },
              { label: "Balance",  value: status.balance != null ? `$${status.balance.toLocaleString()}` : "—" },
              { label: "Equity",   value: status.equity  != null ? `$${status.equity.toLocaleString()}`  : "—" },
            ].map((f) => (
              <div key={f.label} className="bg-surface-50 rounded-lg p-3">
                <p className="text-xs text-slate-400 mb-0.5">{f.label}</p>
                <p className="text-sm font-semibold text-navy-900">{f.value}</p>
              </div>
            ))}
          </div>
          <button
            onClick={onDisconnect}
            disabled={disconnecting}
            className="btn-outline btn-sm text-danger-600 border-danger-200 hover:bg-danger-50"
          >
            {disconnecting ? <Loader2 size={14} className="animate-spin" /> : <Plug size={14} />}
            Disconnect account
          </button>
        </div>
      ) : (
        <form onSubmit={handleSubmit(onConnect)} className="space-y-4">
          {successMsg && (
            <div className="flex items-center gap-2 p-3 rounded-lg bg-success-50 border border-success-200 text-sm text-success-700">
              <CheckCircle size={15} /> {successMsg}
            </div>
          )}
          {errorMsg && (
            <div className="flex items-center gap-2 p-3 rounded-lg bg-danger-50 border border-danger-200 text-sm text-danger-700">
              <AlertCircle size={15} /> {errorMsg}
            </div>
          )}

          <div className="grid sm:grid-cols-2 gap-4">
            <div>
              <label className="label">Account Number</label>
              <input
                {...register("account_number")}
                type="number"
                placeholder="12345678"
                className={`input ${errors.account_number ? "input-error" : ""}`}
              />
              {errors.account_number && <p className="field-error">{errors.account_number.message}</p>}
            </div>
            <div>
              <label className="label">Server</label>
              <input
                {...register("server")}
                type="text"
                placeholder="BrokerName-Live"
                className={`input ${errors.server ? "input-error" : ""}`}
              />
              {errors.server && <p className="field-error">{errors.server.message}</p>}
            </div>
          </div>

          <div>
            <label className="label">MT5 Password</label>
            <input
              {...register("password")}
              type="password"
              placeholder="••••••••"
              className={`input ${errors.password ? "input-error" : ""}`}
            />
            {errors.password && <p className="field-error">{errors.password.message}</p>}
            <p className="text-xs text-slate-400 mt-1.5">
              Your credentials are encrypted with AES-256 and never stored in plain text.
            </p>
          </div>

          <button type="submit" disabled={connecting} className="btn-accent gap-2">
            {connecting ? (
              <><Loader2 size={15} className="animate-spin" /> Connecting…</>
            ) : (
              <><PlugZap size={15} /> Connect Account</>
            )}
          </button>
        </form>
      )}
    </div>
  );
}

// ── Profile Form ──────────────────────────────────────────────────────────────

const profileSchema = z.object({ full_name: z.string().min(2, "Name must be at least 2 characters") });
type ProfileForm = z.infer<typeof profileSchema>;

function ProfileCard() {
  const { user, setUser } = useAuthStore();
  const [saved, setSaved] = useState(false);
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<ProfileForm>({
    resolver: zodResolver(profileSchema),
    defaultValues: { full_name: user?.full_name || "" },
  });

  async function onSubmit(data: ProfileForm) {
    const res = await userApi.updateProfile(data);
    setUser(res.data);
    setSaved(true);
    setTimeout(() => setSaved(false), 3000);
  }

  return (
    <div className="card p-6">
      <h2 className="text-base font-semibold text-navy-900 mb-5">Profile</h2>
      <form onSubmit={handleSubmit(onSubmit)} className="space-y-4 max-w-sm">
        <div>
          <label className="label">Full Name</label>
          <input {...register("full_name")} className={`input ${errors.full_name ? "input-error" : ""}`} />
          {errors.full_name && <p className="field-error">{errors.full_name.message}</p>}
        </div>
        <div>
          <label className="label">Email</label>
          <input value={user?.email || ""} disabled className="input" />
        </div>
        <div className="flex items-center gap-3">
          <button type="submit" disabled={isSubmitting} className="btn-primary btn-sm">
            {isSubmitting ? "Saving…" : "Save changes"}
          </button>
          {saved && (
            <span className="flex items-center gap-1.5 text-xs text-success-600">
              <CheckCircle size={13} /> Saved
            </span>
          )}
        </div>
      </form>
    </div>
  );
}

export default function SettingsPage() {
  return (
    <div className="p-6 max-w-3xl">
      <h1 className="text-xl font-bold text-navy-900 mb-1.5">Settings</h1>
      <p className="text-sm text-slate-500 mb-6">Manage your account and MT5 connection</p>
      <div className="space-y-5">
        <MT5Card />
        <ProfileCard />
      </div>
    </div>
  );
}
