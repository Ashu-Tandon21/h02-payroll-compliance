import React, { useState } from "react";
import { api } from "../api.js";
import { Banner, Field, Json, Loading, day, toast, useAction, useLoad, when } from "../ui.jsx";

const PT_TEMPLATE = {
  state_name: "New state",
  slabs: [{ min: "0", amount: "0" }, { min: "15000", amount: "150" }, { min: "25000", amount: "200" }],
  due_day_next_month: 20,
};

export default function Rules({ company, me }) {
  const { data, error, reload } = useLoad(() => api.get(`/companies/${company.id}/policies`), [company.id]);
  const [editor, setEditor] = useState(null);
  const [busy, act] = useAction();
  if (!data) return <Loading error={error} />;
  const canEdit = me.roles.length > 0;
  const statutory = data.groups.filter((g) => g.scope === "statutory");
  const settings = data.groups.filter((g) => g.scope !== "statutory");
  const remove = async (v) => {
    if (!window.confirm(`Remove the version of ${v.key} that starts ${v.effective_from}?`)) return;
    if (await act("del", () => api.del(`/policies/${v.id}`), "Version removed") !== undefined) reload(true);
  };
  const newVersion = (g, extra = {}) => {
    const latest = g.versions[g.versions.length - 1];
    setEditor({
      key: g.key, jurisdiction: g.jurisdiction, effective_from: "", title: "", note: "",
      payload: JSON.stringify(latest ? latest.payload : g.default || data.templates[g.key] || {}, null, 2), ...extra,
    });
  };
  return (
    <div>
      <div className="topbar">
        <div>
          <h1>Rules &amp; settings</h1>
          <p className="muted">Government rules and company choices are stored as dated versions. A change is a new version with a start date; old versions are never edited, so a month already paid can always be explained with the rules of its time. {data.rule_as_of}</p>
        </div>
        {canEdit && <button className="primary" onClick={() => setEditor({ key: "professional_tax", jurisdiction: "IN-", effective_from: "", title: "", note: "", payload: JSON.stringify(PT_TEMPLATE, null, 2) })}>+ Professional tax for a state</button>}
      </div>
      {editor && <Editor editor={editor} setEditor={setEditor} companyId={company.id} onSaved={() => { setEditor(null); reload(true); }} />}
      <AISwitch company={company} canEdit={me.roles.includes("maker")} />
      <h2 style={{ marginTop: 10 }}>Government rules</h2>
      <div className="grid2">{statutory.map((g) => <Group key={`${g.key}${g.jurisdiction}`} g={g} canEdit={canEdit} onNew={() => newVersion(g)} onRemove={remove} busy={busy} />)}</div>
      <h2 style={{ marginTop: 24 }}>Company settings — {company.name}</h2>
      <div className="grid2">{settings.map((g) => <Group key={g.key} g={g} canEdit={canEdit} onNew={() => newVersion(g)} onRemove={remove} busy={busy} />)}</div>
    </div>
  );
}

// Whether this company's HR data may be sent to an AI model. Known HR tools never need it.
function AISwitch({ company, canEdit }) {
  const { data, error, reload } = useLoad(() => api.get(`/companies/${company.id}`), [company.id]);
  const [busy, act] = useAction();
  if (!data) return <Loading error={error} />;
  const on = data.ai_mode === "allowed";
  const set = async (mode) => {
    if (await act("ai", () => api.patch(`/companies/${company.id}`, { ai_mode: mode }), mode === "off" ? "AI turned off for this company" : "AI allowed for this company")) reload(true);
  };
  return (
    <div className="card" style={{ marginTop: 10 }}>
      <div className="card-head">
        <div>
          <h2>AI for {company.name}: {on ? "allowed" : "off"}</h2>
          <p className="muted small" style={{ margin: "4px 0 0" }}>
            {on
              ? `Used only where the system has no answer of its own: an HR tool or file layout it does not know yet, reading a screen with no table, plain-English instructions and change summaries. Field mapping sends column names and masked sample values. Model: ${data.ai.model}.`
              : "Nothing is sent to an AI model. Known HR tools are still mapped from their profiles; anything else is suggested by the rule-based matcher and checked by a person."}
            {data.ai_mode_changed_by && ` Changed by ${data.ai_mode_changed_by.name} on ${when(data.ai_mode_changed_at)}.`}
          </p>
        </div>
        {canEdit && <button className="small" disabled={!!busy} onClick={() => set(on ? "off" : "allowed")}>{on ? "Turn AI off" : "Allow AI"}</button>}
      </div>
    </div>
  );
}

function Group({ g, canEdit, onNew, onRemove, busy }) {
  const [show, setShow] = useState(null);
  const today = new Date().toISOString().slice(0, 10);
  return (
    <div className="card">
      <div className="card-head">
        <div><h2>{g.title}{g.jurisdiction && g.jurisdiction !== "IN" ? ` · ${g.jurisdiction}` : ""}</h2><div className="muted small mono">{g.key}</div></div>
        {canEdit && <button className="small" onClick={onNew}>+ New version</button>}
      </div>
      {g.versions.length === 0 && <p className="muted small">No version stored; the built-in default is used: <code>{JSON.stringify(g.default)}</code></p>}
      {g.versions.map((v) => {
        const current = v.id === g.current_version_id;
        const future = v.effective_from > today;
        return (
          <div key={v.id} className={`version ${current ? "current" : future ? "future" : ""}`}>
            <div className="row between">
              <div>
                <b>From {day(v.effective_from)}</b> {current && <span className="chip ok">in force now</span>}{future && <span className="chip info">starts later</span>}
                {v.used_by_final_month && <span className="chip" title="Used by a submitted or final month; cannot be removed">🔒 used</span>}
                <div className="small">{v.title}</div>
                {v.note && <div className="muted small">{v.note}</div>}
                <div className="muted small">#{v.id} · added {when(v.created_at)}{v.created_by ? ` by ${v.created_by.name}` : " at setup"}</div>
              </div>
              <div className="row">
                <button className="ghost small" onClick={() => setShow(show === v.id ? null : v.id)}>{show === v.id ? "Hide" : "Data"}</button>
                {canEdit && !v.used_by_final_month && <button className="ghost small" disabled={!!busy} onClick={() => onRemove(v)}>Remove</button>}
              </div>
            </div>
            {show === v.id && <Json value={v.payload} />}
          </div>
        );
      })}
    </div>
  );
}

function Editor({ editor, setEditor, companyId, onSaved }) {
  const [busy, act] = useAction();
  const [warnings, setWarnings] = useState([]);
  const set = (k) => (e) => setEditor({ ...editor, [k]: e.target.value });
  const save = async () => {
    const out = await act("save", () => api.post(`/companies/${companyId}/policies`, editor));
    if (out) {
      setWarnings(out.warnings);
      toast("New version saved. Months before its start date are not affected.", "ok");
      if (!out.warnings.length) onSaved();
    }
  };
  return (
    <div className="card" style={{ marginBottom: 16, borderColor: "#b8d0ea" }}>
      <h2>New version of <span className="mono">{editor.key}</span></h2>
      <div className="grid2">
        {editor.key === "professional_tax" && <Field label="State (IN- plus two-letter code, e.g. IN-MH)"><input value={editor.jurisdiction} onChange={set("jurisdiction")} /></Field>}
        <Field label="Starts on" hint="Picked up by payroll months starting on or after the 1st of this month."><input type="date" value={editor.effective_from} onChange={set("effective_from")} /></Field>
        <Field label="Title"><input value={editor.title} onChange={set("title")} placeholder="e.g. Karnataka PT from Aug 2025" /></Field>
        <Field label="Note / source"><input value={editor.note} onChange={set("note")} placeholder="Notification number, circular…" /></Field>
      </div>
      <Field label="Rule data (JSON)" hint={editor.key === "professional_tax" ? 'Slabs: {"min": 25000, "amount": 200} means ₹200 when pay is ₹25,000 or more. Optional: "above" (strictly more), "max", "gender": "F"/"M", "months": [2], or "rate" instead of "amount".' : "Checked before it is saved."}>
        <textarea rows={14} className="mono" value={editor.payload} onChange={set("payload")} />
      </Field>
      {warnings.map((w, i) => <Banner key={i} kind="warn">{w}</Banner>)}
      <div className="row" style={{ marginTop: 10 }}>
        {warnings.length ? <button className="primary" onClick={onSaved}>Done</button> : <button className="primary" disabled={!!busy || !editor.effective_from} onClick={save}>Save version</button>}
        <button onClick={() => setEditor(null)}>Cancel</button>
      </div>
    </div>
  );
}
