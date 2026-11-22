import React, { useCallback, useEffect, useRef, useState } from "react";

// ---------------------------------------------------------------- formatting
const inrFmt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
export function inr(value) {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return String(value);
  return (n < 0 ? "−₹" : "₹") + inrFmt.format(Math.abs(n));
}
export function signed(value) {
  const n = Number(value || 0);
  if (n === 0) return "no change";
  return (n > 0 ? "+" : "−") + "₹" + inrFmt.format(Math.abs(n));
}
export function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}
export function day(iso) {
  if (!iso) return "—";
  return new Date(iso + (iso.length === 10 ? "T00:00:00" : "")).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}
export function monthLabel(period) {
  if (!period) return "";
  const [y, m] = period.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString("en-IN", { month: "long", year: "numeric" });
}

// ---------------------------------------------------------------- hooks
export function useLoad(loader, deps) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const alive = useRef(true);
  const reload = useCallback(async (quiet) => {
    if (!quiet) setState((s) => ({ ...s, loading: true }));
    try {
      const data = await loader();
      if (alive.current) setState({ data, error: null, loading: false });
      return data;
    } catch (e) {
      if (alive.current) setState((s) => ({ ...s, error: e.message, loading: false }));
      return null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => {
    alive.current = true;
    reload();
    return () => { alive.current = false; };
  }, [reload]);
  return { ...state, reload };
}

export function usePoll(fn, active, ms = 1200) {
  useEffect(() => {
    if (!active) return undefined;
    const t = setInterval(fn, ms);
    return () => clearInterval(t);
  }, [fn, active, ms]);
}

// ---------------------------------------------------------------- toasts
let pushToast = () => {};
export function toast(message, kind = "info") { pushToast({ message, kind, id: Math.random() }); }
export function Toasts() {
  const [items, setItems] = useState([]);
  useEffect(() => {
    pushToast = (t) => {
      setItems((xs) => [...xs, t]);
      setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== t.id)), t.kind === "bad" ? 9000 : 4500);
    };
  }, []);
  return (
    <div className="toast">
      {items.map((t) => (
        <Banner key={t.id} kind={t.kind}>{t.message}</Banner>
      ))}
    </div>
  );
}

// Run an async action with a busy flag and an error toast.
export function useAction() {
  const [busy, setBusy] = useState(null);
  const run = useCallback(async (name, fn, okMessage) => {
    setBusy(name);
    try {
      const out = await fn();
      if (okMessage) toast(okMessage, "ok");
      return out;
    } catch (e) {
      toast(e.message, "bad");
      return undefined;
    } finally {
      setBusy(null);
    }
  }, []);
  return [busy, run];
}

// ---------------------------------------------------------------- components
const ICONS = { info: "ℹ", ok: "✓", warn: "!", bad: "✕", ai: "✦" };
export function Banner({ kind = "info", title, children, action }) {
  return (
    <div className={`banner ${kind}`}>
      <span className="icon">{ICONS[kind]}</span>
      <div style={{ flex: 1 }}>
        {title && <strong>{title} </strong>}
        {children}
      </div>
      {action}
    </div>
  );
}

const STATUS_KIND = {
  draft: "", calculated: "info", awaiting_approval: "warn", rejected: "bad", approved: "ok", finalized: "ok", cancelled: "",
  pending_review: "warn", approved_snapshot: "ok", ready: "info", needs_mapping: "warn", running: "info", queued: "info",
  done: "ok", failed: "bad", accepted: "ok", informational: "info", error: "bad",
};
export function StatusChip({ status, label }) {
  return <span className={`chip ${STATUS_KIND[status] ?? ""}`}>{label || status}</span>;
}

export const isModel = (provider) => provider === "anthropic" || provider === "claude-code";
export function aiName(provider) {
  return provider === "anthropic" ? "Claude (API)" : provider === "claude-code" ? "Claude (Claude Code)" : "Rule-based";
}

export function AIChip({ provider, model }) {
  if (isModel(provider)) return <span className="chip ai" title={model}>✦ {aiName(provider)}</span>;
  return <span className="chip" title="No AI model configured: a rule-based matcher made this suggestion">Rule-based</span>;
}

export function Spinner() { return <span className="spinner" aria-label="loading" />; }

export function Loading({ error }) {
  if (error) return <Banner kind="bad">{error}</Banner>;
  return <div className="empty"><Spinner /> Loading…</div>;
}

export function Field({ label, children, hint }) {
  return (
    <label className="field">
      {label}
      {children}
      {hint && <span className="muted small" style={{ fontWeight: 400 }}>{hint}</span>}
    </label>
  );
}

export function Drawer({ title, subtitle, onClose, children }) {
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <>
      <div className="drawer-back" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label={title}>
        <header>
          <div>
            <h2 style={{ margin: 0 }}>{title}</h2>
            {subtitle && <div className="muted small">{subtitle}</div>}
          </div>
          <button onClick={onClose} aria-label="Close">✕</button>
        </header>
        <div className="body">{children}</div>
      </aside>
    </>
  );
}

export function Checks({ checks }) {
  if (!checks || !checks.length) return <p className="muted">No checks.</p>;
  const order = { error: 0, warning: 1, ok: 2, info: 3 };
  return (
    <div>
      {[...checks].sort((a, b) => (order[a.level] ?? 9) - (order[b.level] ?? 9)).map((c, i) => (
        <div className="check" key={i}>
          <span className={`dot ${c.level}`} />
          <span>{c.message}</span>
        </div>
      ))}
    </div>
  );
}

export function Json({ value }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export function Tabs({ tabs, value, onChange }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map(([key, label]) => (
        <button key={key} role="tab" className={value === key ? "on" : ""} onClick={() => onChange(key)}>{label}</button>
      ))}
    </div>
  );
}

export function Stat({ label, value, delta, hint }) {
  const n = Number(delta || 0);
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {delta !== undefined && (
        <div className={`delta ${n > 0 ? "up" : n < 0 ? "down" : "muted"}`}>{signed(delta)}{hint ? ` ${hint}` : ""}</div>
      )}
    </div>
  );
}
