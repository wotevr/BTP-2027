import { useCallback, useEffect, useRef, useState } from "react";
import { Crosshair, Maximize2, Minus, Plus, TriangleAlert } from "lucide-react";
import { severity, workerState, ZONE_TYPE_LABEL } from "../lib/format";

/**
 * The 2D site plan.
 *
 * This is NOT a geographic map, and deliberately not Google Maps. The
 * coordinate system is local site metres produced by the homography:
 *
 *     X = metres along the site width
 *     Y = metres along the site depth
 *
 * SVG puts Y downward and the site plan wants Y upward, so world (x, y) is
 * drawn at (x, depth - y). Doing the flip in one helper keeps every polygon,
 * marker and trail consistent.
 */
const PAD = 2.5; // metres of margin around the site
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 8;

export default function SiteMap({
  site,
  zones = [],
  workers = [],
  sitePlan = null,
  trail = null,
  selectedWorkerId = null,
  onSelectWorker = () => {},
  calibrated = true,
}) {
  const width = site?.width_m ?? 30;
  const depth = site?.depth_m ?? 20;

  const svgRef = useRef(null);
  const [view, setView] = useState({ k: 1, x: 0, y: 0 });
  const [hovered, setHovered] = useState(null);
  const dragRef = useRef(null);

  const toY = useCallback((y) => depth - y, [depth]);

  const reset = useCallback(() => setView({ k: 1, x: 0, y: 0 }), []);

  // Recentre when the site dimensions change under us.
  useEffect(() => {
    reset();
  }, [width, depth, reset]);

  /** Pointer position in viewBox units. */
  const toLocal = (evt) => {
    const svg = svgRef.current;
    if (!svg) return { x: 0, y: 0 };
    const ctm = svg.getScreenCTM();
    if (!ctm) return { x: 0, y: 0 };
    const pt = svg.createSVGPoint();
    pt.x = evt.clientX;
    pt.y = evt.clientY;
    return pt.matrixTransform(ctm.inverse());
  };

  const zoomAbout = (px, py, factor) => {
    setView((v) => {
      const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v.k * factor));
      if (k === v.k) return v;
      // Keep the point under the cursor fixed while scaling.
      const ratio = k / v.k;
      return { k, x: px - (px - v.x) * ratio, y: py - (py - v.y) * ratio };
    });
  };

  const onWheel = (evt) => {
    evt.preventDefault();
    const p = toLocal(evt);
    zoomAbout(p.x, p.y, evt.deltaY < 0 ? 1.15 : 1 / 1.15);
  };

  const zoomCentre = (factor) => zoomAbout(width / 2, depth / 2, factor);

  const onPointerDown = (evt) => {
    if (evt.button !== 0) return;
    const p = toLocal(evt);
    dragRef.current = { px: p.x, py: p.y, x: view.x, y: view.y, moved: false };
    evt.currentTarget.setPointerCapture(evt.pointerId);
  };

  const onPointerMove = (evt) => {
    const drag = dragRef.current;
    if (!drag) return;
    const p = toLocal(evt);
    const dx = p.x - drag.px;
    const dy = p.y - drag.py;
    if (Math.abs(dx) > 0.15 || Math.abs(dy) > 0.15) drag.moved = true;
    setView((v) => ({ ...v, x: drag.x + dx, y: drag.y + dy }));
  };

  const onPointerUp = (evt) => {
    const drag = dragRef.current;
    dragRef.current = null;
    try {
      evt.currentTarget.releasePointerCapture(evt.pointerId);
    } catch {
      /* pointer already released */
    }
    // A drag that did not move is a click on the background: clear selection.
    if (drag && !drag.moved) onSelectWorker(null);
  };

  const viewBox = `${-PAD} ${-PAD} ${width + PAD * 2} ${depth + PAD * 2}`;
  const s = (n) => n / view.k; // keep strokes and text visually constant

  const gridLines = [];
  for (let x = 0; x <= width; x += 5) gridLines.push({ x1: x, y1: 0, x2: x, y2: depth });
  for (let y = 0; y <= depth; y += 5) gridLines.push({ x1: 0, y1: y, x2: width, y2: y });

  return (
    <div className="relative h-full w-full overflow-hidden bg-graphite-950">
      <svg
        ref={svgRef}
        viewBox={viewBox}
        preserveAspectRatio="xMidYMid meet"
        className="h-full w-full cursor-grab touch-none active:cursor-grabbing"
        onWheel={onWheel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerUp}
      >
        <defs>
          <pattern id="hatch" width="1.2" height="1.2" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="1.2" stroke="currentColor" strokeWidth="0.35" />
          </pattern>
        </defs>

        <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
          {/* ground */}
          <rect
            x={0}
            y={0}
            width={width}
            height={depth}
            fill="#0d1117"
            stroke="#262c36"
            strokeWidth={s(0.12)}
          />

          {/* 5 m grid */}
          <g stroke="#1b2027" strokeWidth={s(0.05)}>
            {gridLines.map((l, i) => (
              <line key={i} x1={l.x1} y1={toY(l.y1)} x2={l.x2} y2={toY(l.y2)} />
            ))}
          </g>

          {/* static site furniture - cosmetic only, never used in any calculation */}
          {sitePlan?.features?.map((f, i) => (
            <g key={`f-${i}`}>
              <polygon
                points={f.polygon.map(([x, y]) => `${x},${toY(y)}`).join(" ")}
                fill="#171c24"
                stroke="#2c333d"
                strokeWidth={s(0.08)}
              />
              {f.label && view.k > 0.8 && (
                <text
                  x={centroid(f.polygon)[0]}
                  y={toY(centroid(f.polygon)[1])}
                  fill="#4a5464"
                  fontSize={s(0.75)}
                  textAnchor="middle"
                  dominantBaseline="middle"
                  className="pointer-events-none select-none"
                >
                  {f.label}
                </text>
              )}
            </g>
          ))}

          {/* danger zones */}
          {zones.map((zone) => {
            const sev = severity(zone.severity);
            const occupied = (zone.occupancy ?? 0) > 0;
            const pts = zone.polygon.map(([x, y]) => `${x},${toY(y)}`).join(" ");
            const [cxm, cym] = centroid(zone.polygon);
            return (
              <g key={zone.zone_id} style={{ color: sev.hex }}>
                <polygon points={pts} fill={sev.hex} fillOpacity={occupied ? 0.26 : 0.12} />
                <polygon points={pts} fill="url(#hatch)" fillOpacity={occupied ? 0.5 : 0.22} />
                <polygon
                  points={pts}
                  fill="none"
                  stroke={sev.hex}
                  strokeWidth={s(occupied ? 0.22 : 0.12)}
                  strokeDasharray={occupied ? undefined : `${s(0.6)} ${s(0.4)}`}
                />
                {view.k > 0.7 &&
                  (() => {
                    // Long names would run off the edge of the site. Split them
                    // over two lines and keep the anchor inside the plan.
                    const lines = wrapLabel(zone.name);
                    const size = s(lines.some((l) => l.length > 20) ? 0.62 : 0.8);
                    const lx = Math.min(Math.max(cxm, width * 0.1), width * 0.9);
                    return (
                      <text
                        x={lx}
                        y={toY(cym) - ((lines.length - 1) * size) / 2}
                        fill={sev.hex}
                        fontSize={size}
                        fontWeight="600"
                        textAnchor="middle"
                        dominantBaseline="middle"
                        className="pointer-events-none select-none uppercase"
                        style={{ letterSpacing: "0.04em" }}
                      >
                        {lines.map((line, i) => (
                          <tspan key={i} x={lx} dy={i === 0 ? 0 : size * 1.15}>
                            {line}
                          </tspan>
                        ))}
                      </text>
                    );
                  })()}
                {occupied && (
                  <text
                    x={Math.min(Math.max(cxm, width * 0.1), width * 0.9)}
                    y={toY(cym) + s(1.9)}
                    fill={sev.hex}
                    fontSize={s(0.62)}
                    textAnchor="middle"
                    className="pointer-events-none select-none"
                  >
                    {zone.occupancy} inside
                  </text>
                )}
              </g>
            );
          })}

          {/* historical path of the selected worker */}
          {trail?.length > 1 && (
            <>
              <polyline
                points={trail.map(([x, y]) => `${x},${toY(y)}`).join(" ")}
                fill="none"
                stroke="#38bdf8"
                strokeOpacity={0.55}
                strokeWidth={s(0.16)}
                strokeLinejoin="round"
                strokeLinecap="round"
                strokeDasharray={`${s(0.5)} ${s(0.35)}`}
              />
              <circle cx={trail[0][0]} cy={toY(trail[0][1])} r={s(0.28)} fill="#38bdf8" fillOpacity={0.8} />
            </>
          )}

          {/* workers */}
          {workers.map((w) => {
            const pos = w.world_position;
            if (!pos) return null;
            const st = workerState(w.state);
            const selected = selectedWorkerId === w.id;
            const cxp = pos.x;
            const cyp = toY(pos.y);
            return (
              <g
                key={w.id}
                transform={`translate(${cxp} ${cyp})`}
                className="cursor-pointer"
                onPointerEnter={() => setHovered(w.id)}
                onPointerLeave={() => setHovered((h) => (h === w.id ? null : h))}
                onClick={(e) => {
                  e.stopPropagation();
                  onSelectWorker(w.id);
                }}
              >
                {w.state === "DANGER" && (
                  <circle r={s(0.5)} fill="none" stroke={st.hex} strokeWidth={s(0.1)} className="danger-ring" />
                )}
                {selected && (
                  <circle r={s(1.15)} fill="none" stroke="#38bdf8" strokeWidth={s(0.12)} strokeDasharray={`${s(0.25)} ${s(0.2)}`} />
                )}
                <circle r={s(0.52)} fill={st.hex} fillOpacity={0.18} />
                <circle r={s(0.34)} fill={st.hex} stroke="#0a0c0f" strokeWidth={s(0.08)} />
                <text
                  y={-s(0.8)}
                  fill="#c7cedb"
                  fontSize={s(0.72)}
                  fontWeight="600"
                  textAnchor="middle"
                  className="pointer-events-none select-none tabular"
                >
                  {w.id}
                </text>
              </g>
            );
          })}

          {/* hover card */}
          {(() => {
            const w = workers.find((x) => x.id === hovered);
            if (!w?.world_position) return null;
            const bw = 13;
            const bh = 8.4;
            // Flip the card so it stays on screen near the edges.
            const left = w.world_position.x > width - bw - 1;
            const bx = left ? w.world_position.x - bw - 0.8 : w.world_position.x + 0.8;
            const by = Math.min(Math.max(toY(w.world_position.y) - bh / 2, 0), depth - bh);
            const st = workerState(w.state);
            return (
              <g className="pointer-events-none" transform={`translate(${bx} ${by})`}>
                <rect width={bw} height={bh} rx={s(0.3)} fill="#101319" fillOpacity={0.97} stroke="#363e4a" strokeWidth={s(0.08)} />
                <text x={0.7} y={1.7} fill="#e6ebf2" fontSize={s(0.86)} fontWeight="700">
                  Worker #{w.id}
                </text>
                <circle cx={bw - 1} cy={1.35} r={s(0.3)} fill={st.hex} />
                <text x={0.7} y={3.2} fill="#98a2b3" fontSize={s(0.66)} className="tabular">
                  X {w.world_position.x.toFixed(1)} m
                </text>
                <text x={0.7} y={4.3} fill="#98a2b3" fontSize={s(0.66)} className="tabular">
                  Y {w.world_position.y.toFixed(1)} m
                </text>
                {Object.entries(w.ppe_labels ?? {}).map(([item, label], i) => (
                  <text
                    key={item}
                    x={6.6}
                    y={3.2 + i * 1.1}
                    fill={label === "OK" ? "#22c55e" : label === "MISSING" ? "#ef4444" : "#4a5464"}
                    fontSize={s(0.62)}
                  >
                    {label === "OK" ? "OK  " : label === "MISSING" ? "X   " : "?   "}
                    {item}
                  </text>
                ))}
                <text x={0.7} y={6.0} fill={w.active_alerts?.length ? "#f59e0b" : "#4a5464"} fontSize={s(0.64)}>
                  {w.active_alerts?.length
                    ? `${w.active_alerts.length} active alert${w.active_alerts.length > 1 ? "s" : ""}`
                    : "No active alerts"}
                </text>
                <text x={0.7} y={7.4} fill="#4a5464" fontSize={s(0.58)}>
                  {w.zones?.length ? `In: ${w.zones.join(", ")}` : "Clear of all zones"}
                </text>
              </g>
            );
          })()}

          {/* scale bar and origin */}
          <g className="pointer-events-none">
            <line x1={0} y1={toY(-1.2)} x2={10} y2={toY(-1.2)} stroke="#4a5464" strokeWidth={s(0.1)} />
            <line x1={0} y1={toY(-0.9)} x2={0} y2={toY(-1.5)} stroke="#4a5464" strokeWidth={s(0.1)} />
            <line x1={10} y1={toY(-0.9)} x2={10} y2={toY(-1.5)} stroke="#4a5464" strokeWidth={s(0.1)} />
            <text x={5} y={toY(-2.1)} fill="#6b7686" fontSize={s(0.7)} textAnchor="middle">
              10 m
            </text>
            <circle cx={0} cy={toY(0)} r={s(0.18)} fill="#6b7686" />
            <text x={-0.5} y={toY(0) + s(1.2)} fill="#6b7686" fontSize={s(0.65)} textAnchor="end">
              0,0
            </text>
          </g>
        </g>
      </svg>

      {/* controls */}
      <div className="absolute left-3 top-3 flex flex-col gap-1">
        <MapButton onClick={() => zoomCentre(1.3)} title="Zoom in">
          <Plus size={14} />
        </MapButton>
        <MapButton onClick={() => zoomCentre(1 / 1.3)} title="Zoom out">
          <Minus size={14} />
        </MapButton>
        <MapButton onClick={reset} title="Reset view">
          <Maximize2 size={14} />
        </MapButton>
      </div>

      <div className="pointer-events-none absolute bottom-3 left-3 flex items-center gap-2 text-[11px] text-graphite-500">
        <Crosshair size={12} />
        <span className="tabular">
          Local site coordinates &middot; {width} m x {depth} m &middot; {Math.round(view.k * 100)}%
        </span>
      </div>

      {!calibrated && (
        <div className="absolute inset-x-0 top-0 flex items-center justify-center gap-2 bg-sev-medium/12 px-3 py-1.5 text-[11px] text-sev-medium">
          <TriangleAlert size={13} />
          Spatial calibration unavailable - workers are tracked but cannot be placed on the map
        </div>
      )}
    </div>
  );
}

function MapButton({ children, ...props }) {
  return (
    <button
      type="button"
      className="flex h-7 w-7 items-center justify-center rounded border border-graphite-700 bg-graphite-900/90 text-graphite-300 transition hover:border-graphite-600 hover:text-graphite-100"
      {...props}
    >
      {children}
    </button>
  );
}

/**
 * Split a zone name over at most two lines.
 *
 * Prefers a natural break on " - " (as in "Open Edge - South Deck"), and
 * otherwise splits near the middle on a word boundary.
 */
function wrapLabel(name) {
  if (!name) return [""];
  if (name.length <= 18) return [name];

  const dash = name.indexOf(" - ");
  if (dash > 0) return [name.slice(0, dash), name.slice(dash + 3)];

  const words = name.split(" ");
  if (words.length < 2) return [name];
  let best = 1;
  let bestDiff = Infinity;
  for (let i = 1; i < words.length; i += 1) {
    const left = words.slice(0, i).join(" ").length;
    const right = words.slice(i).join(" ").length;
    const diff = Math.abs(left - right);
    if (diff < bestDiff) {
      bestDiff = diff;
      best = i;
    }
  }
  return [words.slice(0, best).join(" "), words.slice(best).join(" ")];
}

/** Area-weighted polygon centroid, so the label sits inside concave shapes. */
function centroid(points) {
  let area = 0;
  let cx = 0;
  let cy = 0;
  for (let i = 0; i < points.length; i += 1) {
    const [x1, y1] = points[i];
    const [x2, y2] = points[(i + 1) % points.length];
    const cross = x1 * y2 - x2 * y1;
    area += cross;
    cx += (x1 + x2) * cross;
    cy += (y1 + y2) * cross;
  }
  if (Math.abs(area) < 1e-9) {
    // Degenerate ring: fall back to the vertex mean rather than dividing by ~0.
    const n = points.length || 1;
    return [
      points.reduce((a, p) => a + p[0], 0) / n,
      points.reduce((a, p) => a + p[1], 0) / n,
    ];
  }
  area *= 0.5;
  return [cx / (6 * area), cy / (6 * area)];
}

export { ZONE_TYPE_LABEL };
