import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Clock,
  Layers,
  ShieldAlert,
  ShieldCheck,
  TriangleAlert,
  Users,
} from "lucide-react";
import AlertFeed from "./AlertFeed";
import SiteMap from "./SiteMap";
import WorkerDetail from "./WorkerDetail";
import { api } from "../services/api";
import { cx, duration } from "../lib/format";

/**
 * The primary screen: a live site monitor.
 *
 * The map is the main element. Everything else is arranged around it: alerts
 * on the right, the headline numbers on a single strip along the bottom.
 */
export default function Dashboard({
  state,
  sitePlan,
  analytics,
  onSelectWorker,
  selectedWorkerId,
}) {
  const [trail, setTrail] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailError, setDetailError] = useState(null);

  const workers = state?.workers ?? [];
  const alerts = state?.alerts ?? [];
  const zones = state?.zones ?? [];
  const calibrated = state?.calibration?.available ?? false;

  // Load the movement history and detail panel for the selected worker.
  useEffect(() => {
    if (selectedWorkerId == null) {
      setTrail(null);
      setDetail(null);
      setDetailError(null);
      return;
    }
    let cancelled = false;
    setDetailError(null);

    api
      .worker(selectedWorkerId)
      .then((data) => {
        if (cancelled) return;
        setDetail(data);
        const path = (data.trajectory ?? [])
          .filter((p) => p.world_x != null && p.world_y != null)
          .map((p) => [p.world_x, p.world_y]);
        setTrail(path.length > 1 ? path : null);
      })
      .catch((err) => {
        if (!cancelled) setDetailError(err.message);
      });

    return () => {
      cancelled = true;
    };
  }, [selectedWorkerId, state?.frames_processed]);

  const activeWorkers = workers.length;
  const workersInZones = workers.filter((w) => w.zones?.length).length;
  // Attendance totals are database state, not frame state, so they come from
  // the analytics endpoint rather than the live WebSocket payload.
  const avgTime = analytics?.totals?.avg_time_on_site_seconds ?? null;
  const incidentsToday = analytics?.totals?.incidents_today ?? 0;

  const handleResolve = async (eventId) => {
    try {
      await api.resolveAlert(eventId);
    } catch {
      /* the next broadcast will re-sync regardless */
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex min-h-0 flex-1">
        <div className="relative flex min-w-0 flex-1 flex-col">
          <SiteMap
            site={state?.site}
            zones={zones}
            workers={workers}
            sitePlan={sitePlan}
            trail={trail}
            selectedWorkerId={selectedWorkerId}
            onSelectWorker={onSelectWorker}
            calibrated={calibrated}
          />

          {selectedWorkerId != null && (
            <WorkerDetail
              workerId={selectedWorkerId}
              detail={detail}
              error={detailError}
              live={workers.find((w) => w.id === selectedWorkerId)}
              onClose={() => onSelectWorker(null)}
            />
          )}
        </div>

        <AlertFeed
          alerts={alerts}
          onSelectWorker={onSelectWorker}
          onResolve={handleResolve}
        />
      </div>

      <div className="grid shrink-0 grid-cols-2 gap-px border-t border-graphite-800 bg-graphite-800 sm:grid-cols-3 lg:grid-cols-6">
        <Stat
          icon={Users}
          label="Workers On Site"
          value={activeWorkers}
          tone="neutral"
        />
        <Stat
          icon={AlertTriangle}
          label="Active Alerts"
          value={alerts.length}
          tone={alerts.length ? "danger" : "good"}
        />
        <Stat
          icon={Layers}
          label="In Danger Zones"
          value={workersInZones}
          tone={workersInZones ? "warn" : "good"}
        />
        <Stat
          icon={ShieldAlert}
          label="Incidents Today"
          value={incidentsToday}
          tone={incidentsToday ? "warn" : "good"}
        />
        <Stat
          icon={Clock}
          label="Avg Time On Site"
          value={avgTime == null ? "-" : duration(avgTime)}
          tone="neutral"
        />
        <Stat
          icon={calibrated ? ShieldCheck : TriangleAlert}
          label="Spatial Calibration"
          value={calibrated ? "Active" : "Unavailable"}
          tone={calibrated ? "good" : "warn"}
          hint={
            calibrated
              ? state?.calibration?.reprojection_error_px != null
                ? `${state.calibration.reprojection_error_px.toFixed(2)} px error`
                : undefined
              : "run calibrate_homography.py"
          }
        />
      </div>
    </div>
  );
}

function Stat({ icon: Icon, label, value, tone = "neutral", hint }) {
  const tones = {
    neutral: "text-graphite-200",
    good: "text-safe",
    warn: "text-sev-medium",
    danger: "text-sev-critical",
  };
  return (
    <div className="flex items-center gap-3 bg-graphite-900 px-4 py-2.5">
      <Icon size={16} className="shrink-0 text-graphite-600" />
      <div className="min-w-0 leading-tight">
        <div className="truncate text-[9.5px] tracking-widest text-graphite-500 uppercase">
          {label}
        </div>
        <div className={cx("truncate text-lg font-semibold tabular", tones[tone])}>
          {value}
        </div>
        {hint && (
          <div className="truncate text-[9.5px] text-graphite-600">{hint}</div>
        )}
      </div>
    </div>
  );
}
