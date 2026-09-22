/** Formatting and severity helpers shared across the dashboard. */

export const SEVERITY_ORDER = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];

/** One place where severity becomes colour, so nothing drifts. */
export const SEVERITY = {
  LOW: {
    label: "Low",
    text: "text-sev-low",
    bg: "bg-sev-low/10",
    border: "border-sev-low/35",
    dot: "bg-sev-low",
    hex: "#38bdf8",
  },
  MEDIUM: {
    label: "Medium",
    text: "text-sev-medium",
    bg: "bg-sev-medium/10",
    border: "border-sev-medium/35",
    dot: "bg-sev-medium",
    hex: "#f59e0b",
  },
  HIGH: {
    label: "High",
    text: "text-sev-high",
    bg: "bg-sev-high/10",
    border: "border-sev-high/35",
    dot: "bg-sev-high",
    hex: "#f97316",
  },
  CRITICAL: {
    label: "Critical",
    text: "text-sev-critical",
    bg: "bg-sev-critical/10",
    border: "border-sev-critical/35",
    dot: "bg-sev-critical",
    hex: "#ef4444",
  },
};

export const severity = (key) => SEVERITY[key] ?? SEVERITY.LOW;

export const WORKER_STATE = {
  SAFE: { hex: "#22c55e", text: "text-safe", label: "Safe" },
  WARNING: { hex: "#f59e0b", text: "text-sev-medium", label: "Warning" },
  DANGER: { hex: "#ef4444", text: "text-sev-critical", label: "Danger" },
};

export const workerState = (key) => WORKER_STATE[key] ?? WORKER_STATE.SAFE;

export const ZONE_TYPE_LABEL = {
  HEAVY_MACHINERY: "Heavy Machinery",
  OPEN_EDGE: "Open Edge",
  EXCAVATION: "Excavation",
  RESTRICTED_AREA: "Restricted Area",
  OTHER: "Other",
};

export const ALERT_TYPE_LABEL = {
  PPE_MISSING: "PPE Missing",
  ZONE_BREACH: "Zone Breach",
  WORKER_ENTERED_ZONE: "Entered Zone",
  WORKER_EXITED_ZONE: "Left Zone",
  SYSTEM_WARNING: "System Warning",
};

export const PPE_LABEL = {
  helmet: "Hardhat",
  vest: "Safety Vest",
  mask: "Mask",
};

/** "3h 10m", "42s" - compact enough for a dense table. */
export function duration(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return "-";
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
}

/** "2 seconds ago". Falls back to the raw value if it cannot be parsed. */
export function relativeTime(iso, now = Date.now()) {
  if (!iso) return "-";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return String(iso);
  const delta = Math.round((now - t) / 1000);
  if (delta < 5) return "just now";
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

export function clockTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Position in metres, or an explicit note that there is no spatial fix. */
export function metres(pos) {
  if (!pos || pos.x == null || pos.y == null) return "no fix";
  return `X ${pos.x.toFixed(1)} m, Y ${pos.y.toFixed(1)} m`;
}

export function ppeSummary(ppe) {
  if (!ppe) return [];
  return Object.entries(ppe).map(([item, value]) => ({
    item,
    label: PPE_LABEL[item] ?? item,
    // Tri-state: true = worn, false = detected missing, null = not observed.
    status: value === true ? "OK" : value === false ? "MISSING" : "UNKNOWN",
  }));
}

export function cx(...parts) {
  return parts.filter(Boolean).join(" ");
}
