import { useCallback, useEffect, useRef, useState } from "react";
import { createEventSocket } from "./socket.js";

/**
 * Subscribes to a Triage Line WebSocket channel and exposes:
 *   - status: "idle" | "connecting" | "connected" | "disconnected" | "error"
 *   - latestEvent: the most recent parsed event dict, or null
 *   - events: most-recent-first list of parsed event dicts (capped at maxEvents)
 *   - clear(): empties the local history (does not touch the socket)
 *
 * Each stored event gets a NON-enumerable `_receivedAt` (client Date.now()
 * on arrival). It is used only for UI liveness indicators (MetricsBar);
 * being non-enumerable it never shows up in JSON.stringify / the debug
 * stream, so the event payload as rendered is exactly what the backend sent.
 *
 * `wsUrl` of null/undefined means "not connected yet" (idle) -- this is
 * how the UI lets the user pick a channel before dialing in, rather than
 * connecting to a hardcoded id on mount.
 *
 * Per Phase 7B-1 scope, this hook does NOT auto-reconnect on drop; it only
 * detects and reports the disconnect safely (no throw, no crash). Retrying
 * is a UI-level decision left to a later phase.
 */
export function useEventSocket(wsUrl, { maxEvents = 50 } = {}) {
  const [status, setStatus] = useState("idle");
  const [latestEvent, setLatestEvent] = useState(null);
  const [events, setEvents] = useState([]);
  const socketRef = useRef(null);

  useEffect(() => {
    // Reset history on every new connection attempt so stale events from a
    // previous channel don't linger under a new one.
    setLatestEvent(null);
    setEvents([]);

    if (!wsUrl) {
      setStatus("idle");
      return undefined;
    }

    setStatus("connecting");

    const socket = createEventSocket({
      url: wsUrl,
      onOpen: () => setStatus("connected"),
      onClose: () => setStatus("disconnected"),
      onError: () => setStatus("error"),
      onMessage: (payload) => {
        if (payload && typeof payload === "object") {
          Object.defineProperty(payload, "_receivedAt", { value: Date.now(), enumerable: false });
        }
        setLatestEvent(payload);
        setEvents((prev) => [payload, ...prev].slice(0, maxEvents));
      },
    });
    socketRef.current = socket;

    return () => {
      socket.close();
      socketRef.current = null;
    };
  }, [wsUrl, maxEvents]);

  const clear = useCallback(() => {
    setLatestEvent(null);
    setEvents([]);
  }, []);

  return { status, latestEvent, events, clear };
}
