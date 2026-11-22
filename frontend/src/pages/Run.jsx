import React, { useCallback, useMemo, useState } from "react";
import { api } from "../api.js";
import { go } from "../App.jsx";
import {
  AIChip, Banner, Drawer, Field, Loading, Stat, StatusChip, day, inr, isModel, signed, toast, useAction, useLoad, usePoll, when,
} from "../ui.jsx";

const SCOPE_LABEL = { statutory: "Government rule", company: "Company setting", override: "What-if value", default: "Built-in default" };

export default function RunPage({ runId, me }) {
  const { data: run, error, reload } = useLoad(() => api.get(`/runs/${runId}`), [runId]);
  const [waitingFor, setWaitingFor] = useState(null); // {from: status, until: time}
  const [busy, act] = useAction();
  const [drawer, setDrawer] = useState(null);
  const poll = useCallback(async () => {
    const r = await reload(true);
    if (r && waitingFor && (r.status !== waitingFor.from || r.calculation_count !== waitingFor.count || r.last_error !== waitingFor.err || Date.now() > waitingFor.until)) {
      setWaitingFor(null);
      if (r.last_error && r.last_error !== waitingFor.err) toast(r.last_error, "bad");
    }
  }, [reload, waitingFor]);
  usePoll(poll, !!waitingFor, 900);

  if (!run) return <Loading error={error} />;
  const isMaker = me.roles.includes("maker");
  const isChecker = me.roles.includes("checker");
  const draft = run.run_type === "draft";

  const doAction = async (action, body = {}, ok) => {
    const out = await act(action, () => api.post(`/runs/${run.id}/${action}`, body), ok);
    if (!out) return;
    if (out.outcome?.queued) {
      setWaitingFor({ from: run.status, count: run.calculation_count, err: run.last_error, until: Date.now() + 30000 });
      toast("Sent to the workflow. This page updates when it is done.", "info");
    }
    await reload(true);
  };

  const employees = run.result?.employees || [];
  const changedCodes = new Set((run.comparison?.employees || []).filter((e) => e.status !== "unchanged").map((e) => e.code));
  const openExplain = (code, focus) => setDrawer({ code, focus });

  return (
    <div>
      <div className="topbar">
        <div>
          <a href="#/months" className="small">← Payroll months</a>
          <h1>{run.period_label}{draft && <span className="muted"> · draft re-run #{run.id}</span>}</h1>
          <div className="row">
            <StatusChip status={run.status} label={run.status_label} />
            <WorkflowBadge wf={run.workflow} />
            {run.result_hash && <span className="hash" title="Fingerprint of the result. Same inputs always give the same fingerprint.">result {run.result_hash.slice(0, 10)}</span>}
            {waitingFor && <span className="row small muted"><span className="spinner" /> waiting for the workflow…</span>}
          </div>
        </div>
        <div className="row">
          <button onClick={() => reload(true)}>Refresh</button>
        </div>
      </div>

      {draft && run.base_run && (
        <Banner kind="info" title="This is a what-if.">
          It re-runs {run.base_run.label} (#{run.base_run.id}, {run.base_run.status_label.toLowerCase()}) with today's rules
          {Object.keys(run.policy_overrides || {}).length ? " and these what-if settings" : ""}. The original month is not changed. <a href={`#/runs/${run.base_run.id}`}>Open the original</a>.
          {Object.entries(run.policy_overrides || {}).map(([key, values]) => (
            <div key={key} className="small" style={{ marginTop: 4 }}>
              <span className="chip warn">what-if</span> {key}: {Object.entries(values).map(([k, v]) => `${k} = ${v}`).join(", ")}
            </div>
          ))}
        </Banner>
      )}
      {!draft && <Stepper steps={run.steps} />}
      <NextAction run={run} me={me} />
      {run.last_error && !["finalized"].includes(run.status) && <Banner kind="bad" title="Last action was refused:">{run.last_error}</Banner>}
      {run.newer_hr_data && (
        <Banner kind="warn" title="HR data changed after this month was prepared."
          action={run.can.use_latest_data && <button onClick={() => doAction("use-latest-data", {}, "Now using the latest HR data")}>Use the new data</button>}>
          Data #{run.newer_hr_data.id} ({run.newer_hr_data.source_system}, {when(run.newer_hr_data.reviewed_at)}) differs for this month. It cannot be approved until it is recalculated with the new data.
        </Banner>
      )}
      {run.freshness && !run.freshness.fresh && !run.newer_hr_data && (
        <Banner kind="warn" title="Needs to be calculated again:">{run.freshness.reasons.join("; ")}.</Banner>
      )}
      {run.four_eyes_note && <Banner kind="warn">{run.four_eyes_note}</Banner>}
      {(run.result?.issues || []).filter((i) => i.level !== "info").map((i, k) => (
        <Banner key={k} kind={i.level === "error" ? "bad" : "warn"} title={`${employees.find((e) => e.code === i.employee)?.name || i.employee}:`}>{i.message}</Banner>
      ))}

      {!draft && <DataSection run={run} busy={busy} onAction={doAction} />}
      {!draft && <ChangesSection run={run} onChange={() => reload(true)} />}

      {run.result ? (
        <>
          <Comparison run={run} onExplain={openExplain} />
          <Register run={run} changedCodes={changedCodes} onExplain={openExplain} />
        </>
      ) : (
        <div className="card" style={{ marginTop: 20 }}>
          <div className="empty">
            Not calculated yet.
            {run.can.calculate && <div style={{ marginTop: 10 }}><button className="primary" disabled={!!busy || !!waitingFor} onClick={() => doAction("calculate")}>Calculate {run.period_label}</button></div>}
          </div>
        </div>
      )}

      {!draft && <ApprovalSection run={run} busy={busy || waitingFor} onAction={doAction} isChecker={isChecker} isMaker={isMaker} />}
      {!draft && <FilesSection run={run} />}
      {run.result && <WhatIf run={run} me={me} />}
      {run.result && <Repeatable run={run} />}
      <Events run={run} />

      {drawer && (
        <ExplainDrawer
          run={run}
          employee={employees.find((e) => e.code === drawer.code)}
          focus={drawer.focus}
          comparison={run.comparison?.employees.find((e) => e.code === drawer.code)}
          onClose={() => setDrawer(null)}
        />
      )}
    </div>
  );
}

function WorkflowBadge({ wf }) {
  if (!wf) return null;
  if (wf.backend === "temporal") {
    const state = wf.state;
    const text = wf.reachable === false ? "Temporal not reachable" : `Temporal workflow ${wf.status ? wf.status.toLowerCase() : ""}`;
    return (
      <a className={`chip ${wf.reachable === false ? "bad" : "brand"}`} href={wf.temporal_ui || "#"} target="_blank" rel="noreferrer"
        title={`${wf.workflow_id}${state ? ` · ${state.pending_commands} pending` : ""}`}>⟳ {text}</a>
    );
  }
  if (wf.backend === "none") return <span className="chip">what-if (no workflow)</span>;
  return <span className="chip" title="Steps run in-process; state is kept in the database">local workflow</span>;
}

function Stepper({ steps }) {
  if (!steps?.length) return null;
  const current = steps.findIndex((s) => !s.done);
  return (
    <div className="steps">
      {steps.map((s, i) => (
        <div key={s.key} className={`step ${s.done ? "done" : ""} ${i === current ? "current" : ""}`}>
          <span className="b">{s.done ? "✓" : i + 1}</span>{s.label}
        </div>
      ))}
    </div>
  );
}

function NextAction({ run, me }) {
  const isMaker = me.roles.includes("maker");
  const isChecker = me.roles.includes("checker");
  let text = null;
  let kind = "info";
  if (run.run_type === "draft") return null;
  if (run.status === "finalized") { text = `Final. Approved by ${run.decided_by?.name} on ${when(run.decided_at)}. The register and payment files are below.`; kind = "ok"; }
  else if (run.status === "awaiting_approval") {
    text = isChecker && !run.four_eyes_note ? "Waiting for you. Check what changed and approve, or send it back with a reason."
      : `Submitted by ${run.submitted_by?.name} on ${when(run.submitted_at)}. Waiting for a checker. It keeps waiting through restarts.`;
    kind = "warn";
  } else if (run.status === "rejected") { text = `Sent back by ${run.decided_by?.name}: "${run.decision_note}". Make the corrections, calculate and submit again.`; kind = "bad"; }
  else if (isMaker) {
    if (!run.data_reviewed_at) text = "Step 1: review the HR data that was read, then mark it as reviewed.";
    else if (run.pending_changes) text = "Confirm or discard the proposed changes.";
    else if (run.status === "draft") text = "Calculate the month. You can add changes in plain English first.";
    else if (run.status === "calculated") text = "Check what changed and why, then submit for approval.";
  } else text = "This month is being prepared by a maker.";
  return text ? <Banner kind={kind}>{text}</Banner> : null;
}

function SectionTitle({ n, title, right }) {
  return (
    <div className="section-title">
      {n && <span className="n">{n}</span>}
      <h2>{title}</h2>
      <span className="spacer" />
      {right}
    </div>
  );
}

// ------------------------------------------------------------------ 1. data
function DataSection({ run, busy, onAction }) {
  const s = run.snapshot;
  return (
    <>
      <SectionTitle n="1" title="HR data" />
      <div className="card row between">
        <div>
          <b>Data #{s.id}</b> from {s.source_system} · {s.employee_count} employees · read {when(s.created_at)}
          <div className="muted small">
            Salary changes recorded up to {day(run.knowledge_date)} are used.{" "}
            {s.errors ? <span className="chip bad">{s.errors} errors</span> : s.warnings ? <span className="chip warn">{s.warnings} warnings</span> : <span className="chip ok">all checks passed</span>}
          </div>
        </div>
        <div className="row">
          <button onClick={() => go(`/data/snapshot/${s.id}`)}>See the data</button>
          {run.data_reviewed_at
            ? <span className="chip ok">✓ Reviewed by {run.data_reviewed_by?.name}</span>
            : run.can.review_data && <button className="primary" disabled={!!busy} onClick={() => onAction("review-data", {}, "Data marked as reviewed")}>I have reviewed the data</button>}
        </div>
      </div>
    </>
  );
}

// ------------------------------------------------------------------ 2. changes
function ChangesSection({ run, onChange }) {
  const [text, setText] = useState("");
  const [busy, act] = useAction();
  const propose = async (e) => {
    e?.preventDefault();
    const ch = await act("propose", () => api.post(`/runs/${run.id}/instructions`, { text }));
    if (ch) { setText(""); onChange(); }
  };
  const decide = async (id, decision) => {
    if (await act(decision, () => api.post(`/changes/${id}/${decision}`), decision === "confirm" ? "Change confirmed. Calculate again to include it." : "Change discarded")) onChange();
  };
  const ask = async (c) => {
    const ch = await act("again", () => api.post(`/runs/${run.id}/instructions`, { text: c.text, force: true }));
    if (ch) { await api.post(`/changes/${c.id}/discard`).catch(() => {}); onChange(); }
  };
  return (
    <>
      <SectionTitle n="2" title="Changes this month" />
      <div className="card">
        {run.can.add_changes && (
          <form className="instruction" onSubmit={propose}>
            <input placeholder='Type a change, e.g. "Give Priya a ₹10,000 one-time bonus this month"' value={text} onChange={(e) => setText(e.target.value)} />
            <button className="primary" disabled={!text.trim() || !!busy}>{busy === "propose" ? "Reading…" : "Check what it means"}</button>
          </form>
        )}
        <p className="muted small" style={{ marginTop: 6 }}>The AI only reads the sentence and proposes a change. You confirm it; the calculation works out every number.</p>
        {run.changes.length === 0 && <p className="muted">No changes typed for this month.</p>}
        {run.changes.map((c) => (
          <div key={c.id} className="understood">
            <div className="row between">
              <span className="muted small">“{c.text}” · {c.created_by?.name} · {when(c.created_at)}</span>
              <span className="row">{c.ai && <AIChip provider={c.ai.provider} model={c.ai.model} />}{c.ai?.reused_saved_answer && <span className="chip">saved answer reused</span>}
                <StatusChip status={c.status === "confirmed" ? "accepted" : "pending_review"} label={c.status === "confirmed" ? "Confirmed" : "Proposed"} /></span>
            </div>
            <div className="muted small" style={{ marginTop: 6 }}>The system understood:</div>
            {c.proposal.items.map((it, i) => <div key={i} className="item">{it.summary}</div>)}
            {c.proposal.problems.map((p, i) => <Banner key={i} kind="bad">{p}</Banner>)}
            {(c.proposal.notes || []).map((n, i) => <div key={i} className="muted small" style={{ marginBottom: 6 }}>Note from the AI: {n}</div>)}
            {c.status === "proposed" && run.can.add_changes && (
              <div className="row">
                <button className="good" disabled={!!busy || c.proposal.problems.length > 0} onClick={() => decide(c.id, "confirm")}>Yes, that's right</button>
                <button className="danger" disabled={!!busy} onClick={() => decide(c.id, "discard")}>Discard</button>
                {isModel(c.ai?.provider) && <button className="ghost small" disabled={!!busy} onClick={() => ask(c)}>Ask the AI again</button>}
              </div>
            )}
            {c.status === "confirmed" && <div className="muted small">Confirmed by {c.decided_by?.name} · {when(c.decided_at)}{run.can.add_changes && <> · <button className="ghost small" onClick={() => decide(c.id, "discard")}>remove</button></>}</div>}
          </div>
        ))}
      </div>
    </>
  );
}

// ------------------------------------------------------------------ 3. what changed
function Comparison({ run, onExplain }) {
  const cmp = run.comparison;
  const [ai, setAi] = useState(null);
  const [busy, act] = useAction();
  const [open, setOpen] = useState({});
  const t = (code) => cmp?.totals.find((x) => x.code === code);
  const explain = async (force) => {
    const out = await act("explain", () => api.post(`/runs/${run.id}/explain`, { force }));
    if (out) setAi(out);
  };
  if (!cmp) {
    return (
      <>
        <SectionTitle n="3" title="Summary" />
        <div className="grid4">
          <Stat label="Gross pay" value={inr(run.result.totals.gross)} />
          <Stat label="Deductions" value={inr(run.result.totals.total_deductions)} />
          <Stat label="Net pay" value={inr(run.result.totals.net_pay)} />
          <Stat label="Cost to company" value={inr(run.result.totals.employer_cost)} />
        </div>
        <p className="muted small" style={{ marginTop: 8 }}>No earlier month to compare with.</p>
      </>
    );
  }
  const changed = cmp.employees.filter((e) => e.status !== "unchanged");
  const label = run.run_type === "draft" ? `What changes against ${cmp.before_label}` : `What changed since ${cmp.before_label}`;
  return (
    <>
      <SectionTitle n="3" title={label} right={<span className="muted small">{changed.length} of {cmp.employees.length} employees paid differently</span>} />
      <div className="grid4">
        {["gross", "total_deductions", "net_pay", "employer_cost"].map((c) => (
          <Stat key={c} label={{ gross: "Gross pay", total_deductions: "Deductions", net_pay: "Net pay", employer_cost: "Cost to company" }[c]}
            value={inr(t(c)?.after)} delta={t(c)?.delta} hint={`vs ${cmp.before_label}`} />
        ))}
      </div>
      {cmp.rule_changes.length > 0 && (
        <Banner kind="info" title="Rules or settings differ:">
          {cmp.rule_changes.map((r, i) => (
            <span key={i}>{i ? "; " : ""}{r.title}{r.jurisdiction ? ` (${r.jurisdiction})` : ""}: {r.before ? `from ${r.before.effective_from || "default"}` : "none"} → {r.after ? (r.after.scope === "override" ? "what-if value" : `version from ${r.after.effective_from}`) : "none"}</span>
          ))}
        </Banner>
      )}
      <div className="card" style={{ marginTop: 12 }}>
        {changed.length === 0 && <div className="empty">Nobody's pay changed.</div>}
        {changed.map((e) => (
          <div key={e.code} className="diffrow">
            <div>
              <div className="row"><b>{e.name}</b><span className="muted small">{e.code}</span></div>
              <ul className="drivers">{e.drivers.map((d, i) => <li key={i}>{d.text}</li>)}</ul>
              <button className="ghost small" onClick={() => setOpen({ ...open, [e.code]: !open[e.code] })}>{open[e.code] ? "Hide" : "Show"} the {e.changes.length} amounts that moved</button>
              {open[e.code] && (
                <div className="table-wrap" style={{ marginTop: 6 }}>
                  <table>
                    <thead><tr><th>Amount</th><th className="num">{cmp.before_label}</th><th className="num">{cmp.after_label}</th><th className="num">Change</th><th>How it is worked out now</th></tr></thead>
                    <tbody>{e.changes.map((c) => (
                      <tr key={c.code}>
                        <td className="click" onClick={() => onExplain(e.code, c.code)}>{c.label}</td>
                        <td className="num">{c.code === "unpaid_days" ? c.before : inr(c.before)}</td>
                        <td className="num">{c.code === "unpaid_days" ? c.after : inr(c.after)}</td>
                        <td className={`num ${Number(c.delta) > 0 ? "up" : "down"}`}>{c.code === "unpaid_days" ? c.delta : signed(c.delta)}</td>
                        <td className="small mono">{c.formula_after}</td>
                      </tr>))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
            <div style={{ textAlign: "right" }}>
              <div className="muted small">Net pay</div>
              <div className="num">{inr(e.net_before)} → <b>{inr(e.net_after)}</b></div>
              <div className={`small ${Number(e.net_delta) > 0 ? "up" : "down"}`}>{signed(e.net_delta)}</div>
              <button className="small" style={{ marginTop: 6 }} onClick={() => onExplain(e.code, "net_pay")}>Why? Line by line</button>
            </div>
          </div>
        ))}
      </div>
      <div className="card">
        <div className="row between">
          <div><b>In plain words</b> <span className="muted small">An AI writes this from the comparison above. It changes nothing, and every number in it is checked against the calculation.</span></div>
          <button disabled={!!busy} onClick={() => explain(false)}>{busy ? "Writing…" : ai ? "Show saved summary" : "Explain the changes"}</button>
        </div>
        {ai && (
          <div style={{ marginTop: 10 }}>
            <Banner kind="ai" title={ai.response.headline} action={<AIChip provider={ai.provider} model={ai.model} />}>
              <ul className="drivers">{ai.response.points.map((p, i) => <li key={i}>{p}</li>)}</ul>
            </Banner>
            {ai.validation.map((v, i) => <div key={i} className={`small ${v.level === "ok" ? "up" : "down"}`}>{v.level === "ok" ? "✓ " : "⚠ "}{v.message}</div>)}
            <div className="muted small">Saved as AI answer #{ai.id}{ai.reused_saved_answer ? " (reused, the AI was not asked again)" : ""}. {isModel(ai.provider) && <button className="ghost small" onClick={() => explain(true)}>Ask again</button>}</div>
          </div>
        )}
      </div>
    </>
  );
}

// ------------------------------------------------------------------ 4. register
const REG_COLS = [
  ["unpaid_days", "Unpaid days", false],
  ["fixed_gross", "Monthly salary"],
  ["lop_deduction", "Unpaid leave cut"],
  ["arrears", "Arrears"],
  ["_one_time", "One-time"],
  ["gross", "Gross pay"],
  ["pf_employee", "PF"],
  ["esi_employee", "ESI"],
  ["pt", "Prof. tax"],
  ["tds", "TDS"],
  ["_other_ded", "Other ded."],
  ["net_pay", "Net pay"],
  ["employer_cost", "Cost to co."],
];

function cellValue(e, key) {
  if (key === "_one_time") return Object.entries(e.amounts).filter(([k]) => k.startsWith("one_time.")).reduce((a, [, v]) => a + Number(v), 0);
  if (key === "_other_ded") return Object.entries(e.amounts).filter(([k]) => k.startsWith("deduction.")).reduce((a, [, v]) => a + Number(v), 0);
  return e.amounts[key];
}
function focusFor(e, key) {
  if (key === "_one_time") return Object.keys(e.amounts).find((k) => k.startsWith("one_time.")) || "gross";
  if (key === "_other_ded") return Object.keys(e.amounts).find((k) => k.startsWith("deduction.")) || "total_deductions";
  if (key === "unpaid_days" || key === "fixed_gross") return "lop_deduction";
  return key;
}

function Register({ run, changedCodes, onExplain }) {
  const emps = run.result.employees;
  const totals = run.result.totals;
  const showOther = emps.some((e) => Number(cellValue(e, "_other_ded")));
  const cols = REG_COLS.filter(([k]) => k !== "_other_ded" || showOther);
  return (
    <>
      <SectionTitle n="4" title="Payroll register" right={<span className="muted small">Click any amount to see why it is what it is.</span>} />
      <div className="table-wrap">
        <table>
          <thead><tr><th>Employee</th>{cols.map(([k, l]) => <th key={k} className="num">{l}</th>)}</tr></thead>
          <tbody>
            {emps.map((e) => (
              <tr key={e.code} className={changedCodes.has(e.code) ? "changed" : ""}>
                <td><b>{e.name}</b><div className="muted small">{e.code} · {e.work_state}</div></td>
                {cols.map(([k, , money]) => {
                  const v = cellValue(e, k);
                  const zero = !Number(v);
                  return (
                    <td key={k} className={`num click ${zero ? "muted" : ""}`} onClick={() => onExplain(e.code, focusFor(e, k))} title="Why?">
                      {money === false ? (zero ? "—" : v) : zero ? "—" : inr(v)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td>Total ({emps.length})</td>
              {cols.map(([k, , money]) => {
                let v = totals[k];
                if (k === "_one_time") v = Object.entries(totals).filter(([x]) => x.startsWith("one_time.")).reduce((a, [, y]) => a + Number(y), 0);
                if (k === "_other_ded") v = Object.entries(totals).filter(([x]) => x.startsWith("deduction.")).reduce((a, [, y]) => a + Number(y), 0);
                if (k === "unpaid_days") v = emps.reduce((a, e) => a + Number(e.amounts.unpaid_days || 0), 0);
                return <td key={k} className="num">{money === false ? v : inr(v)}</td>;
              })}
            </tr>
          </tfoot>
        </table>
      </div>
      <p className="muted small" style={{ marginTop: 6 }}>
        PF and ESI shown are the employee's share; the company's share is in the cost to company. <a href={`/api/runs/${run.id}/export?file=register.csv`}>Download this register (CSV)</a>
      </p>
    </>
  );
}

// ------------------------------------------------------------------ explain drawer
function ExplainDrawer({ run, employee, focus, comparison, onClose }) {
  if (!employee) return null;
  const lines = employee.lines;
  const groups = [
    ["Earnings", lines.filter((l) => l.category === "earning" || ["lop_deduction", "gross"].includes(l.code))],
    ["Deductions", lines.filter((l) => l.category === "deduction" || ["total_deductions", "net_pay"].includes(l.code))],
    ["Paid by the company", lines.filter((l) => l.category === "employer" || l.code === "employer_cost")],
  ];
  return (
    <Drawer title={`${employee.name} · ${run.period_label}`} subtitle={`${employee.code} · net pay ${inr(employee.amounts.net_pay)} · every line below is worked out by the calculation, never by AI`} onClose={onClose}>
      {comparison && comparison.status !== "unchanged" && (
        <Banner kind="info" title={`Why it changed since ${run.comparison.before_label} (${signed(comparison.net_delta)} net):`}>
          <ul className="drivers">{comparison.drivers.map((d, i) => <li key={i}>{d.text}</li>)}</ul>
        </Banner>
      )}
      {employee.issues.map((i, k) => <Banner key={k} kind={i.level === "error" ? "bad" : i.level === "warning" ? "warn" : "info"}>{i.message}</Banner>)}
      {groups.map(([title, ls]) => (
        <div key={title}>
          <div className="group-label">{title}</div>
          {ls.map((l) => <LineItem key={l.code} line={l} focus={focus} change={comparison?.changes.find((c) => c.code === l.code)} before={run.comparison?.before_label} />)}
        </div>
      ))}
    </Drawer>
  );
}

function LineItem({ line, focus, change, before }) {
  const isFocus = line.code === focus || line.children?.some((c) => c.code === focus);
  const [open, setOpen] = useState(isFocus || ["net_pay"].includes(line.code));
  const total = ["gross", "net_pay", "total_deductions", "employer_cost"].includes(line.code);
  return (
    <div className={`line ${isFocus ? "focus" : ""}`} ref={(el) => isFocus && el && el.scrollIntoView({ block: "nearest" })}>
      <div className="lh" onClick={() => setOpen(!open)}>
        <span>{open ? "▾" : "▸"} {total ? <b>{line.label}</b> : line.label}
          {change && <span className={`chip ${Number(change.delta) > 0 ? "ok" : "bad"}`} style={{ marginLeft: 6 }}>{signed(change.delta)} vs {before}</span>}
        </span>
        <span className="num">{total ? <b>{inr(line.amount)}</b> : inr(line.amount)}</span>
      </div>
      {open && (
        <div className="lb">
          {line.formula && <div className="formula">{line.formula}</div>}
          {line.reason && <p>{line.reason}</p>}
          {change && change.formula_before && <p className="muted small">In {before}: <span className="mono">{change.formula_before}</span></p>}
          {line.rules?.length > 0 && (
            <div className="rules">
              {line.rules.map((r, i) => (
                <span key={i} className={`chip ${r.scope === "override" ? "warn" : r.scope === "statutory" ? "brand" : "info"}`}
                  title={`Version ${r.version_id}`}>
                  {SCOPE_LABEL[r.scope] || r.scope}: {r.title}{r.jurisdiction && r.jurisdiction !== "IN" ? ` (${r.jurisdiction})` : ""}{r.effective_from ? ` · from ${day(r.effective_from)}` : ""}
                </span>
              ))}
            </div>
          )}
          {line.children?.length > 0 && (
            <div className="children">
              {line.children.map((c) => (
                <div key={c.code} className={c.code === focus ? "line focus" : ""} style={c.code === focus ? { padding: 6 } : {}}>
                  <div className="crow"><span>{c.label}</span><span className="num">{inr(c.amount)}</span></div>
                  {c.formula && <div className="small mono muted">{c.formula}</div>}
                  {c.reason && <div className="small muted">{c.reason}</div>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ 5. approval
function ApprovalSection({ run, busy, onAction, isChecker, isMaker }) {
  const [note, setNote] = useState("");
  const t = run.result?.totals || {};
  const same = run.submitted_result_hash && run.submitted_result_hash === run.result_hash;
  return (
    <>
      <SectionTitle n="5" title="Approval" />
      <div className="card">
        <div className="grid2">
          <div>
            <h3>What is being approved</h3>
            {run.result ? (
              <dl className="kv">
                <dt>Employees</dt><dd>{run.result.employee_count}</dd>
                <dt>Gross pay</dt><dd>{inr(t.gross)}</dd>
                <dt>Deductions</dt><dd>{inr(t.total_deductions)} (PF {inr(t.pf_employee)}, ESI {inr(t.esi_employee)}, PT {inr(t.pt)}, TDS {inr(t.tds)})</dd>
                <dt>Net pay</dt><dd><b>{inr(t.net_pay)}</b></dd>
                <dt>Company PF + ESI</dt><dd>{inr(Number(t.pf_employer || 0) + Number(t.esi_employer || 0))}</dd>
                <dt>Result fingerprint</dt><dd><span className="hash">{run.result_hash.slice(0, 16)}</span></dd>
              </dl>
            ) : <p className="muted">Nothing calculated yet.</p>}
          </div>
          <div>
            <h3>Maker and checker</h3>
            <dl className="kv">
              <dt>Prepared by</dt><dd>{run.calculated_by?.name || "—"} {run.calculation_count > 1 && <span className="muted small">({run.calculation_count} calculations)</span>}</dd>
              <dt>Submitted</dt><dd>{run.submitted_by ? `${run.submitted_by.name}, ${when(run.submitted_at)}` : "—"}</dd>
              <dt>Decision</dt><dd>{run.decided_by ? `${run.status === "rejected" ? "Sent back" : "Approved"} by ${run.decided_by.name}, ${when(run.decided_at)}` : "—"}</dd>
              {run.submitted_result_hash && <><dt>Unchanged since submitted</dt><dd>{same ? <span className="chip ok">✓ same result the maker submitted</span> : <span className="chip bad">result differs from submission</span>}</dd></>}
              {run.freshness && <><dt>Inputs still current</dt><dd>{run.freshness.fresh ? <span className="chip ok">✓ no data, change or rule moved</span> : <span className="chip warn">{run.freshness.reasons.join("; ")}</span>}</dd></>}
            </dl>
          </div>
        </div>
        <div className="row" style={{ marginTop: 14 }}>
          {isMaker && run.can.calculate && <button disabled={!!busy} onClick={() => onAction("calculate")}>{run.result ? "Calculate again" : "Calculate"}</button>}
          {isMaker && run.can.submit && <button className="primary" disabled={!!busy || (run.freshness && !run.freshness.fresh)} onClick={() => onAction("submit", {}, null)}>Submit for approval</button>}
          {isMaker && run.can.cancel && <button className="ghost" disabled={!!busy} onClick={() => window.confirm("Cancel this month? You can open it again later.") && onAction("cancel")}>Cancel month</button>}
          {isChecker && run.status === "awaiting_approval" && (
            <>
              <input style={{ flex: 1, minWidth: 220 }} placeholder="Note (required to send back)" value={note} onChange={(e) => setNote(e.target.value)} />
              <button className="danger" disabled={!!busy || !run.can.reject || !note.trim()} onClick={() => onAction("reject", { note })}>Send back</button>
              <button className="good" disabled={!!busy || !run.can.approve} onClick={() => onAction("approve", { note })}>Approve {run.period_label}</button>
            </>
          )}
        </div>
        {run.status === "awaiting_approval" && !isChecker && <p className="muted small" style={{ marginTop: 8 }}>Sign in as a checker (e.g. <b>checker</b>) to approve.</p>}
      </div>
    </>
  );
}

// ------------------------------------------------------------------ 6. files
function FilesSection({ run }) {
  const files = run.files?.files;
  return (
    <>
      <SectionTitle n="6" title="Register and payment files" />
      <div className="card">
        {!files ? (
          <p className="muted">Written when the checker approves. Until then you can download a draft register from the table above.</p>
        ) : (
          <div className="grid2">
            <div>
              <h3>Files</h3>
              <div className="table-wrap"><table>
                <thead><tr><th>File</th><th className="num">Rows</th><th>Checksum</th></tr></thead>
                <tbody>{Object.entries(files).map(([name, f]) => (
                  <tr key={name}><td><a href={`/api/runs/${run.id}/files/${name}`}>{name}</a></td><td className="num">{f.rows}</td><td className="hash">{f.sha256.slice(0, 12)}</td></tr>
                ))}</tbody>
              </table></div>
              <p className="muted small" style={{ marginTop: 6 }}>One row per employee. No government IDs; no payment is made.</p>
            </div>
            <div>
              <h3>Files match the register</h3>
              {run.files.reconciliation.map((c, i) => (
                <div key={i} className="check"><span className={`dot ${c.matches ? "ok" : "error"}`} /><span>{c.check}: {c.file_total} {c.matches ? "=" : "≠"} {c.register_total}</span></div>
              ))}
            </div>
          </div>
        )}
      </div>
    </>
  );
}

// ------------------------------------------------------------------ what-if
const WHAT_IF = [
  ["lop", "divisor", "One day of unpaid leave is", [["days_in_month", "pay ÷ days in the month"], [30, "pay ÷ 30"], [26, "pay ÷ 26 working days"]]],
  ["pf_policy", "wage_base", "PF is worked out on", [["capped", "the first ₹15,000 of basic"], ["full", "all of basic"]]],
  ["rounding", "earnings", "Rounding", [["per_component", "each part of pay"], ["gross_only", "only the total"]]],
  ["pf_policy", "arrears_contribution", "PF on arrears", [["recompute_delta", "yes, on the revised months"], ["none", "no"]]],
  ["esi_policy", "eligibility_wage", "ESI eligibility uses", [["gross_this_month", "gross this month"], ["regular_earned", "regular pay only"], ["contracted_monthly", "full monthly salary"]]],
];

function WhatIf({ run, me }) {
  const [values, setValues] = useState({});
  const [busy, act] = useAction();
  if (!me.roles.includes("maker")) return null;
  const create = async () => {
    const overrides = {};
    for (const [key, field] of WHAT_IF) {
      const v = values[`${key}.${field}`];
      if (v === undefined || v === "") continue;
      overrides[key] = { ...(overrides[key] || {}), [field]: /^\d+$/.test(v) ? Number(v) : v };
    }
    const base = run.run_type === "draft" ? run.base_run.id : run.id;
    const draft = await act("draft", () => api.post(`/runs/${base}/draft-rerun`, { overrides }), "Draft re-run calculated");
    if (draft) go(`/runs/${draft.id}`);
  };
  return (
    <>
      <SectionTitle title="What if? (draft re-run)" />
      <div className="card">
        <p className="muted">Re-run {run.period_label} as a draft with today's rules (including any rule version you just added on <a href="#/rules">Rules &amp; settings</a>) and, optionally, different company settings. {run.status === "finalized" ? "The approved month stays exactly as it was." : ""}</p>
        <div className="grid2">
          {WHAT_IF.map(([key, field, label, options]) => (
            <Field key={`${key}.${field}`} label={label}>
              <select value={values[`${key}.${field}`] ?? ""} onChange={(e) => setValues({ ...values, [`${key}.${field}`]: e.target.value })}>
                <option value="">Keep the current setting</option>
                {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </Field>
          ))}
        </div>
        <div className="row" style={{ marginTop: 12 }}>
          <button className="primary" disabled={!!busy} onClick={create}>Run a draft and compare</button>
          {run.run_type === "draft" && <span className="muted small">This creates another draft of #{run.base_run.id}.</span>}
        </div>
      </div>
    </>
  );
}

function Repeatable({ run }) {
  const [out, setOut] = useState(null);
  const [busy, act] = useAction();
  const verify = async () => setOut(await act("verify", () => api.post(`/runs/${run.id}/verify`)));
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="row between">
        <div><b>Same inputs, same result</b> <span className="muted small">Calculate again from the stored inputs (no database, no AI) and compare fingerprints.</span></div>
        <button disabled={!!busy} onClick={verify}>Run again and compare</button>
      </div>
      {out && (
        <div style={{ marginTop: 8 }}>
          {out.identical ? <Banner kind="ok">Identical. Stored <span className="hash">{out.stored_result_hash.slice(0, 16)}</span> = recalculated <span className="hash">{out.recalculated_hash.slice(0, 16)}</span></Banner>
            : <Banner kind="bad">Different! Stored {out.stored_result_hash.slice(0, 16)} vs {out.recalculated_hash.slice(0, 16)}</Banner>}
        </div>
      )}
    </div>
  );
}

function Events({ run }) {
  const [open, setOpen] = useState(false);
  const events = useMemo(() => [...run.events].reverse(), [run.events]);
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="row between"><b>Audit trail ({run.events.length})</b><button className="ghost small" onClick={() => setOpen(!open)}>{open ? "Hide" : "Show"}</button></div>
      {open && (
        <div className="table-wrap" style={{ marginTop: 8 }}><table>
          <thead><tr><th>When</th><th>Who</th><th>What</th></tr></thead>
          <tbody>{events.map((e, i) => <tr key={i}><td className="small">{when(e.at)}</td><td className="small">{e.actor}</td><td>{e.message}</td></tr>)}</tbody>
        </table></div>
      )}
    </div>
  );
}
