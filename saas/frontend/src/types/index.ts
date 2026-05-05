// ── Auth ──────────────────────────────────────────────────────────────────────

export interface User {
  id: string;
  email: string;
  full_name: string;
  role: "admin" | "user";
  is_active: boolean;
  is_verified: boolean;
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

// ── MT5 ───────────────────────────────────────────────────────────────────────

export type MT5ConnectionStatus = "connected" | "disconnected" | "connecting" | "error";

export interface MT5Account {
  id: string;
  user_id: string;
  account_number: number;
  server: string;
  broker_name: string | null;
  connection_status: MT5ConnectionStatus;
  is_active: boolean;
  last_connected_at: string | null;
  last_error: string | null;
  created_at: string;
}

export interface MT5Status {
  user_id: string;
  account_number: number | null;
  server: string | null;
  connected: boolean;
  last_heartbeat: string | null;
  balance: number | null;
  equity: number | null;
  error: string | null;
}

// ── Trades ────────────────────────────────────────────────────────────────────

export type TradeStatus = "open" | "closed" | "cancelled" | "pending";
export type TradeDirection = "BUY" | "SELL";

export interface Trade {
  id: string;
  user_id: string;
  mt5_ticket: number | null;
  symbol: string;
  direction: TradeDirection;
  lot_size: number;
  entry_price: number;
  stop_loss: number | null;
  take_profit: number | null;
  close_price: number | null;
  score: number | null;
  risk_amount: number | null;
  rr_ratio: number | null;
  pnl: number | null;
  status: TradeStatus;
  is_dry_run: boolean;
  opened_at: string;
  closed_at: string | null;
}

export interface TradeList {
  trades: Trade[];
  total: number;
  page: number;
  per_page: number;
}

// ── Analytics ─────────────────────────────────────────────────────────────────

export interface EquityPoint {
  date: string;
  balance: number;
  equity: number;
  net_pnl: number;
}

export interface PerformanceSummary {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  profit_factor: number | null;
  net_pnl: number;
  gross_profit: number;
  gross_loss: number;
  max_drawdown: number;
  avg_rr: number | null;
  best_trade: number | null;
  worst_trade: number | null;
  avg_win: number | null;
  avg_loss: number | null;
}

export interface SymbolBreakdown {
  symbol: string;
  trades: number;
  wins: number;
  net_pnl: number;
  win_rate: number;
}

export interface AnalyticsDashboard {
  summary: PerformanceSummary;
  equity_curve: EquityPoint[];
  symbol_breakdown: SymbolBreakdown[];
  monthly_pnl: { month: string; pnl: number }[];
}

// ── MT5 Live Positions ────────────────────────────────────────────────────────

export interface MT5Position {
  ticket: number;
  symbol: string;
  type: "BUY" | "SELL";
  volume: number;
  price_open: number;
  price_current: number;
  sl: number | null;
  tp: number | null;
  profit: number;
  time: number;        // Unix timestamp
  magic: number;
  comment: string;
}

// ── AI Weights ────────────────────────────────────────────────────────────────

export interface AIWeights {
  trend: number;
  liquidity: number;
  fvg: number;
  fibonacci: number;
  ema: number;
  confirmation: number;
  order_block: number;
}

export interface AIWeightsResponse {
  weights: AIWeights;
  last_updated: string | null;
}

// ── Admin ─────────────────────────────────────────────────────────────────────

export interface PlatformAnalytics {
  total_users: number;
  active_users: number;
  total_trades: number;
  open_trades: number;
}

export interface UserList {
  users: User[];
  total: number;
  page: number;
  per_page: number;
}

// ── API ───────────────────────────────────────────────────────────────────────

export interface ApiError {
  detail: string;
}
