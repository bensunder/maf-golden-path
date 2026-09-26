/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Inter Variable"', '"Segoe UI"', "system-ui", "-apple-system", "sans-serif"],
        mono: ['ui-monospace', '"Cascadia Code"', '"SF Mono"', "Menlo", "Consolas", "monospace"],
      },
      colors: {
        // One restrained accent; everything else is neutral.
        accent: {
          50: "#eef4ff", 100: "#dbe7fe", 200: "#bfd3fe", 300: "#93b4fd", 500: "#3b6ef6",
          600: "#2556e8", 700: "#1d44c9", 800: "#1e3ba3",
        },
        canvas: "#f7f7f8",
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },
      boxShadow: {
        card: "0 1px 2px 0 rgb(16 24 40 / 0.04)",
        pop: "0 8px 24px -6px rgb(16 24 40 / 0.14), 0 2px 6px -2px rgb(16 24 40 / 0.08)",
      },
      keyframes: {
        shimmer: { "100%": { transform: "translateX(100%)" } },
        "fade-in": { from: { opacity: "0" }, to: { opacity: "1" } },
        "slide-in": { from: { transform: "translateX(100%)" }, to: { transform: "translateX(0)" } },
        "scale-in": { from: { opacity: "0", transform: "scale(.97)" }, to: { opacity: "1", transform: "scale(1)" } },
      },
      animation: {
        "fade-in": "fade-in 150ms ease-out",
        "slide-in": "slide-in 200ms cubic-bezier(.2,.8,.2,1)",
        "scale-in": "scale-in 150ms ease-out",
      },
    },
  },
  plugins: [],
};
