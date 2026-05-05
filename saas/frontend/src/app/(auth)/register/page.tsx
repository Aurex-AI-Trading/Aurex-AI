"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Eye, EyeOff, ArrowRight, AlertCircle, CheckCircle } from "lucide-react";
import { authApi } from "@/lib/api";
import { useAuthStore } from "@/store/auth";

const schema = z.object({
  full_name: z.string().min(2, "Name must be at least 2 characters"),
  email: z.string().email("Enter a valid email"),
  password: z
    .string()
    .min(8, "At least 8 characters")
    .regex(/[A-Z]/, "Must include an uppercase letter")
    .regex(/\d/, "Must include a number"),
});
type FormData = z.infer<typeof schema>;

const PASSWORD_RULES = [
  { label: "At least 8 characters",    test: (v: string) => v.length >= 8 },
  { label: "One uppercase letter",      test: (v: string) => /[A-Z]/.test(v) },
  { label: "One number",               test: (v: string) => /\d/.test(v) },
];

export default function RegisterPage() {
  const router = useRouter();
  const { login } = useAuthStore();
  const [showPwd, setShowPwd] = useState(false);
  const [apiError, setApiError] = useState<string | null>(null);

  const {
    register,
    handleSubmit,
    watch,
    formState: { errors, isSubmitting },
  } = useForm<FormData>({ resolver: zodResolver(schema) });

  const pwdValue = watch("password", "");

  async function onSubmit(data: FormData) {
    setApiError(null);
    try {
      const res = await authApi.register(data.email, data.password, data.full_name);
      const tokens = res.data;
      const meRes = await import("@/lib/api").then((m) =>
        m.api.get("/auth/me", {
          headers: { Authorization: `Bearer ${tokens.access_token}` },
        })
      );
      login(meRes.data, tokens.access_token, tokens.refresh_token);
      router.push("/dashboard");
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Registration failed. Please try again.";
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
          <h2 className="text-3xl font-bold text-white mb-4 leading-tight">
            Start trading with institutional precision
          </h2>
          <p className="text-navy-300 leading-relaxed text-sm mb-8">
            Connect your MT5 account in under 2 minutes. Aurex AI scans 10+ symbols
            24/7 and executes only the highest-probability setups.
          </p>
          <ul className="space-y-3">
            {[
              "No software to install",
              "Encrypted credentials — always",
              "Isolated execution per account",
              "Cancel any time",
            ].map((item) => (
              <li key={item} className="flex items-center gap-3 text-sm text-navy-200">
                <CheckCircle size={16} className="text-accent-400 flex-shrink-0" />
                {item}
              </li>
            ))}
          </ul>
        </div>
        <p className="text-xs text-navy-500">
          By creating an account you agree to our Terms of Service and Privacy Policy.
        </p>
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
            <h1 className="text-2xl font-bold text-navy-900 mb-1.5">Create your account</h1>
            <p className="text-slate-500 text-sm">Get started with Aurex AI in 60 seconds</p>
          </div>

          {apiError && (
            <div className="mb-5 flex items-start gap-3 p-3.5 rounded-lg bg-danger-50 border border-danger-200">
              <AlertCircle size={16} className="text-danger-600 mt-0.5 flex-shrink-0" />
              <p className="text-sm text-danger-700">{apiError}</p>
            </div>
          )}

          <form onSubmit={handleSubmit(onSubmit)} className="space-y-5">
            <div>
              <label className="label" htmlFor="full_name">Full name</label>
              <input
                {...register("full_name")}
                id="full_name"
                type="text"
                autoComplete="name"
                placeholder="John Smith"
                className={`input ${errors.full_name ? "input-error" : ""}`}
              />
              {errors.full_name && <p className="field-error">{errors.full_name.message}</p>}
            </div>

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
              <label className="label" htmlFor="password">Password</label>
              <div className="relative">
                <input
                  {...register("password")}
                  id="password"
                  type={showPwd ? "text" : "password"}
                  autoComplete="new-password"
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
              {/* Password strength rules */}
              {pwdValue && (
                <ul className="mt-2 space-y-1">
                  {PASSWORD_RULES.map((rule) => {
                    const ok = rule.test(pwdValue);
                    return (
                      <li key={rule.label} className={`flex items-center gap-2 text-xs ${ok ? "text-success-600" : "text-slate-400"}`}>
                        <CheckCircle size={12} className={ok ? "text-success-600" : "text-slate-300"} />
                        {rule.label}
                      </li>
                    );
                  })}
                </ul>
              )}
              {errors.password && !pwdValue && (
                <p className="field-error">{errors.password.message}</p>
              )}
            </div>

            <button
              type="submit"
              disabled={isSubmitting}
              className="btn-primary w-full justify-center py-2.5 mt-2"
            >
              {isSubmitting ? (
                <span className="flex items-center gap-2">
                  <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  Creating account…
                </span>
              ) : (
                <span className="flex items-center gap-2">
                  Create account <ArrowRight size={16} />
                </span>
              )}
            </button>
          </form>

          <p className="mt-6 text-center text-sm text-slate-500">
            Already have an account?{" "}
            <Link href="/login" className="text-navy-700 font-semibold hover:text-accent-500">
              Sign in
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
