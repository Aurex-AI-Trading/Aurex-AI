"use client";
import { useEffect, useRef, useState, useCallback } from "react";
import Cookies from "js-cookie";

const WS_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000")
  .replace(/^http/, "ws");

export interface WSStatus {
  type: "status";
  connected: boolean;
  balance: number | null;
  equity: number | null;
  open_trades: number;
  timestamp: string;
}

export function useWebSocket() {
  const wsRef          = useRef<WebSocket | null>(null);
  const timerRef       = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef     = useRef(true);
  const [wsLive, setWsLive]     = useState(false);
  const [status, setStatus]     = useState<WSStatus | null>(null);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;
    const token = Cookies.get("access_token");
    if (!token) return;

    try {
      const ws = new WebSocket(`${WS_URL}/ws?token=${encodeURIComponent(token)}`);
      wsRef.current = ws;

      ws.onopen  = () => { if (mountedRef.current) setWsLive(true); };
      ws.onclose = () => {
        if (!mountedRef.current) return;
        setWsLive(false);
        timerRef.current = setTimeout(connect, 5_000);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data as string) as WSStatus;
          if (msg.type === "status" && mountedRef.current) setStatus(msg);
        } catch { /* ignore malformed */ }
      };
    } catch { /* ignore connection errors */ }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    connect();
    return () => {
      mountedRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return { status, wsLive };
}
