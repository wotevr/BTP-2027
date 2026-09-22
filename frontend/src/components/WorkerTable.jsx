import { useMemo, useState } from "react";
import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";
import { clockTime, cx, duration, relativeTime, workerState } from "../lib/format";

const COLUMNS = [
  { key: "worker_id", label: "Worker", align: "left" },
  { key: "first_seen", label: "First Seen", align: "left" },
  { key: "last_seen", label: "Last Seen", align: "left" },
  { key: "total_time_seconds", label: "Time On Site", align: "right" },
  { key: "location", label: "Location", align: "right", sortable: false },
  { key: "ppe", label: "PPE", align: "left", sortable: false },
  { key: "incident_count", label: "Incidents", align: "right" },
  { key: "current_status", label: "Status", align: "left" },
];

export default function WorkerTable({ rows = [], liveWorkers = [], onSelect }) {
  const [sort, setSort] = useState({ key: "last_seen", dir: "desc" });

  const live = useMemo(
    () => new Map(liveWorkers.map((w) => [w.id, w])),
    [liveWorkers],
  );

  const sorted = useMemo(() => {
    const out = [...rows];
    out.sort((a, b) => {
      const av = a[sort.key];
      const bv = b[sort.key];
      let cmp;
      if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
      else cmp = String(av ?? "").localeCompare(String(bv ?? ""));
      return sort.dir === "asc" ? cmp : -cmp;
    });
    return out;
  }, [rows, sort]);

  const toggle = (key) =>
    setSort((s) =>
      s.key === key
        ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
        : { key, dir: "desc" },
    );

  if (!rows.length) {
    return (
      <p className="px-3 py-6 text-center text-[11px] text-graphite-600">
        No workers recorded yet. Start demo mode or connect the vision pipeline.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11.5px]">
        <thead className="sticky top-0 bg-graphite-900">
          <tr className="border-b border-graphite-800">
            {COLUMNS.map((col) => (
              <th
                key={col.key}
                className={cx(
                  "px-3 py-2 text-[9.5px] font-medium tracking-widest text-graphite-500 uppercase",
                  col.align === "right" ? "text-right" : "text-left",
                )}
              >
                {col.sortable === false ? (
                  col.label
                ) : (
                  <button
                    type="button"
                    onClick={() => toggle(col.key)}
                    className={cx(
                      "inline-flex items-center gap-1 transition hover:text-graphite-300",
                      sort.key === col.key && "text-graphite-300",
                    )}
                  >
                    {col.label}
                    {sort.key === col.key ? (
                      sort.dir === "asc" ? (
                        <ArrowUp size={10} />
                      ) : (
                        <ArrowDown size={10} />
                      )
                    ) : (
                      <ChevronsUpDown size={10} className="opacity-40" />
                    )}
                  </button>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-graphite-850">
          {sorted.map((w) => {
            const liveRow = live.get(w.worker_id);
            const st = workerState(liveRow?.state);
            const onSite = w.current_status === "ON_SITE";
            return (
              <tr
                key={w.worker_id}
                onClick={() => onSelect?.(w.worker_id)}
                className="cursor-pointer transition hover:bg-graphite-850"
              >
                <td className="px-3 py-2">
                  <div className="flex items-center gap-2">
                    <span
                      className="h-1.5 w-1.5 rounded-full"
                      style={{ background: liveRow ? st.hex : "#4a5464" }}
                    />
                    <span className="font-semibold text-graphite-100 tabular">
                      #{w.worker_id}
                    </span>
                  </div>
                </td>
                <td className="px-3 py-2 text-graphite-400 tabular">
                  {clockTime(w.first_seen)}
                </td>
                <td className="px-3 py-2 text-graphite-400">
                  {relativeTime(w.last_seen)}
                </td>
                <td className="px-3 py-2 text-right text-graphite-200 tabular">
                  {duration(w.total_time_seconds)}
                </td>
                <td className="px-3 py-2 text-right text-graphite-400 tabular">
                  {w.last_world_x != null
                    ? `${w.last_world_x.toFixed(1)}, ${w.last_world_y.toFixed(1)} m`
                    : "-"}
                </td>
                <td className="px-3 py-2">
                  {liveRow?.ppe_labels ? (
                    <div className="flex gap-1">
                      {Object.entries(liveRow.ppe_labels).map(([item, label]) => (
                        <span
                          key={item}
                          title={`${item}: ${label}`}
                          className={cx(
                            "rounded px-1 py-px text-[9px] font-bold uppercase",
                            label === "OK"
                              ? "bg-safe/12 text-safe"
                              : label === "MISSING"
                                ? "bg-sev-critical/12 text-sev-critical"
                                : "bg-graphite-800 text-graphite-600",
                          )}
                        >
                          {item.slice(0, 1)}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <span className="text-graphite-600">-</span>
                  )}
                </td>
                <td
                  className={cx(
                    "px-3 py-2 text-right tabular",
                    w.incident_count ? "font-semibold text-sev-medium" : "text-graphite-500",
                  )}
                >
                  {w.incident_count}
                </td>
                <td className="px-3 py-2">
                  <span
                    className={cx(
                      "rounded px-1.5 py-0.5 text-[9.5px] font-semibold tracking-wider uppercase",
                      onSite
                        ? "bg-safe/12 text-safe"
                        : "bg-graphite-800 text-graphite-500",
                    )}
                  >
                    {onSite ? "On Site" : "Off Site"}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
