import { useEffect, useMemo, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import WorkerTable from "./WorkerTable";
import { api } from "../services/api";
import { ALERT_TYPE_LABEL, cx, duration, severity } from "../lib/format";

const WINDOWS = [
  { hours: 1, label: "1h" },
  { hours: 8, label: "8h" },
  { hours: 24, label: "24h" },
  { hours: 168, label: "7d" },
];

export default function Analytics({ liveWorkers = [], onSelectWorker }) {
  const [data, setData] = useState(null);
  const [workers, setWorkers] = useState([]);
  const [hours, setHours] = useState(24);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = async (h = hours) => {
    setLoading(true);
    try {
      const [analytics, workerRows] = await Promise.all([
        api.analytics(h),
        api.workers(),
      ]);
      setData(analytics);
      setWorkers(workerRows);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(hours);
    const id = setInterval(() => load(hours), 10000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hours]);

  const totals = data?.totals ?? {};

  return (
    <div className="min-h-0 flex-1 overflow-y-auto bg-graphite-950">
      <div className="mx-auto max-w-[1400px] p-4">
        <div className="mb-3 flex items-center gap-3">
          <h2 className="text-[13px] font-semibold tracking-wide text-graphite-200 uppercase">
            Site Analytics
          </h2>
          <div className="ml-auto flex items-center gap-1 rounded border border-graphite-800 bg-graphite-900 p-0.5">
            {WINDOWS.map((w) => (
              <button
                key={w.hours}
                type="button"
                onClick={() => setHours(w.hours)}
                className={cx(
                  "rounded px-2 py-1 text-[11px] font-medium transition",
                  hours === w.hours
                    ? "bg-graphite-800 text-graphite-100"
                    : "text-graphite-400 hover:text-graphite-200",
                )}
              >
                {w.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={() => load(hours)}
            className="flex items-center gap-1.5 rounded border border-graphite-800 bg-graphite-900 px-2.5 py-1.5 text-[11px] text-graphite-300 transition hover:border-graphite-700 hover:text-graphite-100"
          >
            {loading ? (
              <Loader2 size={12} className="animate-spin" />
            ) : (
              <RefreshCw size={12} />
            )}
            Refresh
          </button>
        </div>

        {error && (
          <div className="mb-3 rounded border border-sev-critical/35 bg-sev-critical/10 px-3 py-2 text-xs text-sev-critical">
            {error}
          </div>
        )}

        <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-3 lg:grid-cols-6">
          <Metric label="Total Workers" value={totals.total_workers ?? 0} />
          <Metric label="Active Now" value={totals.active_workers ?? 0} tone="good" />
          <Metric
            label="Active Alerts"
            value={totals.active_alerts ?? 0}
            tone={totals.active_alerts ? "danger" : "neutral"}
          />
          <Metric
            label="Incidents Today"
            value={totals.incidents_today ?? 0}
            tone={totals.incidents_today ? "warn" : "neutral"}
          />
          <Metric
            label="Avg Time On Site"
            value={duration(totals.avg_time_on_site_seconds)}
          />
          <Metric label="Zones Configured" value={totals.zones_configured ?? 0} />
        </div>

        <div className="mb-3 grid grid-cols-1 gap-3 lg:grid-cols-3">
          <Panel title="Alerts by Type">
            <Breakdown
              data={data?.alerts_by_type}
              labeller={(k) => ALERT_TYPE_LABEL[k] ?? k}
            />
          </Panel>
          <Panel title="Alerts by Severity">
            <Breakdown
              data={data?.alerts_by_severity}
              colourFor={(k) => severity(k).hex}
            />
          </Panel>
          <Panel title="PPE Violations by Item">
            <Breakdown data={data?.alerts_by_ppe_item} />
          </Panel>
        </div>

        <div className="mb-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
          <Panel title={`Alert Timeline (last ${hours}h)`}>
            <Timeline points={data?.timeline ?? []} />
          </Panel>
          <Panel title="Zone Occupancy & Breaches">
            <ZoneTable rows={data?.zone_occupancy ?? []} />
          </Panel>
        </div>

        <Panel title="Worker Attendance" padded={false}>
          <WorkerTable
            rows={workers}
            liveWorkers={liveWorkers}
            onSelect={onSelectWorker}
          />
        </Panel>
      </div>
    </div>
  );
}

function Metric({ label, value, tone = "neutral" }) {
  const tones = {
    neutral: "text-graphite-100",
    good: "text-safe",
    warn: "text-sev-medium",
    danger: "text-sev-critical",
  };
  return (
    <div className="rounded border border-graphite-800 bg-graphite-900 px-3 py-2.5">
      <div className="text-[9.5px] tracking-widest text-graphite-500 uppercase">
        {label}
      </div>
      <div className={cx("mt-0.5 text-xl font-semibold tabular", tones[tone])}>
        {value}
      </div>
    </div>
  );
}

function Panel({ title, children, padded = true }) {
  return (
    <section className="overflow-hidden rounded border border-graphite-800 bg-graphite-900">
      <header className="border-b border-graphite-800 px-3 py-2">
        <h3 className="text-[10px] font-semibold tracking-widest text-graphite-400 uppercase">
          {title}
        </h3>
      </header>
      <div className={padded ? "p-3" : ""}>{children}</div>
    </section>
  );
}

function Breakdown({ data, labeller = (k) => k, colourFor }) {
  const entries = Object.entries(data ?? {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) {
    return <p className="text-[11px] text-graphite-600">Nothing recorded yet</p>;
  }
  const max = Math.max(...entries.map(([, v]) => v));
  return (
    <ul className="space-y-2">
      {entries.map(([key, value]) => (
        <li key={key}>
          <div className="mb-1 flex items-center justify-between text-[11px]">
            <span className="text-graphite-300">{labeller(key)}</span>
            <span className="text-graphite-400 tabular">{value}</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-graphite-850">
            <div
              className="h-full rounded-full transition-all"
              style={{
                width: `${Math.max(3, (value / max) * 100)}%`,
                background: colourFor ? colourFor(key) : "var(--color-accent)",
              }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}

function Timeline({ points }) {
  if (!points.length) {
    return <p className="text-[11px] text-graphite-600">No alerts in this window</p>;
  }
  const max = Math.max(...points.map((p) => p.count), 1);
  return (
    <div>
      <div className="flex h-28 items-end gap-1">
        {points.map((p) => (
          // h-full matters: the bar height is a percentage, and a percentage
          // height only resolves against a parent with a definite height.
          <div
            key={p.hour}
            className="group flex h-full flex-1 items-end"
            title={`${p.hour}: ${p.count}`}
          >
            <div
              className="w-full rounded-t bg-accent/70 transition group-hover:bg-accent"
              style={{ height: `${Math.max(4, (p.count / max) * 100)}%` }}
            />
          </div>
        ))}
      </div>
      <div className="mt-1.5 flex justify-between text-[10px] text-graphite-600">
        <span>{points[0]?.hour?.slice(11, 16)}</span>
        <span className="tabular">peak {max}</span>
        <span>{points[points.length - 1]?.hour?.slice(11, 16)}</span>
      </div>
    </div>
  );
}

function ZoneTable({ rows }) {
  if (!rows.length) {
    return <p className="text-[11px] text-graphite-600">No zones configured</p>;
  }
  return (
    <table className="w-full text-[11px]">
      <thead>
        <tr className="text-left text-[9.5px] tracking-widest text-graphite-500 uppercase">
          <th className="pb-1.5 font-medium">Zone</th>
          <th className="pb-1.5 font-medium">Severity</th>
          <th className="pb-1.5 text-right font-medium">Area</th>
          <th className="pb-1.5 text-right font-medium">Inside</th>
          <th className="pb-1.5 text-right font-medium">Breaches</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-graphite-850">
        {rows.map((z) => {
          const sev = severity(z.severity);
          return (
            <tr key={z.zone_id}>
              <td className="py-1.5 text-graphite-200">{z.name}</td>
              <td className="py-1.5">
                <span className={cx("rounded px-1.5 py-px text-[9.5px] font-bold uppercase", sev.bg, sev.text)}>
                  {z.severity}
                </span>
              </td>
              <td className="py-1.5 text-right text-graphite-400 tabular">
                {z.area_m2} m2
              </td>
              <td
                className={cx(
                  "py-1.5 text-right tabular",
                  z.occupancy ? "font-semibold text-sev-medium" : "text-graphite-500",
                )}
              >
                {z.occupancy}
              </td>
              <td className="py-1.5 text-right text-graphite-400 tabular">
                {z.breaches ?? 0}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
