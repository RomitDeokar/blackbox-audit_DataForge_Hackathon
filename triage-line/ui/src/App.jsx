import { useState } from "react";
import { buildWsUrl } from "./lib/config.js";
import { useEventSocket } from "./lib/useEventSocket.js";
import ConnectionBadge from "./components/ConnectionBadge.jsx";
import Panel from "./components/Panel.jsx";
import ConversationPanel from "./components/ConversationPanel.jsx";
import DeliberationPanel from "./components/DeliberationPanel.jsx";
import DispatchPanel from "./components/DispatchPanel.jsx";
import DebugEventStream from "./components/DebugEventStream.jsx";
import MetricsBar from "./components/MetricsBar.jsx";
import DemoControls from "./components/DemoControls.jsx";
import LiveControls from "./components/LiveControls.jsx";

/**
 * Live Call View (docs/UI_SPEC.md §1.1).
 *
 * 7B-2A: Conversation. 7B-2B: Deliberation + Dispatch.
 * 7B-2C: MetricsBar (MetricsTick / BargeIn / connection / stream activity),
 * the single demo-trigger write action, and a light layout pass to the
 * spec's 3-column (desktop) / stacked (mobile) console layout.
 *
 * Every panel still folds over the ONE `events` stream from ONE WebSocket.
 */
export default function App() {
  const [channelKind, setChannelKind] = useState("call");
  const [channelId, setChannelId] = useState(() => `call-${crypto.randomUUID().slice(0, 8)}`);
  const [activeWsUrl, setActiveWsUrl] = useState(null);
  const [activeChannel, setActiveChannel] = useState(null);

  const { status, latestEvent, events, clear } = useEventSocket(activeWsUrl, { maxEvents: 500 });

  function handleConnect(e) {
    e.preventDefault();
    const url = buildWsUrl(channelKind, channelId);
    setActiveWsUrl(url);
    setActiveChannel(url ? { kind: channelKind, id: channelId.trim() } : null);
  }

  function handleDisconnect() {
    setActiveWsUrl(null);
    setActiveChannel(null);
  }

  const connected = status === "connected";
  const canDemo = connected && activeChannel?.kind === "replay";

  return (
    <div className="min-h-screen bg-slate-950 pb-32 text-slate-100 antialiased lg:pb-20">
      <header className="z-10 border-b lg:sticky lg:top-0 border-slate-800 bg-slate-950/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div className="flex items-baseline gap-3">
            <h1 className="text-xl font-bold tracking-tight">
              Triage Line
              <span className="ml-2 text-sm font-medium text-slate-500">dispatch console</span>
            </h1>
          </div>

          <form onSubmit={handleConnect} className="flex flex-wrap items-center gap-2 text-sm">
            <select
              value={channelKind}
              onChange={(e) => setChannelKind(e.target.value)}
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1"
              aria-label="channel kind"
            >
              <option value="call">Interactive call</option>
              <option value="replay">Replay run</option>
            </select>
            <input
              type="text"
              value={channelId}
              onChange={(e) => setChannelId(e.target.value)}
              placeholder={channelKind === "replay" ? "run_id" : "call_id"}
              className="w-28 rounded border border-slate-700 bg-slate-900 px-2 py-1"
              aria-label="channel id"
            />
            <button type="submit" className="rounded bg-sky-600 px-3 py-1 font-medium hover:bg-sky-500">
              Connect
            </button>
            <button
              type="button"
              onClick={handleDisconnect}
              className="rounded border border-slate-700 px-3 py-1 font-medium hover:bg-slate-800"
            >
              Disconnect
            </button>
            <ConnectionBadge status={status} />
          </form>
        </div>

        <div className="border-t border-slate-800/70 bg-slate-900/40">
          <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-2">
            {activeChannel?.kind === "call" ? (
              <LiveControls key={activeChannel.id} callId={activeChannel.id} connected={connected} events={events} />
            ) : (
              <DemoControls runId={canDemo ? activeChannel.id : null} connected={canDemo} />
            )}
            <div className="flex items-center gap-3 text-xs text-slate-500">
              {activeChannel && (
                <span className="font-mono">
                  channel: /ws/{activeChannel.kind}/{activeChannel.id}
                </span>
              )}
              <button
                type="button"
                onClick={clear}
                className="rounded border border-slate-700 px-2 py-0.5 hover:bg-slate-800"
              >
                Clear view
              </button>
            </div>
          </div>
        </div>
      </header>

      <main className="mx-auto flex max-w-7xl flex-col gap-4 p-4">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <Panel title="Conversation" accent="bg-sky-400">
            <ConversationPanel events={events} />
          </Panel>
          <Panel title="Deliberation" accent="bg-violet-400">
            <DeliberationPanel events={events} />
          </Panel>
          <Panel title="Dispatch state" accent="bg-emerald-400">
            <DispatchPanel events={events} />
          </Panel>
        </div>

        <details className="group rounded-xl border border-slate-800 bg-slate-900/40">
          <summary className="cursor-pointer select-none px-4 py-2 text-xs font-semibold uppercase tracking-wider text-slate-500 hover:text-slate-300">
            Raw event stream ({events.length})
          </summary>
          <div className="px-2 pb-2">
            <DebugEventStream status={status} latestEvent={latestEvent} events={events} />
          </div>
        </details>
      </main>

      <MetricsBar status={status} events={events} />
    </div>
  );
}
