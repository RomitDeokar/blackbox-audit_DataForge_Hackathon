import { useEffect, useState } from "react";

/**
 * Phase 7B-2C: the UI's single write action (docs/UI_SPEC.md intro:
 * "exactly one write action (triggering a scripted demo call)").
 *
 * It only calls the existing backend demo endpoints on the currently
 * connected replay run_id -- it does not render anything from the HTTP
 * responses. Everything shown on screen still arrives over the WebSocket.
 *
 *   1. POST /demo/dialogue/{run_id}/{scenario}  -> DialogueEngine + mock
 *      providers: transcripts, backchannels, BargeIn, MetricsTick.
 *   2. POST /demo/run/{run_id}/{scenario}       -> harness.replay: the real
 *      DeliberationEngine + CommitStateMachine events.
 */
export default function DemoControls({ runId, connected }) {
  const [scenarios, setScenarios] = useState([]);
  const [scenario, setScenario] = useState("barge_in");
  const [agentType, setAgentType] = useState("deliberative");
  const [paceMs, setPaceMs] = useState(350);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);

  useEffect(() => {
    fetch("/demo/scenarios")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data) => setScenarios(data.scenarios ?? []))
      .catch((err) => setMessage(`could not load scenarios: ${err.message}`));
  }, []);

  async function runDemo() {
    if (!runId) return;
    setBusy(true);
    setMessage(null);
    const id = encodeURIComponent(runId);
    const sc = encodeURIComponent(scenario);
    try {
      const d = await fetch(`/demo/dialogue/${id}/${sc}?pace_ms=${paceMs}`, { method: "POST" });
      if (!d.ok) throw new Error(`dialogue: HTTP ${d.status}`);
      const r = await fetch(
        `/demo/run/${id}/${sc}?agent_type=${agentType}&pace_ms=${paceMs}`,
        { method: "POST" }
      );
      if (!r.ok) throw new Error(`replay: HTTP ${r.status}`);
      setMessage("demo run complete");
    } catch (err) {
      setMessage(`demo failed: ${err.message}`);
    } finally {
      setBusy(false);
    }
  }

  const disabled = busy || !connected || !runId;

  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <select
        value={scenario}
        onChange={(e) => setScenario(e.target.value)}
        className="rounded border border-slate-700 bg-slate-900 px-2 py-1"
        aria-label="scenario"
      >
        {(scenarios.length ? scenarios : [scenario]).map((s) => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>
      <select
        value={agentType}
        onChange={(e) => setAgentType(e.target.value)}
        className="rounded border border-slate-700 bg-slate-900 px-2 py-1"
        aria-label="agent type"
      >
        <option value="deliberative">deliberative</option>
        <option value="naive">naive</option>
      </select>
      <select
        value={paceMs}
        onChange={(e) => setPaceMs(Number(e.target.value))}
        className="rounded border border-slate-700 bg-slate-900 px-2 py-1"
        aria-label="pace"
        title="Delay between relayed events (presentation only)"
      >
        <option value={0}>instant</option>
        <option value={150}>fast</option>
        <option value={350}>normal</option>
        <option value={800}>slow</option>
      </select>
      <button
        type="button"
        onClick={runDemo}
        disabled={disabled}
        className="rounded bg-emerald-600 px-3 py-1 font-medium hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-40"
        title={connected ? "Run scripted demo call" : "Connect to a replay channel first"}
      >
        {busy ? "Running…" : "▶ Run demo call"}
      </button>
      {message && <span className="text-xs text-slate-400">{message}</span>}
    </div>
  );
}
