import React, { useState } from "react";
import { api } from "../api.js";
import { go } from "../App.jsx";
import { Banner, Bars, Field, Loading, Stat, StatusChip, aiName, day, inr, signed, stateName, useAction, useLoad, when } from "../ui.jsx";
import { DueStatus } from "./Dues.jsx";

export default function Overview({ company, me }) {
  const { data, error, reload } = useLoad(() => api.get(`/companies/${company.id}/overview`), [company.id]);
  const [busy, act] = useAction();
  const [opening, setOpening] = useState(false);
  const [days, setDays] = useState("");
  if (!data) return <Loading error={error} />;
  const isMaker = me.roles.includes("maker");
  const isChecker = me.roles.includes("checker");
  const dayCount = days || (data.next_day_count ? String(data.next_day_count.days) : "");
  const open = async (period) => {
    const run = await act("open", () => api.post(`/companies/${company.id}/runs`, { period, day_count: dayCount }), `${period} opened`);
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
      {isMaker && data.setting_up && (
        <Banner kind="info" title={`${data.company.name} is being set up.`} action={<button className="primary" onClick={() => go(`/onboarding/${data.company.id}`)}>Continue setup</button>}>
          Company details, pay settings, HR data, opening balances and the first month, step by step.
        </Banner>
      )}
      {data.dues.overdue > 0 && (
        <Banner kind="bad" title={`${data.dues.overdue} government payment${data.dues.overdue > 1 ? "s are" : " is"} overdue.`}
          action={<button onClick={() => go("/dues")}>See dues</button>}>
          Deposit them and record the challan numbers.
        </Banner>
      )}
      {!data.approved_snapshot && !data.setting_up && (
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
          {isMaker && data.next_period && !data.months.some((m) => m.period === data.next_period && m.regular) && !opening && (
            <button className="primary" disabled={!!busy || !data.approved_snapshot} onClick={() => setOpening(true)}>
              Open {data.months.length ? "next month" : "a month"}: {data.next_period}
            </button>
          )}
        </div>
        {opening && (
          <div className="card tight row" style={{ alignItems: "flex-end", marginBottom: 12 }}>
            <b style={{ alignSelf: "center" }}>Open {data.next_period}</b>
            <Field label="Days for one day's pay" hint={data.next_day_count ? `Company setting: ${data.next_day_count.days} (${data.next_day_count.source})` : ""}>
              <input type="number" min="1" max="31" style={{ width: 90 }} value={dayCount} onChange={(e) => setDays(e.target.value)} />
            </Field>
            <button className="primary" disabled={!!busy || !dayCount} onClick={() => open(data.next_period)}>Open</button>
            <button className="ghost" onClick={() => setOpening(false)}>Cancel</button>
          </div>
        )}
        {data.months.length === 0 && <div className="empty">No months yet. Go to <a href="#/months">Payroll months</a> to open one.</div>}
        <div className="timeline">
          {data.months.map((m, i) => {
            const prev = data.months.slice(i + 1).find((x) => x.regular?.net_pay);
            const delta = m.regular?.net_pay && prev ? Number(m.regular.net_pay) - Number(prev.regular.net_pay) : null;
            return (
            <div className="month-card" key={m.period}>
              <div>
                <b>{m.label}</b>
                <div className="muted small">{m.period}</div>
              </div>
              <div className="row">
                {m.regular ? (
                  <>
                    <StatusChip status={m.regular.status} label={m.regular.status_label} />
                    {m.regular.employee_count != null && <span className="muted">{m.regular.employee_count} employees</span>}
                    {m.regular.net_pay && <span>Net pay <b>{inr(m.regular.net_pay)}</b>{delta !== null && delta !== 0 && <span className={`small ${delta > 0 ? "up" : "down"}`} style={{ marginLeft: 6 }}>{signed(delta)}</span>}</span>}
                    {m.regular.employer_cost && <span className="muted small">Cost to company {inr(m.regular.employer_cost)}</span>}
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
            );
          })}
        </div>
      </div>

      {(data.dues.open > 0 || finals.length > 0) && (
        <div className="card">
          <div className="card-head"><h2>Government dues</h2><a href="#/dues" className="small">All dues</a></div>
          {data.dues.next.length === 0 ? <div className="empty">Everything due has been paid and recorded.</div> : (
            <div className="table-wrap"><table>
              <thead><tr><th>Month</th><th>What</th><th className="num">To pay</th><th>Due by</th><th>Status</th></tr></thead>
              <tbody>{data.dues.next.map((x) => (
                <tr key={x.id}><td>{x.period_label}</td><td>{x.title}</td><td className="num"><b>{inr(x.total)}</b></td><td>{day(x.due_date)}</td><td><DueStatus status={x.status} /></td></tr>
              ))}</tbody>
            </table></div>
          )}
          {data.dues.open > data.dues.next.length && <p className="muted small">and {data.dues.open - data.dues.next.length} more still to pay.</p>}
        </div>
      )}

      {data.workforce && (
        <div className="grid2" style={{ marginTop: 16 }}>
          <div className="card">
            <div className="card-head"><h2>People by department</h2><span className="muted small">{data.workforce.total} in the HR data in use</span></div>
            <Bars items={data.workforce.by_department} />
          </div>
          <div className="card">
            <div className="card-head"><h2>Where they work</h2><span className="muted small">by state</span></div>
            <Bars items={data.workforce.by_state.map(([code, n]) => [stateName(code), n])} />
            <p className="muted small" style={{ marginTop: 12 }}>
              Professional tax follows the state of work. {data.workforce.joined_this_year} joined since {day(data.workforce.financial_year_from)} (this financial year);
              their first month is paid from the joining date.
            </p>
          </div>
        </div>
      )}

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
