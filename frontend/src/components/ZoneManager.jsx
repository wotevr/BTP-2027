import { useEffect, useState } from "react";
import {
  Loader2,
  Pencil,
  Plus,
  Power,
  RefreshCw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import SiteMap from "./SiteMap";
import { api } from "../services/api";
import { cx, severity, ZONE_TYPE_LABEL } from "../lib/format";

const ZONE_TYPES = [
  "HEAVY_MACHINERY",
  "OPEN_EDGE",
  "EXCAVATION",
  "RESTRICTED_AREA",
  "OTHER",
];
const SEVERITIES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];

const BLANK = {
  name: "",
  zone_type: "OTHER",
  severity: "MEDIUM",
  description: "",
  active: true,
  polygon: "[[10, 5], [20, 5], [20, 15], [10, 15]]",
};

/**
 * Danger zone management.
 *
 * Polygons are edited as JSON text rather than drawn with the mouse. That is a
 * deliberate scope decision: a click-to-draw editor is a lot of surface area
 * for very little extra capability at this stage, and the zones for a fixed
 * site are set once from surveyed measurements. The API is the same either
 * way, so a drawing tool can be added later without touching the backend.
 */
export default function ZoneManager({ site, sitePlan, workers = [] }) {
  const [zones, setZones] = useState([]);
  const [editing, setEditing] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      setZones(await api.zones());
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const startCreate = () => {
    setError(null);
    setEditing({ ...BLANK, __new: true });
  };

  const startEdit = (zone) => {
    setError(null);
    setEditing({
      ...zone,
      polygon: JSON.stringify(zone.polygon),
      __new: false,
    });
  };

  const save = async () => {
    if (!editing) return;
    let polygon;
    try {
      polygon = JSON.parse(editing.polygon);
    } catch {
      setError("Polygon must be valid JSON, e.g. [[10, 5], [20, 5], [20, 15]]");
      return;
    }

    setBusy(true);
    try {
      const body = {
        name: editing.name,
        zone_type: editing.zone_type,
        severity: editing.severity,
        description: editing.description,
        active: editing.active,
        polygon,
      };
      if (editing.__new) await api.createZone(body);
      else await api.updateZone(editing.zone_id, body);
      setEditing(null);
      setError(null);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const toggleActive = async (zone) => {
    setBusy(true);
    try {
      await api.updateZone(zone.zone_id, { active: !zone.active });
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (zone) => {
    if (!window.confirm(`Delete "${zone.name}"? This cannot be undone.`)) return;
    setBusy(true);
    try {
      await api.deleteZone(zone.zone_id);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  // Preview uses the same map component as the live view, so what you see here
  // is exactly what the control room will see.
  const preview = zones
    .filter((z) => z.active)
    .map((z) => ({ ...z, occupancy: 0 }));

  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex w-[420px] shrink-0 flex-col border-r border-graphite-800 bg-graphite-900">
        <div className="flex h-10 shrink-0 items-center gap-2 border-b border-graphite-800 px-3">
          <h2 className="text-[11px] font-semibold tracking-widest text-graphite-300 uppercase">
            Danger Zones
          </h2>
          <span className="rounded bg-graphite-800 px-1.5 py-0.5 text-[10px] text-graphite-400 tabular">
            {zones.length}
          </span>
          <button
            type="button"
            onClick={load}
            className="ml-auto rounded p-1 text-graphite-500 transition hover:text-graphite-200"
            title="Refresh"
          >
            {loading ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <RefreshCw size={13} />
            )}
          </button>
          <button
            type="button"
            onClick={startCreate}
            className="flex items-center gap-1 rounded border border-graphite-700 bg-graphite-850 px-2 py-1 text-[11px] text-graphite-200 transition hover:border-accent/40 hover:text-accent"
          >
            <Plus size={12} />
            New
          </button>
        </div>

        {error && (
          <div className="border-b border-sev-critical/25 bg-sev-critical/10 px-3 py-2 text-[11px] text-sev-critical">
            {error}
          </div>
        )}

        <div className="flex-1 overflow-y-auto">
          {editing && (
            <ZoneForm
              value={editing}
              onChange={setEditing}
              onSave={save}
              onCancel={() => {
                setEditing(null);
                setError(null);
              }}
              busy={busy}
            />
          )}

          <ul className="divide-y divide-graphite-850">
            {zones.map((zone) => {
              const sev = severity(zone.severity);
              return (
                <li key={zone.zone_id} className="px-3 py-2.5">
                  <div className="flex items-center gap-2">
                    <span className={cx("h-1.5 w-1.5 rounded-full", sev.dot)} />
                    <span
                      className={cx(
                        "text-xs font-semibold",
                        zone.active ? "text-graphite-100" : "text-graphite-500 line-through",
                      )}
                    >
                      {zone.name}
                    </span>
                    <span className={cx("rounded px-1.5 py-px text-[9px] font-bold uppercase", sev.bg, sev.text)}>
                      {zone.severity}
                    </span>
                    <div className="ml-auto flex items-center gap-0.5">
                      <IconButton onClick={() => toggleActive(zone)} title={zone.active ? "Disable" : "Enable"}>
                        <Power size={12} className={zone.active ? "text-safe" : "text-graphite-600"} />
                      </IconButton>
                      <IconButton onClick={() => startEdit(zone)} title="Edit">
                        <Pencil size={12} />
                      </IconButton>
                      <IconButton onClick={() => remove(zone)} title="Delete" danger>
                        <Trash2 size={12} />
                      </IconButton>
                    </div>
                  </div>
                  <p className="mt-1 text-[10.5px] text-graphite-500">
                    {ZONE_TYPE_LABEL[zone.zone_type] ?? zone.zone_type} &middot;{" "}
                    {zone.polygon.length} vertices &middot;{" "}
                    <span className="text-graphite-600">{zone.zone_id}</span>
                  </p>
                  {zone.description && (
                    <p className="mt-0.5 text-[10.5px] leading-snug text-graphite-600">
                      {zone.description}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>

          {!loading && !zones.length && !editing && (
            <p className="px-3 py-6 text-center text-[11px] text-graphite-600">
              No zones yet. Run <span className="text-graphite-400">seed_demo_data.py</span>{" "}
              or create one.
            </p>
          )}
        </div>
      </div>

      <div className="min-w-0 flex-1">
        <SiteMap
          site={site}
          zones={preview}
          workers={workers}
          sitePlan={sitePlan}
          onSelectWorker={() => {}}
        />
      </div>
    </div>
  );
}

function ZoneForm({ value, onChange, onSave, onCancel, busy }) {
  const set = (patch) => onChange({ ...value, ...patch });
  return (
    <div className="border-b border-graphite-800 bg-graphite-850 px-3 py-3">
      <div className="mb-2 flex items-center gap-2">
        <h3 className="text-[10px] font-semibold tracking-widest text-graphite-300 uppercase">
          {value.__new ? "New Zone" : `Edit ${value.zone_id}`}
        </h3>
        <button
          type="button"
          onClick={onCancel}
          className="ml-auto rounded p-0.5 text-graphite-500 hover:text-graphite-200"
        >
          <X size={13} />
        </button>
      </div>

      <div className="space-y-2">
        <Field label="Name">
          <input
            value={value.name}
            onChange={(e) => set({ name: e.target.value })}
            placeholder="Heavy Machinery Zone"
            className={inputCls}
          />
        </Field>

        <div className="grid grid-cols-2 gap-2">
          <Field label="Type">
            <select
              value={value.zone_type}
              onChange={(e) => set({ zone_type: e.target.value })}
              className={inputCls}
            >
              {ZONE_TYPES.map((t) => (
                <option key={t} value={t}>
                  {ZONE_TYPE_LABEL[t]}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Severity">
            <select
              value={value.severity}
              onChange={(e) => set({ severity: e.target.value })}
              className={inputCls}
            >
              {SEVERITIES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <Field label="Polygon - [[x, y], ...] in site metres">
          <textarea
            value={value.polygon}
            onChange={(e) => set({ polygon: e.target.value })}
            rows={3}
            spellCheck={false}
            className={cx(inputCls, "font-mono text-[10.5px] leading-relaxed")}
          />
        </Field>

        <Field label="Description">
          <input
            value={value.description ?? ""}
            onChange={(e) => set({ description: e.target.value })}
            placeholder="Crane slewing radius, spotter required"
            className={inputCls}
          />
        </Field>

        <label className="flex items-center gap-2 text-[11px] text-graphite-300">
          <input
            type="checkbox"
            checked={value.active}
            onChange={(e) => set({ active: e.target.checked })}
            className="accent-accent"
          />
          Active (geofenced)
        </label>

        <div className="flex gap-2 pt-1">
          <button
            type="button"
            onClick={onSave}
            disabled={busy || !value.name.trim()}
            className="flex items-center gap-1.5 rounded bg-accent/15 px-3 py-1.5 text-[11px] font-medium text-accent transition hover:bg-accent/25 disabled:opacity-40"
          >
            {busy ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
            Save
          </button>
          <button
            type="button"
            onClick={onCancel}
            className="rounded border border-graphite-700 px-3 py-1.5 text-[11px] text-graphite-400 transition hover:text-graphite-200"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

const inputCls =
  "w-full rounded border border-graphite-700 bg-graphite-900 px-2 py-1.5 text-[11.5px] text-graphite-100 outline-none transition focus:border-accent/60";

function Field({ label, children }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[9.5px] tracking-widest text-graphite-500 uppercase">
        {label}
      </span>
      {children}
    </label>
  );
}

function IconButton({ children, danger, ...props }) {
  return (
    <button
      type="button"
      className={cx(
        "rounded p-1 text-graphite-500 transition hover:bg-graphite-800",
        danger ? "hover:text-sev-critical" : "hover:text-graphite-200",
      )}
      {...props}
    >
      {children}
    </button>
  );
}
