/**
 * Generic section frame for the three live-console columns. Phase 7B-2C
 * light cleanup: top-aligned (was centered), independently scrollable body
 * with a bounded height so long calls don't push the status bar away, and
 * an optional right-aligned header slot for small per-panel status.
 */
export default function Panel({ title, accent = "bg-slate-500", aside, children }) {
  return (
    <section className="flex min-h-[260px] flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-900/60 shadow-lg shadow-black/20 lg:h-[calc(100vh-15rem)] lg:min-h-[420px]">
      <header className="flex items-center justify-between gap-2 border-b border-slate-800 px-4 py-2.5">
        <h2 className="flex items-center gap-2 text-xs font-bold uppercase tracking-[0.18em] text-slate-300">
          <span className={`h-2 w-2 rounded-full ${accent}`} />
          {title}
        </h2>
        {aside}
      </header>
      <div className="flex flex-1 flex-col overflow-y-auto p-4 text-sm">
        {children ?? <span className="text-slate-600">Nothing yet.</span>}
      </div>
    </section>
  );
}
