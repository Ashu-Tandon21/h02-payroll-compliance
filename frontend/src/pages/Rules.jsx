import React, { useState } from "react";
import { api } from "../api.js";
import { Banner, Field, Loading, Modal, day, toast, useAction, useLoad, when } from "../ui.jsx";
import { RuleForm, RuleSummary } from "./RuleForms.jsx";

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
  // A new version starts as a copy of the latest one, so only what changes needs typing.
  const newVersion = (g, extra = {}) => {
    const latest = g.versions[g.versions.length - 1];
    setEditor({
      key: g.key, name: g.title + (g.jurisdiction && g.jurisdiction !== "IN" ? ` · ${g.jurisdiction}` : ""),
      jurisdiction: g.jurisdiction, effective_from: "", title: "", note: "",
      value: structuredClone(latest ? latest.payload : g.default || data.templates[g.key] || {}), ...extra,
    });
  };
  return (
    <div>
      <div className="topbar">
        <div>
          <h1>Rules &amp; settings</h1>
          <p className="muted">Government rules and company choices are stored as dated versions. A change is a new version with a start date; old versions are never edited, so a month already paid can always be explained with the rules of its time. {data.rule_as_of}</p>
        </div>
        {canEdit && <button className="primary" onClick={() => setEditor({ key: "professional_tax", name: "Professional tax for a new state", jurisdiction: "IN-", effective_from: "", title: "", note: "", value: structuredClone(PT_TEMPLATE) })}>+ Professional tax for a state</button>}
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
        <div><h2>{g.title}{g.jurisdiction && g.jurisdiction !== "IN" ? ` · ${g.jurisdiction}` : ""}</h2></div>
        {canEdit && <button className="small" onClick={onNew}>+ New version</button>}
      </div>
      {g.versions.length === 0 && <div className="muted small">No version stored; the built-in default is used:<RuleSummary ruleKey={g.key} payload={g.default} /></div>}
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
                <button className="ghost small" onClick={() => setShow(show === v.id ? null : v.id)}>{show === v.id ? "Hide details" : "Details"}</button>
                {canEdit && !v.used_by_final_month && <button className="ghost small" disabled={!!busy} onClick={() => onRemove(v)}>Remove</button>}
              </div>
            </div>
            {show === v.id && <RuleSummary ruleKey={g.key} payload={v.payload} />}
          </div>
        );
      })}
    </div>
  );
}

function Editor({ editor, setEditor, companyId, onSaved }) {
  const [busy, act] = useAction();
  const [warnings, setWarnings] = useState([]);
  const [asJson, setAsJson] = useState(null); // text while "Edit as JSON" is open (for people who prefer it)
  const set = (k) => (e) => setEditor({ ...editor, [k]: e.target.value });
  const save = async () => {
    let payload = editor.value;
    if (asJson !== null) {
      try { payload = JSON.parse(asJson); } catch (e) { toast(`The JSON is not valid: ${e.message}`, "bad"); return; }
    }
    const { value, name, ...rest } = editor;
    const out = await act("save", () => api.post(`/companies/${companyId}/policies`, { ...rest, payload }));
    if (out) {
      setWarnings(out.warnings);
      toast("New version saved. Months before its start date are not affected.", "ok");
      if (!out.warnings.length) onSaved();
    }
  };
  const toggleJson = () => {
    if (asJson === null) { setAsJson(JSON.stringify(editor.value, null, 2)); return; }
    try { setEditor({ ...editor, value: JSON.parse(asJson) }); setAsJson(null); } catch (e) { toast(`The JSON is not valid: ${e.message}`, "bad"); }
  };
  return (
    <Modal title={`New version: ${editor.name}`} onClose={() => setEditor(null)}>
      <div className="grid3" style={{ marginBottom: 14 }}>
        {editor.key === "professional_tax" && <Field label="State code (IN- and two letters, e.g. IN-MH)"><input value={editor.jurisdiction} onChange={set("jurisdiction")} /></Field>}
        <Field label="Starts on" hint="Used by pay months that start on or after this date."><input type="date" value={editor.effective_from} onChange={set("effective_from")} /></Field>
        <Field label="Title"><input value={editor.title} onChange={set("title")} placeholder={`e.g. ${editor.name} from Aug 2025`} /></Field>
        <Field label="Note / source"><input value={editor.note} onChange={set("note")} placeholder="Notification number, circular…" /></Field>
      </div>
      {asJson === null
        ? <RuleForm ruleKey={editor.key} value={editor.value} onChange={(value) => setEditor({ ...editor, value })} />
        : <textarea rows={14} className="mono" value={asJson} onChange={(e) => setAsJson(e.target.value)} />}
      {warnings.map((w, i) => <Banner key={i} kind="warn">{w}</Banner>)}
      <div className="row" style={{ marginTop: 14 }}>
        {warnings.length ? <button className="primary" onClick={onSaved}>Done</button> : <button className="primary" disabled={!!busy || !editor.effective_from} onClick={save}>Save version</button>}
        <button onClick={() => setEditor(null)}>Cancel</button>
        <span className="spacer" />
        <button type="button" className="ghost small" onClick={toggleJson}>{asJson === null ? "Edit as JSON (advanced)" : "Back to the form"}</button>
      </div>
      {!editor.effective_from && <p className="muted small" style={{ marginTop: 6 }}>Pick a start date to save.</p>}
    </Modal>
  );
}
