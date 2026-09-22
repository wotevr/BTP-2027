import { Check, Minus, Route, X } from "lucide-react";
import {
  ALERT_TYPE_LABEL,
  clockTime,
  cx,
  duration,
  ppeSummary,
  relativeTime,
  severity,
  workerState,
} from "../lib/format";

/**
 * Floating detail card for one worker.
 *
 * Overlays the map rather than pushing it aside, so the movement trail stays
 * visible next to the numbers that describe it.
 */
export default function WorkerDetail({ workerId, detail, live, error, onClose }) {
  const st = workerState(live?.state);
  const worker = detail?.worker;
  const trajectory = detail?.trajectory ?? [];
  const withFix = trajectory.filter((p) => p.world_x != null);

  return (
    <div className="absolute bottom-3 left-3 z-10 w-[310px] rounded-lg border border-graphite-700 bg-graphite-900/97 shadow-2xl backdrop-blur">
      <div className="flex items-center gap-2 border-b border-graphite-800 px-3 py-2">
        <span className="h-2 w-2 rounded-full" style={{ background: st.hex }} />
        <h3 className="text-sm font-semibold text-graphite-100 tabular">
          Worker #{workerId}
        </h3>
        <span className={cx("text-[10px] font-semibold tracking-wider uppercase", st.text)}>
          {st.label}
        </span>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto rounded p-1 text-graphite-500 transition hover:bg-graphite-800 hover:text-graphite-200"
          title="Close"
        >
          <X size={13} />
        </button>
      </div>

      {error && (
        <p className="px-3 py-3 text-[11px] text-sev-medium">{error}</p>
      )}

      <div className="max-h-[52vh] overflow-y-auto">
        <section className="grid grid-cols-2 gap-x-3 gap-y-2 px-3 py-2.5">
          <Field
            label="Position"
            value={
              live?.world_position
                ? `${live.world_position.x.toFixed(1)} , ${live.world_position.y.toFixed(1)} m`
                : "no spatial fix"
            }
          />
          <Field label="Time on site" value={duration(worker?.total_time_seconds)} />
          <Field label="Incidents" value={worker?.incident_count ?? 0} />
          <Field label="Status" value={worker?.current_status ?? "-"} />
          <Field label="First seen" value={clockTime(worker?.first_seen)} />
          <Field label="Last seen" value={relativeTime(live?.last_seen ?? worker?.last_seen)} />
        </section>

        <section className="border-t border-graphite-850 px-3 py-2.5">
          <SectionLabel>PPE</SectionLabel>
          <ul className="mt-1.5 space-y-1">
            {ppeSummary(live?.ppe).map(({ item, label, status }) => (
              <li key={item} className="flex items-center gap-2 text-[11.5px]">
                {status === "OK" ? (
                  <Check size={12} className="text-safe" />
                ) : status === "MISSING" ? (
                  <X size={12} className="text-sev-critical" />
                ) : (
                  <Minus size={12} className="text-graphite-600" />
                )}
                <span className="text-graphite-300">{label}</span>
                <span
                  className={cx(
                    "ml-auto text-[10px] font-semibold tracking-wider uppercase",
                    status === "OK"
                      ? "text-safe"
                      : status === "MISSING"
                        ? "text-sev-critical"
                        : "text-graphite-600",
                  )}
                >
                  {status === "UNKNOWN" ? "not observed" : status}
                </span>
              </li>
            ))}
            {!live?.ppe && (
              <li className="text-[11px] text-graphite-600">No PPE data</li>
            )}
          </ul>
        </section>

        {live?.zones?.length > 0 && (
          <section className="border-t border-graphite-850 px-3 py-2.5">
            <SectionLabel>Currently inside</SectionLabel>
            <div className="mt-1.5 flex flex-wrap gap-1">
              {live.zones.map((z) => (
                <span
                  key={z}
                  className="rounded border border-sev-critical/35 bg-sev-critical/10 px-1.5 py-0.5 text-[10px] text-sev-critical"
                >
                  {z}
                </span>
              ))}
            </div>
          </section>
        )}

        <section className="border-t border-graphite-850 px-3 py-2.5">
          <div className="flex items-center gap-1.5">
            <Route size={11} className="text-graphite-500" />
            <SectionLabel>Movement history</SectionLabel>
          </div>
          <p className="mt-1 text-[11px] text-graphite-500">
            {withFix.length > 1 ? (
              <>
                <span className="text-graphite-300 tabular">{withFix.length}</span>{" "}
                stored positions, drawn on the map
              </>
            ) : (
              "Not enough stored positions yet"
            )}
          </p>
        </section>

        <section className="border-t border-graphite-850 px-3 py-2.5">
          <SectionLabel>Recent alerts</SectionLabel>
          {detail?.recent_alerts?.length ? (
            <ul className="mt-1.5 space-y-1.5">
              {detail.recent_alerts.slice(0, 6).map((a) => {
                const sev = severity(a.severity);
                return (
                  <li key={a.event_id} className="flex items-start gap-2">
                    <span className={cx("mt-1 h-1.5 w-1.5 shrink-0 rounded-full", sev.dot)} />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-[11px] text-graphite-300">
                        {ALERT_TYPE_LABEL[a.alert_type] ?? a.alert_type}
                        {a.zone_id ? ` - ${a.zone_id}` : ""}
                        {a.ppe_item ? ` - ${a.ppe_item}` : ""}
                      </p>
                      <p className="text-[10px] text-graphite-600">
                        {relativeTime(a.timestamp)} &middot; {a.status}
                      </p>
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="mt-1 text-[11px] text-graphite-600">No alerts recorded</p>
          )}
        </section>
      </div>
    </div>
  );
}

function Field({ label, value }) {
  return (
    <div className="leading-tight">
      <div className="text-[9.5px] tracking-widest text-graphite-500 uppercase">
        {label}
      </div>
      <div className="text-[12px] text-graphite-200 tabular">{value}</div>
    </div>
  );
}

function SectionLabel({ children }) {
  return (
    <span className="text-[9.5px] font-semibold tracking-widest text-graphite-400 uppercase">
      {children}
    </span>
  );
}
