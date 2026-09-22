import { useCallback, useEffect, useState } from "react";
import { Loader2, ServerCrash, TriangleAlert } from "lucide-react";
import Analytics from "./components/Analytics";
import Dashboard from "./components/Dashboard";
import Header from "./components/Header";
import ZoneManager from "./components/ZoneManager";
import { useWebSocket } from "./hooks/useWebSocket";
import { api } from "./services/api";

export default function App() {
  const { state, status } = useWebSocket();
  const [tab, setTab] = useState("live");
  const [selectedWorkerId, setSelectedWorkerId] = useState(null);
  const [analytics, setAnalytics] = useState(null);
  const [sitePlan, setSitePlan] = useState(null);
  const [demoBusy, setDemoBusy] = useState(false);
  const [notice, setNotice] = useState(null);

  const demoMode = state?.demo_mode ?? false;

  // Attendance totals live in the database, not in the frame payload, so they
  // are polled rather than pushed.
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .analytics(24)
        .then((d) => !cancelled && setAnalytics(d))
        .catch(() => {});
    load();
    const id = setInterval(load, 10000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Static site furniture for the map background. Optional: the map renders
  // perfectly well without it.
  useEffect(() => {
    api
      .sitePlan()
      .then((plan) => setSitePlan(plan?.available ? plan : null))
      .catch(() => setSitePlan(null));
  }, []);

  const toggleDemo = useCallback(async () => {
    setDemoBusy(true);
    setNotice(null);
    try {
      if (demoMode) {
        await api.stopDemo();
      } else {
        const result = await api.startDemo();
        if (result?.synthetic_calibration) {
          setNotice(
            result.note ??
              "Demo mode installed a synthetic camera calibration. Coordinates are illustrative until a real camera is calibrated.",
          );
        }
      }
    } catch (err) {
      setNotice(err.message);
    } finally {
      setDemoBusy(false);
    }
  }, [demoMode]);

  const selectWorker = useCallback((id) => {
    setSelectedWorkerId(id);
    if (id != null) setTab("live");
  }, []);

  const disconnected = status === "error" || status === "reconnecting";
  const waiting = !state && status !== "error";

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-graphite-950">
      <Header
        tab={tab}
        onTab={setTab}
        status={status}
        state={state}
        demoMode={demoMode}
        demoBusy={demoBusy}
        onToggleDemo={toggleDemo}
      />

      {notice && (
        <div className="flex shrink-0 items-center gap-2 border-b border-sev-medium/25 bg-sev-medium/10 px-4 py-2 text-[11.5px] text-sev-medium">
          <TriangleAlert size={13} className="shrink-0" />
          <span className="flex-1">{notice}</span>
          <button
            type="button"
            onClick={() => setNotice(null)}
            className="text-sev-medium/70 transition hover:text-sev-medium"
          >
            Dismiss
          </button>
        </div>
      )}

      {disconnected && (
        <div className="flex shrink-0 items-center gap-2 border-b border-sev-critical/25 bg-sev-critical/10 px-4 py-1.5 text-[11.5px] text-sev-critical">
          <ServerCrash size={13} />
          Lost connection to the backend. Retrying automatically.
        </div>
      )}

      {waiting ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 text-graphite-500">
          <Loader2 size={22} className="animate-spin" />
          <p className="text-xs">Connecting to the safety monitor...</p>
        </div>
      ) : tab === "live" ? (
        <Dashboard
          state={state}
          sitePlan={sitePlan}
          analytics={analytics}
          selectedWorkerId={selectedWorkerId}
          onSelectWorker={selectWorker}
        />
      ) : tab === "analytics" ? (
        <Analytics
          liveWorkers={state?.workers ?? []}
          onSelectWorker={selectWorker}
        />
      ) : (
        <ZoneManager
          site={state?.site}
          sitePlan={sitePlan}
          workers={state?.workers ?? []}
        />
      )}
    </div>
  );
}
