// Timezone list for the picker. Russian + CIS zones are surfaced first (this is
// a RU meeting tool); everything else comes from the browser's full IANA list.

export const TZ_RU = [
  "Europe/Kaliningrad", "Europe/Moscow", "Europe/Samara", "Asia/Yekaterinburg",
  "Asia/Omsk", "Asia/Novosibirsk", "Asia/Krasnoyarsk", "Asia/Irkutsk",
  "Asia/Yakutsk", "Asia/Vladivostok", "Asia/Magadan", "Asia/Kamchatka",
];

export const TZ_CIS = [
  "Europe/Kyiv", "Europe/Minsk", "Asia/Almaty", "Asia/Tashkent",
  "Asia/Tbilisi", "Asia/Yerevan", "Asia/Baku", "Asia/Bishkek", "Asia/Ashgabat",
];

// Every IANA zone the browser knows (modern browsers); [] on old ones.
export function allZones(): string[] {
  try {
    // @ts-expect-error supportedValuesOf is newer than the TS lib target
    return (Intl.supportedValuesOf?.("timeZone") as string[]) || [];
  } catch {
    return [];
  }
}

// Current UTC offset of a zone, e.g. "UTC+3" — recomputed per render is fine.
export function tzOffset(tz: string): string {
  try {
    const part = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "shortOffset" })
      .formatToParts(new Date())
      .find((p) => p.type === "timeZoneName");
    return (part?.value || "").replace("GMT", "UTC") || "UTC";
  } catch {
    return "";
  }
}

export function tzLabel(tz: string): string {
  const off = tzOffset(tz);
  return off ? `${tz} (${off})` : tz;
}

// Zones not already in the RU/CIS groups, for the "Все" group.
export function otherZones(): string[] {
  const shown = new Set([...TZ_RU, ...TZ_CIS]);
  return allZones().filter((z) => !shown.has(z));
}
