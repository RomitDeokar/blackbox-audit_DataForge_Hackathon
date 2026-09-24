import { useEffect, useMemo, useState } from "react";
import ConnectionBadge from "./ConnectionBadge.jsx";

/**
 * Phase 7B-2C: bottom status bar (docs/UI_SPEC.md §1.1 / §2 `MetricsBar`).
 *
 * Renders ONLY what the event stream actually carries -- no metrics are
 * computed here:
 *   - MetricsTick.barge_in_latency_ms / time_to_decision_ms: latest non-null
 *     value of each, exactly as sent by the backend. A field that no
 *     MetricsTick has populated is shown as "not reported", never guessed.
 *     (Aggregate reporting -- p50/p95 etc. -- stays in Phase 6D's
 *     reporting/ and is intentionally not duplicated here.)
 *   - BargeIn: count + a short-lived "INTERRUPTED" flash, plus the
 *     event's own position_ms / latency_ms.
 *   - Call ID(s): the call_id field of received events.
 *   - Stream activity: whether an event arrived in the last few seconds
 *     (there is no explicit call-start/call-end event in core/events.py,
 *     so "live" is honestly derived from event arrival, not invented).
 *   - Current floor: speaker of the latest TurnStarted event.
 */
const ACTIVE_WINDOW_MS = 3000;
const BARGE_FLASH_MS = 2500;

export default function MetricsBar({ status, events }) {
  const m = useMemo(() => foldMetrics(events), [events]);
  const now = useNow(500);

  const streamActive = m.lastEventAt != null && now - m.lastEventAt < ACTIVE_WINDOW_MS;
  const bargeFlash = m.lastBargeInAt != null && now - m.lastBargeInAt < BARGE_FLASH_MS;

  return (
    <footer className="fixed inset-x-0 bottom-0 z-20 border-t border-slate-800 bg-slate-950/95 backdrop-blur">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-5 gap-y-1.5 px-4 py-2 text-sm">
        <Metric
          label="Barge-in latency"
          value={m.bargeInLatencyMs != null ? `${m.bargeInLatencyMs} ms` : null}
          hint={m.tickCount > 0 ? `${m.tickCount} MetricsTick` : "no MetricsTick yet"}
        />
        <Metric
          label="Time-to-decision"
          value={m.timeToDecisionMs != null ? `${m.timeToDecisionMs} ms` : null}
        />

        <span className="hidden h-6 w-px bg-slate-800 md:block" />

        <span
          className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-wider transition-colors duration-200 ${
            bargeFlash
              ? "animate-pulse border-amber-400 bg-amber-400/20 text-amber-200"
              : "border-slate-700 bg-slate-900 text-slate-400"
          }`}
          title={
            m.lastBargeIn
              ? `last BargeIn: position_ms=${m.lastBargeIn.position_ms}, latency_ms=${m.lastBargeIn.latency_ms ?? "n/a"}`
              : "no BargeIn event received"
          }
        >
          <span className={`h-2 w-2 rounded-full ${bargeFlash ? "bg-amber-400" : "bg-slate-600"}`} />
          {bargeFlash ? "Interrupted" : "Barge-ins"} · {m.bargeInCount}
        </span>

        <span
          className="inline-flex items-center gap-2 text-xs uppercase tracking-wider text-slate-400"
          title="Derived from event arrival: no explicit call start/end event exists in core/events.py"
        >
          <span
            className={`h-2 w-2 rounded-full ${
              streamActive ? "animate-pulse bg-rose-500" : "bg-slate-600"
            }`}
          />
          {streamActive ? "Live stream" : "Stream idle"}
          {m.currentSpeaker && (
            <span className="text-slate-500">· floor: {m.currentSpeaker}</span>
          )}
        </span>

        <span className="ml-auto flex flex-wrap items-center gap-3">
          <span className="font-mono text-xs text-slate-400" title={m.callIds.join("\n")}>
            Call:{" "}
            <span className="text-slate-200">
              {m.callIds.length === 0 ? "—" : m.callIds[m.callIds.length - 1]}
            </span>
            {m.callIds.length > 1 && (
              <span className="text-slate-500"> (+{m.callIds.length - 1})</span>
            )}
          </span>
          <span className="hidden font-mono text-xs text-slate-500 xl:inline">{m.eventCount} events</span>
          <span className="hidden sm:inline-flex">
            <ConnectionBadge status={status} />
          </span>
        </span>
      </div>
    </footer>
  );
}

function Metric({ label, value, hint }) {
  return (
    <div className="flex items-baseline gap-2" title={hint}>
      <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
        {label}
      </span>
      {value != null ? (
        <span className="font-mono text-lg font-semibold text-emerald-300">{value}</span>
      ) : (
        <span className="text-xs italic text-slate-600">not reported</span>
      )}
    </div>
  );
}

function useNow(intervalMs) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/**
 * `events` is most-recent-first (useEventSocket). Each event carries the
 * client-side `_receivedAt` stamp added by useEventSocket on arrival; that
 * stamp is only used for the activity/flash indicators, never as a metric.
 */
export function foldMetrics(events) {
  const out = {
    bargeInLatencyMs: null,
    timeToDecisionMs: null,
    tickCount: 0,
    bargeInCount: 0,
    lastBargeIn: null,
    lastBargeInAt: null,
    lastEventAt: events.length > 0 ? events[0]._receivedAt ?? null : null,
    currentSpeaker: null,
    callIds: [],
    eventCount: events.length,
  };

  const seenCalls = new Set();
  const chronological = [...events].reverse();
  for (const evt of chronological) {
    if (evt.call_id && !seenCalls.has(evt.call_id)) {
      seenCalls.add(evt.call_id);
      out.callIds.push(evt.call_id);
    }
    switch (evt.event_type) {
      case "MetricsTick":
        out.tickCount += 1;
        if (evt.barge_in_latency_ms != null) out.bargeInLatencyMs = evt.barge_in_latency_ms;
        if (evt.time_to_decision_ms != null) out.timeToDecisionMs = evt.time_to_decision_ms;
        break;
      case "BargeIn":
        out.bargeInCount += 1;
        out.lastBargeIn = evt;
        out.lastBargeInAt = evt._receivedAt ?? null;
        break;
      case "TurnStarted":
        out.currentSpeaker = evt.speaker ?? out.currentSpeaker;
        break;
      default:
        break;
    }
  }
  return out;
}
