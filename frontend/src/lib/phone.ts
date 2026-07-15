// Live-format a phone as a Russian +7 number while typing:
//   "8..."          -> "+7..."   (leading 8 becomes 7)
//   "923..."        -> "+7923..." (bare local number gets +7)
//   "+7 923..."     -> "+7923..." (kept)
// Digits only, capped at +7 plus 10 digits.
export function formatRuPhone(raw: string): string {
  let d = (raw || "").replace(/\D/g, "");
  if (!d) return "";
  if (d[0] === "8") d = "7" + d.slice(1);
  else if (d[0] !== "7") d = "7" + d;
  return "+" + d.slice(0, 11);
}
