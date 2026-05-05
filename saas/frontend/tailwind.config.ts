import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        navy: {
          50:  "#E8EEF7",
          100: "#C5D3EC",
          200: "#9FB4DF",
          300: "#7895D2",
          400: "#5C7DC8",
          500: "#3F65BE",
          600: "#2E54A8",
          700: "#1E3F8A",
          800: "#132B6B",
          900: "#0A1628",
          950: "#060E1A",
        },
        accent: {
          50:  "#FFF7ED",
          100: "#FFEDD5",
          200: "#FED7AA",
          300: "#FDBA74",
          400: "#FB923C",
          500: "#F97316",
          600: "#EA6B00",
          700: "#C2570B",
          800: "#9A440D",
          900: "#7C370E",
        },
        success: {
          50:  "#F0FDF4",
          100: "#DCFCE7",
          200: "#BBF7D0",
          500: "#22C55E",
          600: "#16A34A",
          700: "#15803D",
        },
        danger: {
          50:  "#FFF1F2",
          100: "#FFE4E6",
          200: "#FECDD3",
          500: "#EF4444",
          600: "#DC2626",
          700: "#B91C1C",
        },
        surface: {
          DEFAULT: "#FFFFFF",
          50:  "#F8FAFC",
          100: "#F1F5F9",
          200: "#E2E8F0",
          300: "#CBD5E1",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "sans-serif"],
        mono: ["JetBrains Mono", "Fira Code", "monospace"],
      },
      borderRadius: {
        "4xl": "2rem",
      },
      boxShadow: {
        card:    "0 1px 3px 0 rgb(0 0 0 / 0.04), 0 1px 2px -1px rgb(0 0 0 / 0.04)",
        "card-md": "0 4px 6px -1px rgb(0 0 0 / 0.06), 0 2px 4px -2px rgb(0 0 0 / 0.04)",
        "card-lg": "0 10px 15px -3px rgb(0 0 0 / 0.06), 0 4px 6px -4px rgb(0 0 0 / 0.04)",
        "inset-sm": "inset 0 1px 2px 0 rgb(0 0 0 / 0.05)",
      },
      animation: {
        "fade-up":   "fadeUp 0.5s ease-out both",
        "fade-in":   "fadeIn 0.4s ease-out both",
        "pulse-dot": "pulseDot 2s ease-in-out infinite",
      },
      keyframes: {
        fadeUp: {
          from: { opacity: "0", transform: "translateY(16px)" },
          to:   { opacity: "1", transform: "translateY(0)" },
        },
        fadeIn: {
          from: { opacity: "0" },
          to:   { opacity: "1" },
        },
        pulseDot: {
          "0%, 100%": { opacity: "1" },
          "50%":      { opacity: "0.4" },
        },
      },
    },
  },
  plugins: [],
};

export default config;
