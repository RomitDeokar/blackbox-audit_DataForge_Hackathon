import { useEffect, useRef, useState } from "react";

// Browser speech is optional: the same server-side call works by keyboard.
// A confirmed action only records the decision; no dispatch provider is wired.
export default function LiveControls({ callId, connected, events }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [listening, setListening] = useState(false);
  const [message, setMessage] = useState("");
  const recognition = useRef(null);
  const active = useRef(true);
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const pending = events.find((e) => e.event_type === "ActionPending" &&
    !events.some((later) => (later.event_type === "ActionFinalized" || later.event_type === "ActionAborted") &&
      later.action_id === e.action_id));

  useEffect(() => () => {
    active.current = false;
    recognition.current?.abort();
    window.speechSynthesis?.cancel();
  }, []);

  async function post(path, body) {
    const response = await fetch(`/calls/${encodeURIComponent(callId)}/${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    return data;
  }

  async function interrupt() {
    if (window.speechSynthesis?.speaking) {
      window.speechSynthesis.cancel(); // stop locally BEFORE network round-trip
      await post("interrupt");
    }
  }

  async function send(line = text) {
    if (!line.trim() || !connected || busy) return;
    setBusy(true);
    setMessage("");
    try {
      await interrupt();
      const result = await post("turn", { text: line.trim() });
      if (active.current) setText("");
      if (window.speechSynthesis && active.current) {
        window.speechSynthesis.cancel();
        const utterance = new SpeechSynthesisUtterance(result.reply);
        utterance.rate = 1.05;
        window.speechSynthesis.speak(utterance);
      }
    } catch (err) { if (active.current) setMessage(err.message); }
    finally { if (active.current) setBusy(false); }
  }

  function toggleMic() {
    if (listening) { recognition.current?.stop(); return; }
    if (!SpeechRecognition) { setMessage("Speech recognition is unavailable in this browser. Use text input."); return; }
    const mic = new SpeechRecognition();
    recognition.current = mic;
    mic.lang = "en-US";
    mic.continuous = true;
    mic.interimResults = true;
    mic.onstart = () => setListening(true);
    mic.onend = () => setListening(false);
    mic.onerror = (event) => setMessage(`Microphone: ${event.error}`);
    mic.onspeechstart = () => { interrupt().catch((err) => setMessage(err.message)); };
    mic.onresult = (event) => {
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const value = event.results[i][0]?.transcript || "";
        if (event.results[i].isFinal) send(value);
        else setText(value);
      }
    };
    try { mic.start(); } catch (err) { setMessage(err.message); }
  }

  async function decide(confirm) {
    if (!pending) return;
    setBusy(true);
    try {
      const result = await post("decision", { action_id: pending.action_id, confirm });
      setMessage(`${result.state}: ${result.notice}`);
    } catch (err) { setMessage(err.message); }
    finally { setBusy(false); }
  }

  async function endCall() {
    setBusy(true);
    try {
      recognition.current?.abort();
      window.speechSynthesis?.cancel();
      await post("end");
      setMessage("Call ended. Pending proposals were aborted.");
    } catch (err) { setMessage(err.message); }
    finally { setBusy(false); }
  }

  return (
    <div className="flex w-full flex-wrap items-center gap-2 text-sm">
      <form className="flex min-w-[240px] flex-1 gap-2" onSubmit={(e) => { e.preventDefault(); send(); }}>
        <input aria-label="Caller message" value={text} onChange={(e) => setText(e.target.value)}
          placeholder="Describe your breakdown or location…" maxLength={2000}
          className="min-w-0 flex-1 rounded border border-slate-700 bg-slate-900 px-3 py-2" />
        <button disabled={!connected || busy || !text.trim()} className="rounded bg-sky-600 px-3 py-2 disabled:opacity-40">Send</button>
      </form>
      <button type="button" onClick={toggleMic} disabled={!connected || busy}
        aria-label={listening ? "Stop microphone" : "Start microphone"}
        className="rounded border border-slate-600 px-3 py-2 disabled:opacity-40">{listening ? "Stop mic" : "Mic"}</button>
      {pending && <div className="flex items-center gap-2 rounded border border-amber-500/50 px-2 py-1">
        <span className="text-amber-300">Awaiting explicit confirmation</span>
        <button type="button" disabled={busy} onClick={() => decide(true)} className="rounded bg-emerald-700 px-2 py-1 disabled:opacity-40">Confirm</button>
        <button type="button" disabled={busy} onClick={() => decide(false)} className="rounded bg-rose-800 px-2 py-1 disabled:opacity-40">Reject</button>
      </div>}
      <button type="button" onClick={endCall} disabled={!connected || busy} className="rounded border border-rose-700 px-2 py-1 disabled:opacity-40">End call</button>
      {message && <span role="status" className="text-xs text-amber-300">{message}</span>}
      <span className="w-full text-xs text-slate-500">Browser voice is optional. No external dispatch is sent; confirmation records an action only.</span>
    </div>
  );
}
