/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["class", '[data-theme="light"]'],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)", card: "var(--card)", card2: "var(--card2)",
        line: "var(--line)", txt: "var(--txt)", muted: "var(--muted)",
        accent: "var(--accent)", accent2: "var(--accent2)",
        ok: "var(--ok)", bad: "var(--bad)", warn: "var(--warn)",
      },
      borderRadius: { xl2: "1.15rem" },
      boxShadow: { glass: "var(--shadow)" },
    },
  },
  plugins: [],
};
