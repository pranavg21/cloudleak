import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Concrete + oxidized copper. Industrial metering, not dashboard neon.
        concrete: "#DFE3E0",
        panel: "#F2F4F1",
        ink: "#12191C",
        graphite: "#4A565A",
        hairline: "#C3C9C4",
        verdigris: "#2E6B5E",
        rust: "#B4402A",
        brass: "#8A6A2F",
      },
      fontFamily: {
        display: ["var(--font-display)", "system-ui", "sans-serif"],
        sans: ["var(--font-body)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      letterSpacing: {
        signage: "-0.03em",
      },
    },
  },
  plugins: [],
};

export default config;
