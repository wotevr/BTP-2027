/**
 * Thin REST client for the BTP-2027 backend.
 *
 * In development Vite proxies /api and /ws to localhost:8000 (see
 * vite.config.js), so the browser only ever talks to one origin. Set
 * VITE_API_BASE to point at a backend somewhere else.
 */
const BASE = import.meta.env.VITE_API_BASE ?? "";
const API = `${BASE}/api/v1`;

class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(`${API}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (cause) {
    // Network-level failure: the backend is not running or unreachable.
    throw new ApiError(
      "Cannot reach the backend. Is it running on port 8000?",
      0,
      null,
    );
  }

  if (!response.ok) {
    let body = null;
    let detail = `${response.status} ${response.statusText}`;
    try {
      body = await response.json();
      if (body?.detail) {
        detail =
          typeof body.detail === "string"
            ? body.detail
            : JSON.stringify(body.detail);
      }
    } catch {
      /* response had no JSON body; keep the status text */
    }
    throw new ApiError(detail, response.status, body);
  }

  if (response.status === 204) return null;
  return response.json();
}

export const api = {
  // live state
  state: () => request("/state"),
  systemStatus: () => request("/system/status"),

  // workers
  workers: (params = "") => request(`/workers${params}`),
  worker: (id) => request(`/workers/${id}`),
  trajectory: (id, minutes = 60) =>
    request(`/workers/${id}/trajectory?minutes=${minutes}`),

  // alerts
  alerts: (query = "") => request(`/alerts${query}`),
  activeAlerts: () => request("/alerts/active"),
  resolveAlert: (eventId) =>
    request(`/alerts/${eventId}/resolve`, { method: "POST" }),

  // zones
  zones: () => request("/zones"),
  sitePlan: () => request("/site-plan"),

  // camera
  cameraStatus: () => request("/camera/status"),
  createZone: (zone) =>
    request("/zones", { method: "POST", body: JSON.stringify(zone) }),
  updateZone: (id, patch) =>
    request(`/zones/${id}`, { method: "PUT", body: JSON.stringify(patch) }),
  deleteZone: (id) => request(`/zones/${id}`, { method: "DELETE" }),

  // analytics
  analytics: (hours = 24) => request(`/analytics?hours=${hours}`),

  // calibration
  calibration: () => request("/calibration"),
  reloadCalibration: () => request("/calibration/reload", { method: "POST" }),

  // demo
  startDemo: () => request("/demo/start", { method: "POST" }),
  stopDemo: () => request("/demo/stop", { method: "POST" }),
  resetState: () => request("/demo/reset", { method: "POST" }),
};

export { ApiError };

export function wsUrl() {
  if (import.meta.env.VITE_WS_URL) return import.meta.env.VITE_WS_URL;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws/live`;
}
