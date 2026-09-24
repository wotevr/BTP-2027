import { useEffect, useRef, useState } from "react";
import { Camera, CameraOff, Maximize2, Minimize2 } from "lucide-react";
import { cx } from "../lib/format";

/**
 * Live camera view, beside the map.
 *
 * The map alone is not enough to trust the system: coloured dots on a plan give
 * an operator no way to check that the detector is seeing what they think it
 * is. Showing the annotated camera frame next to the map makes the whole
 * pipeline legible - a bounding box in pixels and its marker in metres, at the
 * same moment.
 *
 * Rendered as MJPEG straight into an <img>. The browser decodes
 * multipart/x-mixed-replace natively, so there is no player library, no
 * WebSocket and no canvas loop - the stream simply paints itself.
 *
 * The boxes are drawn UPSTREAM by the detector that produced them, so this is
 * the detector's own rendering rather than a second interpretation of it.
 */
export default function CameraFeed({ cameraId, live, expanded, onToggleExpand }) {
  const [status, setStatus] = useState("connecting");
  const imgRef = useRef(null);
  const [nonce, setNonce] = useState(() => Date.now());

  // A new nonce forces the browser to drop a dead stream and reconnect,
  // instead of sitting on a connection the backend has already closed.
  useEffect(() => {
    if (!live) return undefined;
    setStatus("connecting");
    const id = setInterval(() => {
      const img = imgRef.current;
      if (img && !img.complete) return;
      if (img && img.naturalWidth === 0) setNonce(Date.now());
    }, 8000);
    return () => clearInterval(id);
  }, [live, cameraId]);

  const src = cameraId
    ? `/api/v1/camera/${encodeURIComponent(cameraId)}/stream.mjpg?n=${nonce}`
    : null;

  return (
    <div
      className={cx(
        "flex flex-col overflow-hidden border border-graphite-800 bg-graphite-900",
        expanded ? "absolute inset-3 z-20 rounded-lg shadow-2xl" : "rounded",
      )}
    >
      <div className="flex h-8 shrink-0 items-center gap-2 border-b border-graphite-800 px-2.5">
        {live ? (
          <Camera size={12} className="text-safe" />
        ) : (
          <CameraOff size={12} className="text-graphite-600" />
        )}
        <span className="text-[10px] font-semibold tracking-widest text-graphite-400 uppercase">
          Camera Feed
        </span>
        <span className="font-mono text-[10px] text-graphite-500">{cameraId ?? "-"}</span>
        <span
          className={cx(
            "ml-auto rounded px-1.5 py-px text-[9px] font-bold tracking-wider uppercase",
            live && status === "ok"
              ? "bg-safe/12 text-safe"
              : "bg-graphite-800 text-graphite-500",
          )}
        >
          {live ? (status === "ok" ? "Live" : "Connecting") : "No feed"}
        </span>
        <button
          type="button"
          onClick={onToggleExpand}
          title={expanded ? "Shrink" : "Expand"}
          className="rounded p-1 text-graphite-500 transition hover:text-graphite-200"
        >
          {expanded ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
        </button>
      </div>

      <div className="relative flex min-h-0 flex-1 items-center justify-center bg-black">
        {live && src ? (
          <img
            ref={imgRef}
            key={src}
            src={src}
            alt="Live annotated camera feed"
            className="h-full w-full object-contain"
            onLoad={() => setStatus("ok")}
            onError={() => setStatus("error")}
          />
        ) : (
          <div className="px-5 py-8 text-center">
            <CameraOff size={20} className="mx-auto text-graphite-700" />
            <p className="mt-2 text-[11.5px] text-graphite-500">No camera feed</p>
            <p className="mx-auto mt-1 max-w-[230px] text-[10.5px] leading-relaxed text-graphite-600">
              Run the bridge with{" "}
              <span className="font-mono text-graphite-400">--send-frames</span> to
              stream annotated video here. The map and alerts work without it.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
