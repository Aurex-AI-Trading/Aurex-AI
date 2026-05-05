"use client";
import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { User } from "@/types";
import { clearAuth, setAuth } from "@/lib/api";

interface AuthState {
  user: User | null;
  isLoading: boolean;
  setUser: (user: User | null) => void;
  setLoading: (v: boolean) => void;
  logout: () => void;
  login: (user: User, accessToken: string, refreshToken: string) => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      user: null,
      isLoading: false,
      setUser: (user) => set({ user }),
      setLoading: (isLoading) => set({ isLoading }),
      login: (user, accessToken, refreshToken) => {
        setAuth(accessToken, refreshToken);
        set({ user });
      },
      logout: () => {
        clearAuth();
        set({ user: null });
      },
    }),
    {
      name: "aurex-auth",
      partialize: (s) => ({ user: s.user }),
    }
  )
);
