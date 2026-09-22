import { useCallback, useEffect, useRef, useState } from "react";
import { wsUrl } from "../services/api";

/**
 * Live site-state socket.
 *
 * Responsibilities beyond "open a socket":
 *  - reconnects with exponential backoff, so a backend restart heals itself
 *  - keeps a heartbeat so a silently dead connection (sleeping laptop, dropped
 *    wifi) is noticed instead of appearing frozen but connected
 *  - keeps a rolling log of recent events, since the WebSocket only sends the
 *    CURRENT state and a one-shot event disappears from it immediately
 */
const MAX_BACKOFF = 15000;
const HEARTBEAT_MS = 10000;
const STALE_MS = 25000;
const EVENT_LOG_LIMIT = 60;

export function useWebSocket() {
  const [state, setState] = useState(null);
  const [status, setStatus] = useState("connecting");
  const [eventLog, setEventLog] = useState([]);

  const socketRef = useRef(null);
  const retryRef = useRef(0);
  const timersRef = useRef({ reconnect: null, heartbeat: null });
  const lastMessageRef = useRef(Date.now());
  const closedByUs = useRef(false);

  const clearTimers = () => {
    const t = timersRef.current;
    if (t.reconnect) clearTimeout(t.reconnect);
    if (t.heartbeat) clearInterval(t.heartbeat);
    t.reconnect = null;
    t.heartbeat = null;
  };

  const connect = useCallback(() => {
    clearTimers();
    let socket;
    try {
      socket = new WebSocket(wsUrl());
    } catch {
      setStatus("error");
      return;
    }
    socketRef.current = socket;
    setStatus(retryRef.current === 0 ? "connecting" : "reconnecting");

    socket.onopen = () => {
      retryRef.current = 0;
      lastMessageRef.current = Date.now();
      setStatus("connected");

      timersRef.current.heartbeat = setInterval(() => {
        if (socket.readyState !== WebSocket.OPEN) return;
        // If nothing has arrived for a while the connection is dead even
        // though the browser still reports it open. Force a reconnect.
        if (Date.now() - lastMessageRef.current > STALE_MS) {
          socket.close();
          return;
        }
        socket.send(JSON.stringify({ type: "ping" }));
      }, HEARTBEAT_MS);
    };

    socket.onmessage = (event) => {
      lastMessageRef.current = Date.now();
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return; // ignore anything that is not JSON
      }
      if (message.type === "pong") return;
      if (message.type !== "site_state") return;

      setState(message);
      if (message.new_events?.length) {
        setEventLog((log) =>
          [...message.new_events, ...log].slice(0, EVENT_LOG_LIMIT),
        );
      }
    };

    socket.onerror = () => setStatus("error");

    socket.onclose = () => {
      clearTimers();
      if (closedByUs.current) return;
      // Exponential backoff, capped, so a backend that is down does not get
      // hammered and the UI does not spin.
      const delay = Math.min(1000 * 2 ** retryRef.current, MAX_BACKOFF);
      retryRef.current += 1;
      setStatus("reconnecting");
      timersRef.current.reconnect = setTimeout(connect, delay);
    };
  }, []);

  useEffect(() => {
    closedByUs.current = false;
    connect();
    return () => {
      closedByUs.current = true;
      clearTimers();
      socketRef.current?.close();
    };
  }, [connect]);

  const requestState = useCallback(() => {
    const socket = socketRef.current;
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "state" }));
    }
  }, []);

  return { state, status, eventLog, requestState };
}
