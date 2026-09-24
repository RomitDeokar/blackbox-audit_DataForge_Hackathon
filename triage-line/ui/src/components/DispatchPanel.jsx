import { useMemo } from "react";

/**
 * Phase 7B-2B: renders the dispatch/action state machine from the same
 * event stream `useEventSocket` already exposes (see App.jsx) -- no second
 * event source, no new WebSocket connection. Builds one record per
 * action_id by folding over `events` (oldest -> newest), matching the
 * pattern used by ConversationPanel.jsx / DeliberationPanel.jsx.
 *
 * Events handled (per docs/UI_SPEC.md section 3 + core/events.py):
 *   - ActionProposed  -> state = PROPOSED
 *   - ActionPending   -> state = PENDING_CONFIRMATION
 *   - ActionFinalized -> state = FINALIZED
 *   - ActionAborted   -> state = ABORTED (with reason)
 *
 * State is derived strictly from the most recent matching event for that
 * action_id -- never inferred or defaulted to FINALIZED.
 */
const STEPS = ["PROPOSED", "PENDING_CONFIRMATION", "FINALIZED"];

export default function DispatchPanel({ events }) {
  const actions = useMemo(() => buildActions(events), [events]);

  if (actions.length === 0) {
    return (
      <div className="m-auto text-center text-sm text-slate-600">
        No dispatch action yet — connect and run a call or replay.
      </div>
    );
  }

  // Most recent action first.
  const ordered = [...actions].reverse();

  return (
    <div className="flex w-full flex-col gap-4 overflow-y-auto text-sm">
      {ordered.map((a) => (
        <ActionCard key={a.actionId} action={a} />
      ))}
    </div>
  );
}

function ActionCard({ action }) {
  const { actionId, actionType, state, reason, log } = action;
  const aborted = state === "ABORTED";
  const reached = new Set(log.map((entry) => entry.state));

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-800/40 p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
          {actionType ?? "action"} · {actionId}
        </span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1">
        {STEPS.map((step, i) => (
          <StepChip
            key={step}
            label={step.replace("_", " ")}
            // 7B-2C: an aborted action shows which steps it actually
            // reached (from its own transition log) -- never FINALIZED.
            tone={stepTone(step, state, reached)}
            isLast={!aborted && i === STEPS.length - 1}
            hideArrow={aborted && i === STEPS.length - 1}
          />
        ))}
        {aborted && <StepChip label="ABORTED" tone="aborted" isLast />}
      </div>

      {state === "FINALIZED" && (
        <p className="mt-2 text-xs text-amber-300">Recorded confirmation only — no external provider was contacted.</p>
      )}
      {aborted && reason && (
        <div className="mt-2 rounded bg-red-500/10 px-2 py-1 text-xs text-red-300">
          aborted: {reason}
        </div>
      )}

      {log.length > 0 && (
        <div className="mt-3">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">
            transition log
          </div>
          <ul className="mt-1 space-y-0.5 text-xs text-slate-400">
            {log.map((entry, i) => (
              <li key={i}>
                <span className="text-slate-600">{entry.ts ?? ""}</span>{" "}
                {entry.label}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function stepTone(step, state, reached) {
  if (state === "ABORTED") return reached.has(step) ? "reached" : "idle";
  if (step === state) return step === "FINALIZED" ? "finalized" : "active";
  return STEPS.indexOf(state) > STEPS.indexOf(step) ? "done" : "idle";
}

// UI_SPEC.md §4: grey = not started, amber = pending, green = finalized, red = aborted.
const TONES = {
  idle: "bg-slate-800 text-slate-500 border-slate-700",
  active: "bg-amber-400/20 text-amber-200 border-amber-400/60 animate-pulse",
  done: "bg-emerald-400/10 text-emerald-300/80 border-emerald-400/30",
  finalized: "bg-emerald-500/25 text-emerald-200 border-emerald-400/70",
  reached: "bg-slate-700/60 text-slate-300 border-slate-600",
  aborted: "bg-red-500/25 text-red-200 border-red-500/70",
};

function StepChip({ label, tone, isLast, hideArrow }) {
  return (
    <div className="flex items-center gap-1">
      <span
        className={`rounded border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider transition-colors duration-200 ${
          TONES[tone] ?? TONES.idle
        }`}
      >
        {label}
      </span>
      {!isLast && !hideArrow && <span className="text-slate-600">→</span>}
    </div>
  );
}

/**
 * `events` is most-recent-first and capped; iterate oldest -> newest so
 * state transitions apply in order. One record per action_id, state is
 * simply "whatever the last matching event said" -- e.g. a stale
 * ActionPending can never appear after a real ActionFinalized because we
 * process strictly in chronological order and always overwrite `state`.
 */
function buildActions(events) {
  const chronological = [...events].reverse();
  const byId = new Map();
  const order = [];

  function getOrCreate(actionId) {
    if (!byId.has(actionId)) {
      const record = {
        actionId,
        actionType: null,
        state: "PROPOSED",
        reason: null,
        log: [],
      };
      byId.set(actionId, record);
      order.push(actionId);
    }
    return byId.get(actionId);
  }

  function fmtTs(ts) {
    return ts != null ? String(ts) : null;
  }

  chronological.forEach((evt) => {
    switch (evt.event_type) {
      case "ActionProposed": {
        const rec = getOrCreate(evt.action_id);
        rec.actionType = evt.action_type ?? rec.actionType;
        rec.state = "PROPOSED";
        rec.log.push({ ts: fmtTs(evt.timestamp_ms), state: "PROPOSED", label: "PROPOSED" });
        break;
      }

      case "ActionPending": {
        const rec = getOrCreate(evt.action_id);
        rec.state = "PENDING_CONFIRMATION";
        rec.log.push({ ts: fmtTs(evt.timestamp_ms), state: "PENDING_CONFIRMATION", label: "PENDING CONFIRMATION" });
        break;
      }

      case "ActionFinalized": {
        const rec = getOrCreate(evt.action_id);
        rec.state = "FINALIZED";
        rec.log.push({ ts: fmtTs(evt.timestamp_ms), state: "FINALIZED", label: "FINALIZED" });
        break;
      }

      case "ActionAborted": {
        const rec = getOrCreate(evt.action_id);
        rec.state = "ABORTED";
        rec.reason = evt.reason ?? null;
        rec.log.push({
          ts: fmtTs(evt.timestamp_ms),
          state: "ABORTED",
          label: `ABORTED${evt.reason ? ` (${evt.reason})` : ""}`,
        });
        break;
      }

      default:
        // Conversation/deliberation/metrics events -- not this panel's concern.
        break;
    }
  });

  return order.map((id) => byId.get(id));
}
