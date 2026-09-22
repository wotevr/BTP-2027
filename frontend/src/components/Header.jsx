import {
  Activity,
  BarChart3,
  HardHat,
  Loader2,
  Map,
  Play,
  Radio,
  Shapes,
  Square,
  WifiOff,
} from "lucide-react";
import { cx } from "../lib/format";

const TABS = [
  { id: "live", label: "Live Monitor", icon: Map },
  { id: "analytics", label: "Analytics", icon: BarChart3 },
  { id: "zones", label: "Danger Zones", icon: Shapes },
];

export default function Header({
  tab,
  onTab,
  status,
  state,
  demoMode,
  demoBusy,
  onToggleDemo,
}) {
  const fps = state?.fps ?? 0;
  const camera = state?.camera_id ?? "-";

  return (
    <header className="flex shrink-0 flex-col border-b border-graphite-800 bg-graphite-900">
      <div className="flex h-14 items-center gap-4 px-4">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded bg-accent/12 text-accent">
            <HardHat size={18} />
          </div>
          <div className="leading-tight">
            <h1 className="text-[13px] font-semibold tracking-wide text-graphite-100 uppercase">
              Construction Safety Monitor
            </h1>
            <p className="text-[10.5px] text-graphite-500">
              Spatial worker tracking &amp; geofencing
            </p>
          </div>
        </div>

        <nav className="ml-4 flex items-center gap-1">
          {TABS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              onClick={() => onTab(id)}
              className={cx(
                "flex items-center gap-1.5 rounded px-3 py-1.5 text-xs font-medium transition",
                tab === id
                  ? "bg-graphite-800 text-graphite-100"
                  : "text-graphite-400 hover:bg-graphite-850 hover:text-graphite-200",
              )}
            >
              <Icon size={13} />
              {label}
            </button>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-3">
          {demoMode && (
            <span className="rounded border border-accent/40 bg-accent/10 px-2 py-1 text-[10px] font-bold tracking-widest text-accent uppercase">
              Demo Mode
            </span>
          )}

          <button
            type="button"
            onClick={onToggleDemo}
            disabled={demoBusy}
            className={cx(
              "flex items-center gap-1.5 rounded border px-2.5 py-1.5 text-xs font-medium transition disabled:opacity-50",
              demoMode
                ? "border-sev-high/40 bg-sev-high/10 text-sev-high hover:bg-sev-high/15"
                : "border-graphite-700 bg-graphite-850 text-graphite-300 hover:border-graphite-600 hover:text-graphite-100",
            )}
          >
            {demoBusy ? (
              <Loader2 size={13} className="animate-spin" />
            ) : demoMode ? (
              <Square size={13} />
            ) : (
              <Play size={13} />
            )}
            {demoMode ? "Stop Demo" : "Start Demo"}
          </button>

          <div className="flex items-center gap-4 border-l border-graphite-800 pl-4 text-[11px]">
            <Metric label="Camera" value={camera} />
            <Metric label="FPS" value={fps ? fps.toFixed(1) : "-"} />
            <ConnectionBadge status={status} />
          </div>
        </div>
      </div>
    </header>
  );
}

function Metric({ label, value }) {
  return (
    <div className="leading-tight">
      <div className="text-[9.5px] tracking-widest text-graphite-500 uppercase">
        {label}
      </div>
      <div className="tabular font-medium text-graphite-200">{value}</div>
    </div>
  );
}

function ConnectionBadge({ status }) {
  const map = {
    connected: {
      text: "Live",
      cls: "text-safe border-safe/35 bg-safe/10",
      icon: Radio,
      pulse: true,
    },
    connecting: {
      text: "Connecting",
      cls: "text-graphite-400 border-graphite-700 bg-graphite-850",
      icon: Loader2,
      spin: true,
    },
    reconnecting: {
      text: "Reconnecting",
      cls: "text-sev-medium border-sev-medium/35 bg-sev-medium/10",
      icon: Loader2,
      spin: true,
    },
    error: {
      text: "Offline",
      cls: "text-sev-critical border-sev-critical/35 bg-sev-critical/10",
      icon: WifiOff,
    },
  };
  const s = map[status] ?? map.error;
  const Icon = s.icon ?? Activity;
  return (
    <span
      className={cx(
        "flex items-center gap-1.5 rounded border px-2 py-1 text-[10px] font-semibold tracking-wider uppercase",
        s.cls,
      )}
    >
      <Icon size={11} className={cx(s.spin && "animate-spin", s.pulse && "pulse-dot")} />
      {s.text}
    </span>
  );
}
