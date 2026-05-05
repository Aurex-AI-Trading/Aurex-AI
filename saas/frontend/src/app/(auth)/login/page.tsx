"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Eye, EyeOff, ArrowRight, AlertCircle } from "lucide-react";
import { authApi } from "@/lib/api";
import { useAuthStore } from "@/store/auth";

const schema = z.object({
  email: z.string().email("Enter a valid email"),
  password: z.string().min(1, "Password is required"),
});
type FormData = z.infer<typeof schema>;

export default function LoginPage() {
  const router = useRouter();
  const { login } = useAuthStore();
  const [showPwd, setShowPwd] = useState(false);
  const [apiError, setApiError] = useState<string | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormData>({ resolver: zodResolver(schema) });

  async function onSubmit(data: FormData) {
    setApiError(null);
    try {
      const res = await authApi.login(data.email, data.password);
      const tokens = res.data;
      const meRes = await import("@/lib/api").then((m) =>
        m.api.get("/auth/me", {
          headers: { Authorization: `Bearer ${tokens.access_token}` },
        })
      );
      login(meRes.data, tokens.access_token, tokens.refresh_token);
      router.push(meRes.data.role === "admin" ? "/admin" : "/dashboard");
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Login failed. Please try again.";
      setApiError(msg);
    }
  }

  return (
    <div className="min-h-screen bg-surface-50 flex">
      {/* Left panel */}
      <div className="hidden lg:flex flex-col justify-between w-[480px] flex-shrink-0 bg-navy-900 p-10">
        <Link href="/">
          <img src="/logo.svg" alt="Aurex AI" className="h-10 w-auto" />
        </Link>
        <div>
          <blockquote className="text-navy-100 text-lg leading-relaxed mb-6">
            &ldquo;Aurex AI has completely transformed how I approach trading. The AI
            executes setups I would have missed — consistently and without emotion.&rdquo;
          </blockquote>
          <p className="text-navy-400 text-sm">James R. — Professional FX Trader</p>
        </div>
        <div className="grid grid-cols-2 gap-4">
          {[
            { label: "Active traders", value: "1,200+" },
            { label: "Trades executed", value: "48,000+" },
            { label: "Avg win rate",    value: "68%" },
            { label: "Profit factor",   value: "2.3" },
          ].map((s) => (
            <div key={s.label} className="bg-white/5 rounded-xl p-4">
              <div className="text-xl font-bold text-white">{s.value}</div>
              <div className="text-xs text-navy-400 mt-0.5">{s.label}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Right panel */}
      <div className="flex-1 flex flex-col items-center justify-center px-6 py-12">
        <div className="w-full max-w-md">
          <div className="lg:hidden mb-8">
            <Link href="/">
              <img src="/logo.svg" alt="Aurex AI" className="h-9 w-auto" />
            </Link>
          </div>

          <div className="mb-8">
            <h1 className="text-2xl font-bold text-navy-900 mb-1.5">Welcome back</h1>
            <p className="text-slate-500 text-sm">
              Sign in to your Aurex AI account
            </p>
          </div>

          {apiError && (
            <div className="mb-5 flex items-start gap-3 p-3.5 rounded-lg bg-danger-50 border border-danger-200">
              <AlertCircle size={16} className="text-danger-600 mt-0.5 flex-shrink-0" />
              <p className="text-sm text-danger-700">{apiError}</p>
            </div>
          )}

          <form onSubmit={handleSubmit(onSubmit)} className="space-y-5">
            <div>
              <label className="label" htmlFor="email">Email</label>
              <input
                {...register("email")}
                id="email"
                type="email"
                autoComplete="email"
                placeholder="you@example.com"
                className={`input ${errors.email ? "input-error" : ""}`}
              />
              {errors.email && <p className="field-error">{errors.email.message}</p>}
            </div>

            <div>
              <div className="flex items-center justify-between mb-1.5">
                <label className="label mb-0" htmlFor="password">Password</label>
                <a href="#" className="text-xs text-navy-700 hover:text-accent-500 font-medium">
                  Forgot password?
                </a>
              </div>
              <div className="relative">
                <input
                  {...register("password")}
                  id="password"
                  type={showPwd ? "text" : "password"}
                  autoComplete="current-password"
                  placeholder="••••••••"
                  className={`input pr-10 ${errors.password ? "input-error" : ""}`}
                />
                <button
                  type="button"
                  onClick={() => setShowPwd(!showPwd)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
                >
                  {showPwd ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
              {errors.password && <p className="field-error">{errors.password.message}</p>}
            </div>

            <button
              type="submit"
              disabled={isSubmitting}
              className="btn-primary w-full justify-center py-2.5"
            >
              {isSubmitting ? (
                <span className="flex items-center gap-2">
                  <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  Signing in…
                </span>
              ) : (
                <span className="flex items-center gap-2">
                  Sign in <ArrowRight size={16} />
                </span>
              )}
            </button>
          </form>

          <p className="mt-6 text-center text-sm text-slate-500">
            Don&apos;t have an account?{" "}
            <Link href="/register" className="text-navy-700 font-semibold hover:text-accent-500">
              Create one free
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
