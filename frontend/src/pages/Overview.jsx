import React from "react";
import { api } from "../api.js";
import { go } from "../App.jsx";
import { Banner, Loading, Stat, StatusChip, aiName, inr, useAction, useLoad, when } from "../ui.jsx";

export default function Overview({ company, me }) {
  const { data, error, reload } = useLoad(() => api.get(`/companies/${company.id}/overview`), [company.id]);
  const [busy, act] = useAction();
  if (!data) return <Loading error={error} />;
  const isMaker = me.roles.includes("maker");
  const isChecker = me.roles.includes("checker");
  const open = async (period) => {
    const run = await act("open", () => api.post(`/companies/${company.id}/runs`, { period }), `${period} opened`);
    if (run) go(`/runs/${run.id}`);
  };
  const finals = data.months.filter((m) => m.regular?.status === "finalized");
  return (
    <div>
      <div className="topbar">
        <div>
          <h1>{data.company.name}</h1>
          <p className="muted">Payroll, PF, ESI, professional tax and TDS for {data.employee_count} employees.</p>
        </div>
      </div>

      {isChecker && data.awaiting_approval.map((r) => (
        <Banner key={r.id} kind="warn" title={`${r.label} is waiting for your approval.`}
          action={<button className="primary" onClick={() => go(`/runs/${r.id}`)}>Review {r.label}</button>}>
          Net pay {inr(r.net_pay)} for {r.employee_count} employees.
        </Banner>
      ))}
      {!data.approved_snapshot && (
        <Banner kind="info" title="Start here:" action={<button className="primary" onClick={() => go("/data")}>Get HR data</button>}>
          read employees, salaries and leave from your HR tool, or upload a spreadsheet.
        </Banner>
      )}

      <div className="grid4" style={{ marginBottom: 16 }}>
        <Stat label="Employees" value={data.employee_count} />
        <Stat label="Last final month" value={finals[0] ? finals[0].label : "none yet"} />
        <Stat label="HR data in use" value={data.approved_snapshot ? `#${data.approved_snapshot.id} ${data.approved_snapshot.source_system}` : "none"} />
        <Stat label="AI" value={aiName(data.ai.provider)} />
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Payroll months</h2>
          {isMaker && data.next_period && !data.months.some((m) => m.period === data.next_period && m.regular) && (
            <button className="primary" disabled={!!busy || !data.approved_snapshot} onClick={() => open(data.next_period)}>
              Open {data.months.length ? "next month" : "a month"}: {data.next_period}
            </button>
          )}
        </div>
        {data.months.length === 0 && <div className="empty">No months yet. Go to <a href="#/months">Payroll months</a> to open one.</div>}
        <div className="timeline">
          {data.months.map((m) => (
            <div className="month-card" key={m.period}>
              <div>
                <b>{m.label}</b>
                <div className="muted small">{m.period}</div>
              </div>
              <div className="row">
                {m.regular ? (
                  <>
                    <StatusChip status={m.regular.status} label={m.regular.status_label} />
                    {m.regular.net_pay && <span>Net pay <b>{inr(m.regular.net_pay)}</b></span>}
                    {m.regular.workflow_backend === "temporal" && <span className="chip brand">Temporal workflow</span>}
                    {m.drafts.length > 0 && <span className="chip">{m.drafts.length} draft re-run{m.drafts.length > 1 ? "s" : ""}</span>}
                  </>
                ) : <span className="muted">Only draft re-runs</span>}
              </div>
              <div className="row">
                {m.regular && <button onClick={() => go(`/runs/${m.regular.id}`)}>Open</button>}
                {m.drafts.slice(-1).map((d) => <button key={d.id} className="ghost" onClick={() => go(`/runs/${d.id}`)}>Latest draft</button>)}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <h2>How a month works</h2>
        <ol style={{ margin: 0, paddingLeft: 20, lineHeight: 1.8 }}>
          <li><a href="#/data">HR data</a>: the agent reads your HR tool's screens (or you upload a spreadsheet). A person confirms the field mapping once.</li>
          <li>Open the month. The <b>maker</b> reviews the data that was read.</li>
          <li>Type changes in plain English ("Give Priya a ₹10,000 one-time bonus this month"), check what the system understood, confirm.</li>
          <li>Calculate. Every number carries its reason; click any amount to see it. <i>AI never works out an amount.</i></li>
          <li>Submit. The month waits for the <b>checker</b> (a different person) inside a durable workflow that survives restarts.</li>
          <li>The checker approves. The register and the PF, ESI, PT and TDS files are written and reconciled.</li>
        </ol>
        <p className="muted small" style={{ marginTop: 8 }}>
          Rules and company settings are dated data on the <a href="#/rules">Rules &amp; settings</a> page. Every AI answer is in the <a href="#/ai">AI log</a>.
          {data.temporal_ui && <> Workflows can be inspected in the <a href={data.temporal_ui} target="_blank" rel="noreferrer">Temporal UI</a>.</>}
        </p>
      </div>
      <p className="muted small">Updated {when(new Date().toISOString())} · <button className="ghost small" onClick={() => reload()}>Refresh</button></p>
    </div>
  );
}
