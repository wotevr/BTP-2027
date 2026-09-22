import { useEffect, useMemo, useState } from "react";
import { BellOff, Check, ShieldAlert } from "lucide-react";
import {
  ALERT_TYPE_LABEL,
  cx,
  metres,
  relativeTime,
  severity,
  SEVERITY_ORDER,
} from "../lib/format";

/**
 * The live alert panel.
 *
 * Shows only ONGOING conditions (zone breaches, PPE violations). Momentary
 * facts such as "entered zone" belong to the audit trail, not to a list of
 * things somebody needs to act on.
 */
export default function AlertFeed({ alerts = [], onSelectWorker, onResolve }) {
  // Re-render once a second so the relative timestamps stay honest even when
  // no new state has arrived.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const sorted = useMemo(() => {
    const rank = (a) => SEVERITY_ORDER.indexOf(a.severity);
    return [...alerts].sort((a, b) => {
      const bySeverity = rank(b) - rank(a);
      if (bySeverity !== 0) return bySeverity;
      return new Date(b.timestamp) - new Date(a.timestamp);
    });
  }, [alerts]);

  return (
    <aside className="flex h-full w-[330px] shrink-0 flex-col border-l border-graphite-800 bg-graphite-900">
      <div className="flex h-10 shrink-0 items-center gap-2 border-b border-graphite-800 px-3">
        <ShieldAlert size={14} className="text-graphite-400" />
        <h2 className="text-[11px] font-semibold tracking-widest text-graphite-300 uppercase">
          Live Alerts
        </h2>
        <span
          className={cx(
            "ml-auto rounded px-1.5 py-0.5 text-[10px] font-bold tabular",
            sorted.length
              ? "bg-sev-critical/15 text-sev-critical"
              : "bg-graphite-800 text-graphite-500",
          )}
        >
          {sorted.length}
        </span>
      </div>

      <div className="flex-1 overflow-y-auto">
        {sorted.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
            <BellOff size={22} className="text-graphite-700" />
            <p className="text-xs text-graphite-500">No active alerts</p>
            <p className="text-[11px] leading-relaxed text-graphite-600">
              Zone breaches and PPE violations appear here the moment they are
              raised.
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-graphite-850">
            {sorted.map((alert) => (
              <AlertRow
                key={alert.event_id}
                alert={alert}
                onSelectWorker={onSelectWorker}
                onResolve={onResolve}
              />
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}

function AlertRow({ alert, onSelectWorker, onResolve }) {
  const sev = severity(alert.severity);
  const hasFix = alert.location?.x != null;

  return (
    <li className="alert-in">
      <div
        role="button"
        tabIndex={0}
        onClick={() => onSelectWorker?.(alert.worker_id)}
        onKeyDown={(e) => e.key === "Enter" && onSelectWorker?.(alert.worker_id)}
        className="group w-full cursor-pointer px-3 py-2.5 text-left transition hover:bg-graphite-850"
      >
        <div className="flex items-center gap-2">
          <span className={cx("h-1.5 w-1.5 shrink-0 rounded-full", sev.dot)} />
          <span
            className={cx(
              "rounded px-1.5 py-px text-[9.5px] font-bold tracking-wider uppercase",
              sev.bg,
              sev.text,
            )}
          >
            {alert.severity}
          </span>
          <span className="text-xs font-semibold text-graphite-100 tabular">
            Worker #{alert.worker_id}
          </span>
          <span className="ml-auto text-[10px] text-graphite-500">
            {relativeTime(alert.timestamp)}
          </span>
        </div>

        <p className="mt-1.5 pl-3.5 text-[11.5px] leading-snug text-graphite-300">
          {alert.message}
        </p>

        <div className="mt-1 flex items-center gap-2 pl-3.5">
          <span className="text-[10px] tracking-wide text-graphite-500 uppercase">
            {ALERT_TYPE_LABEL[alert.event_type] ?? alert.event_type}
          </span>
          <span className="text-[10px] text-graphite-600 tabular">
            {hasFix ? metres(alert.location) : "no spatial fix"}
          </span>

          {onResolve && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onResolve(alert.event_id);
              }}
              title="Acknowledge and close this alert"
              className="ml-auto flex items-center gap-1 rounded border border-graphite-700 px-1.5 py-0.5 text-[10px] text-graphite-400 opacity-0 transition group-hover:opacity-100 hover:border-safe/40 hover:text-safe"
            >
              <Check size={10} />
              Ack
            </button>
          )}
        </div>
      </div>
    </li>
  );
}
