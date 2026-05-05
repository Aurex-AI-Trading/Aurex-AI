import Link from "next/link";
import {
  BarChart2, Bot, Shield, TrendingUp, Zap, Globe,
  ArrowRight, CheckCircle, ChevronRight, Activity,
} from "lucide-react";

// ── Navbar ────────────────────────────────────────────────────────────────────

function Navbar() {
  return (
    <header className="fixed top-0 left-0 right-0 z-50 bg-white/95 backdrop-blur-sm border-b border-surface-200">
      <div className="max-w-7xl mx-auto px-6 h-16 flex items-center justify-between">
        <Link href="/" className="flex items-center gap-3">
          <img src="/logo.svg" alt="Aurex AI" className="h-9 w-auto" />
        </Link>
        <nav className="hidden md:flex items-center gap-8">
          {[
            { href: "#features", label: "Features" },
            { href: "#how-it-works", label: "How it works" },
            { href: "#pricing", label: "Pricing" },
          ].map((link) => (
            <a
              key={link.href}
              href={link.href}
              className="text-sm font-medium text-slate-600 hover:text-navy-900 transition-colors"
            >
              {link.label}
            </a>
          ))}
        </nav>
        <div className="flex items-center gap-3">
          <Link href="/login" className="btn-outline btn-sm hidden md:inline-flex">
            Sign in
          </Link>
          <Link href="/register" className="btn-accent btn-sm">
            Get started <ArrowRight size={14} />
          </Link>
        </div>
      </div>
    </header>
  );
}

// ── Hero ──────────────────────────────────────────────────────────────────────

function Hero() {
  return (
    <section className="relative pt-32 pb-20 overflow-hidden">
      {/* Background gradient */}
      <div className="absolute inset-0 bg-gradient-to-b from-navy-950 via-navy-900 to-navy-800 -z-10" />
      <div
        className="absolute inset-0 opacity-[0.07] -z-10"
        style={{
          backgroundImage: `radial-gradient(circle at 1px 1px, #F97316 1px, transparent 0)`,
          backgroundSize: "48px 48px",
        }}
      />

      <div className="max-w-7xl mx-auto px-6">
        <div className="max-w-3xl">
          {/* Tag */}
          <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-white/10 border border-white/20 text-accent-400 text-xs font-semibold mb-8">
            <span className="w-1.5 h-1.5 rounded-full bg-accent-400 animate-pulse-dot" />
            Institutional-grade algorithmic trading
          </div>

          <h1 className="text-5xl md:text-6xl font-bold text-white leading-[1.08] tracking-tight mb-6 text-balance">
            Trade smarter with{" "}
            <span className="text-accent-400">AI precision</span>
          </h1>

          <p className="text-lg text-navy-200 mb-10 max-w-2xl leading-relaxed">
            Connect your MetaTrader 5 account in one click. Aurex AI continuously scans
            markets, identifies high-probability setups, and executes trades based on
            institutional-grade confluence analysis — 24/7, without emotion.
          </p>

          <div className="flex flex-col sm:flex-row gap-4">
            <Link href="/register" className="btn bg-accent-500 text-white hover:bg-accent-600 btn-lg gap-2">
              Start trading free <ArrowRight size={18} />
            </Link>
            <a
              href="#how-it-works"
              className="btn border border-white/20 text-white hover:bg-white/10 btn-lg"
            >
              See how it works
            </a>
          </div>
        </div>

        {/* Live stats panel */}
        <div className="mt-16 grid grid-cols-2 md:grid-cols-4 gap-4 max-w-3xl">
          {[
            { label: "Win Rate",       value: "68.4%",  sub: "30-day average"    },
            { label: "Profit Factor",  value: "2.31",   sub: "All time"          },
            { label: "Symbols",        value: "10+",    sub: "Forex & Gold"      },
            { label: "Avg R:R",        value: "2.5:1",  sub: "Minimum threshold" },
          ].map((stat) => (
            <div
              key={stat.label}
              className="bg-white/5 border border-white/10 rounded-xl p-4 backdrop-blur-sm"
            >
              <div className="text-2xl font-bold text-white mb-0.5">{stat.value}</div>
              <div className="text-sm font-medium text-navy-100">{stat.label}</div>
              <div className="text-xs text-navy-300 mt-0.5">{stat.sub}</div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

// ── Trusted by / Social proof ─────────────────────────────────────────────────

function SocialProof() {
  return (
    <section className="bg-surface-50 border-y border-surface-200 py-10">
      <div className="max-w-7xl mx-auto px-6 text-center">
        <p className="text-xs font-semibold text-slate-400 uppercase tracking-widest mb-6">
          Trusted by traders across the globe
        </p>
        <div className="flex flex-wrap items-center justify-center gap-10 text-slate-300">
          {["MetaTrader 5", "HFM", "IC Markets", "Pepperstone", "XM", "OANDA"].map((broker) => (
            <span key={broker} className="text-sm font-semibold text-slate-400">
              {broker}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}

// ── Features ──────────────────────────────────────────────────────────────────

const FEATURES = [
  {
    icon: Bot,
    title: "AI Confluence Engine",
    desc: "Multi-factor signal scoring: trend alignment, liquidity sweeps, Fair Value Gaps, Fibonacci, and candle confirmation — all weighted and combined into a single precision score.",
  },
  {
    icon: Zap,
    title: "1-Click MT5 Integration",
    desc: "Enter your account number, password, and server. Aurex AI establishes a persistent, encrypted connection to your broker instantly — no software to install.",
  },
  {
    icon: Shield,
    title: "Institutional Risk Control",
    desc: "Per-trade risk percentage, dynamic stop-loss from sweep levels, minimum R:R enforcement, daily loss limits, and adaptive position sizing. Capital protection first.",
  },
  {
    icon: TrendingUp,
    title: "Real-Time Analytics",
    desc: "Live equity curves, win-rate tracking, profit factor analysis, symbol-level P&L breakdown, and drawdown monitoring — all in one clean dashboard.",
  },
  {
    icon: Globe,
    title: "Multi-Symbol Scanning",
    desc: "Concurrent scanning across EURUSD, GBPUSD, XAUUSD, and 7+ more pairs simultaneously. Each symbol processed independently with its own cooldown and risk controls.",
  },
  {
    icon: BarChart2,
    title: "Strategy Transparency",
    desc: "Every trade decision is logged with full reasoning — score breakdown, direction logic, skip reasons. You always know why Aurex AI traded or chose to wait.",
  },
];

function Features() {
  return (
    <section id="features" className="py-24">
      <div className="max-w-7xl mx-auto px-6">
        <div className="max-w-2xl mb-16">
          <p className="text-accent-500 text-sm font-semibold uppercase tracking-wider mb-3">
            Platform capabilities
          </p>
          <h2 className="text-4xl font-bold text-navy-900 mb-4">
            Built for serious traders
          </h2>
          <p className="text-lg text-slate-500 leading-relaxed">
            Every component of Aurex AI is engineered to institutional standards.
            No shortcuts, no guesswork — just systematic, evidence-based execution.
          </p>
        </div>

        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-6">
          {FEATURES.map((f) => (
            <div key={f.title} className="card p-6 hover:shadow-card-md transition-shadow duration-200 group">
              <div className="w-10 h-10 rounded-lg bg-navy-50 flex items-center justify-center mb-4
                              group-hover:bg-navy-900 transition-colors duration-200">
                <f.icon size={20} className="text-navy-700 group-hover:text-white transition-colors duration-200" />
              </div>
              <h3 className="text-base font-semibold text-navy-900 mb-2">{f.title}</h3>
              <p className="text-sm text-slate-500 leading-relaxed">{f.desc}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

// ── How it works ──────────────────────────────────────────────────────────────

function HowItWorks() {
  const steps = [
    {
      num: "01",
      title: "Create your account",
      desc: "Register in under 60 seconds. No credit card required to start.",
    },
    {
      num: "02",
      title: "Connect MT5 in one click",
      desc: "Enter your broker account number, password, and server. Aurex AI handles the rest — encrypted, secure, persistent.",
    },
    {
      num: "03",
      title: "Aurex AI trades for you",
      desc: "The engine scans all symbols every 15 seconds, executes high-confluence trades, manages risk automatically, and logs every decision.",
    },
  ];

  return (
    <section id="how-it-works" className="py-24 bg-surface-50">
      <div className="max-w-7xl mx-auto px-6">
        <div className="max-w-2xl mb-16">
          <p className="text-accent-500 text-sm font-semibold uppercase tracking-wider mb-3">
            Simple setup
          </p>
          <h2 className="text-4xl font-bold text-navy-900 mb-4">
            From signup to live trading in minutes
          </h2>
        </div>

        <div className="grid md:grid-cols-3 gap-8">
          {steps.map((step, i) => (
            <div key={step.num} className="relative">
              {i < steps.length - 1 && (
                <div className="hidden md:block absolute top-8 left-full w-full h-px bg-surface-200 -translate-x-4 z-0" />
              )}
              <div className="relative z-10">
                <div className="w-16 h-16 rounded-2xl bg-navy-900 flex items-center justify-center mb-5">
                  <span className="text-2xl font-bold text-accent-400">{step.num}</span>
                </div>
                <h3 className="text-lg font-semibold text-navy-900 mb-2">{step.title}</h3>
                <p className="text-slate-500 leading-relaxed">{step.desc}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

// ── Pricing ───────────────────────────────────────────────────────────────────

const PLANS = [
  {
    name: "Starter",
    price: "$49",
    period: "/month",
    desc: "For individual traders getting started with algorithmic trading.",
    features: [
      "1 MT5 account",
      "5 trading symbols",
      "30-day trade history",
      "Basic analytics",
      "Email support",
    ],
    cta: "Get started",
    highlighted: false,
  },
  {
    name: "Pro",
    price: "$149",
    period: "/month",
    desc: "For active traders who need full symbol coverage and priority support.",
    features: [
      "1 MT5 account",
      "All 10+ symbols",
      "Unlimited trade history",
      "Advanced analytics & equity curve",
      "Priority support",
      "Strategy transparency logs",
    ],
    cta: "Start Pro trial",
    highlighted: true,
  },
  {
    name: "Enterprise",
    price: "Custom",
    period: "",
    desc: "For funds and professional trading desks managing multiple accounts.",
    features: [
      "Multiple MT5 accounts",
      "All symbols",
      "Custom risk profiles per account",
      "Dedicated infrastructure",
      "SLA guarantee",
      "Onboarding & training",
    ],
    cta: "Contact sales",
    highlighted: false,
  },
];

function Pricing() {
  return (
    <section id="pricing" className="py-24">
      <div className="max-w-7xl mx-auto px-6">
        <div className="max-w-2xl mx-auto text-center mb-16">
          <p className="text-accent-500 text-sm font-semibold uppercase tracking-wider mb-3">
            Transparent pricing
          </p>
          <h2 className="text-4xl font-bold text-navy-900 mb-4">
            Simple, predictable pricing
          </h2>
          <p className="text-lg text-slate-500">
            No hidden fees. Cancel anytime.
          </p>
        </div>

        <div className="grid md:grid-cols-3 gap-6 max-w-5xl mx-auto">
          {PLANS.map((plan) => (
            <div
              key={plan.name}
              className={`rounded-2xl p-8 flex flex-col ${
                plan.highlighted
                  ? "bg-navy-900 text-white shadow-2xl scale-[1.02]"
                  : "card"
              }`}
            >
              {plan.highlighted && (
                <div className="mb-4">
                  <span className="inline-flex items-center gap-1 px-3 py-1 rounded-full
                                   bg-accent-500/20 text-accent-400 text-xs font-semibold">
                    Most popular
                  </span>
                </div>
              )}
              <div className="mb-6">
                <h3 className={`text-lg font-semibold mb-1 ${plan.highlighted ? "text-white" : "text-navy-900"}`}>
                  {plan.name}
                </h3>
                <div className="flex items-baseline gap-1 mb-2">
                  <span className={`text-4xl font-bold ${plan.highlighted ? "text-white" : "text-navy-900"}`}>
                    {plan.price}
                  </span>
                  {plan.period && (
                    <span className={`text-sm ${plan.highlighted ? "text-navy-300" : "text-slate-500"}`}>
                      {plan.period}
                    </span>
                  )}
                </div>
                <p className={`text-sm leading-relaxed ${plan.highlighted ? "text-navy-300" : "text-slate-500"}`}>
                  {plan.desc}
                </p>
              </div>

              <ul className="space-y-3 mb-8 flex-1">
                {plan.features.map((feature) => (
                  <li key={feature} className="flex items-start gap-2.5">
                    <CheckCircle
                      size={16}
                      className={`mt-0.5 flex-shrink-0 ${plan.highlighted ? "text-accent-400" : "text-success-600"}`}
                    />
                    <span className={`text-sm ${plan.highlighted ? "text-navy-200" : "text-slate-600"}`}>
                      {feature}
                    </span>
                  </li>
                ))}
              </ul>

              <Link
                href="/register"
                className={`btn w-full justify-center ${
                  plan.highlighted
                    ? "bg-accent-500 text-white hover:bg-accent-600"
                    : "btn-outline"
                }`}
              >
                {plan.cta}
              </Link>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

// ── CTA ───────────────────────────────────────────────────────────────────────

function CTA() {
  return (
    <section className="py-24 bg-navy-900">
      <div className="max-w-7xl mx-auto px-6 text-center">
        <div className="max-w-2xl mx-auto">
          <h2 className="text-4xl font-bold text-white mb-5">
            Ready to trade with an edge?
          </h2>
          <p className="text-lg text-navy-300 mb-10 leading-relaxed">
            Join traders already using Aurex AI to execute systematic, rules-based strategies
            across major forex pairs and gold — without being at their screens.
          </p>
          <div className="flex flex-col sm:flex-row gap-4 justify-center">
            <Link href="/register" className="btn bg-accent-500 text-white hover:bg-accent-600 btn-lg">
              Create free account <ArrowRight size={18} />
            </Link>
            <Link href="/login" className="btn border border-white/20 text-white hover:bg-white/10 btn-lg">
              Sign in
            </Link>
          </div>
        </div>
      </div>
    </section>
  );
}

// ── Footer ────────────────────────────────────────────────────────────────────

function Footer() {
  return (
    <footer className="bg-navy-950 text-navy-400 py-12">
      <div className="max-w-7xl mx-auto px-6">
        <div className="flex flex-col md:flex-row justify-between gap-8 mb-10">
          <div className="max-w-xs">
            <img src="/logo.svg" alt="Aurex AI" className="h-9 w-auto mb-4 brightness-[2] saturate-0" />
            <p className="text-sm leading-relaxed">
              Intelligent algorithmic trading for disciplined investors and active traders.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-8 text-sm">
            <div>
              <p className="font-semibold text-white mb-3">Platform</p>
              <ul className="space-y-2">
                <li><a href="#features" className="hover:text-white transition-colors">Features</a></li>
                <li><a href="#pricing" className="hover:text-white transition-colors">Pricing</a></li>
                <li><Link href="/login" className="hover:text-white transition-colors">Login</Link></li>
                <li><Link href="/register" className="hover:text-white transition-colors">Register</Link></li>
              </ul>
            </div>
            <div>
              <p className="font-semibold text-white mb-3">Legal</p>
              <ul className="space-y-2">
                <li><a href="#" className="hover:text-white transition-colors">Privacy Policy</a></li>
                <li><a href="#" className="hover:text-white transition-colors">Terms of Service</a></li>
                <li><a href="#" className="hover:text-white transition-colors">Risk Disclosure</a></li>
              </ul>
            </div>
          </div>
        </div>
        <div className="border-t border-white/10 pt-6 flex flex-col md:flex-row items-center justify-between gap-4">
          <p className="text-xs">
            © {new Date().getFullYear()} Aurex AI. All rights reserved.
          </p>
          <p className="text-xs text-navy-500 max-w-lg text-center md:text-right">
            Trading involves substantial risk of loss. Past performance is not indicative of
            future results. Aurex AI is not a financial advisor.
          </p>
        </div>
      </div>
    </footer>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function LandingPage() {
  return (
    <div className="min-h-screen">
      <Navbar />
      <main>
        <Hero />
        <SocialProof />
        <Features />
        <HowItWorks />
        <Pricing />
        <CTA />
      </main>
      <Footer />
    </div>
  );
}
