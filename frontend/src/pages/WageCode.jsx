import React, { useState } from "react";
import { api } from "../api.js";
import { Banner, Field, inr, signed, useAction } from "../ui.jsx";

export default function WageCode({ company }) {
  const [form, setForm] = useState({ name: "Nisha Verma", monthly_gross: "60000", basic_share: "0.40", hra_share: "0.20" });
  const [out, setOut] = useState(null);
  const [busy, act] = useAction();
  const run = async () => setOut(await act("sim", () => api.post(`/companies/${company.id}/tools/wage-code`, form)));
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  return (
    <div>
      <div className="topbar"><div><h1>Labour Codes: the 50% wage rule</h1>
        <p className="muted">If the parts of pay that are not wages (such as HRA) add up to more than half of total pay, the extra counts as wages for PF. The rule is a dated government rule on the <a href="#/rules">Rules</a> page; every number here comes from the payroll calculation.</p></div></div>
      <div className="card">
        <div className="row" style={{ alignItems: "flex-end" }}>
          <Field label="Made-up employee"><input value={form.name} onChange={set("name")} /></Field>
          <Field label="Monthly pay (₹)"><input value={form.monthly_gross} onChange={set("monthly_gross")} /></Field>
          <Field label="Basic as share of pay"><input value={form.basic_share} onChange={set("basic_share")} /></Field>
          <Field label="HRA as share of pay"><input value={form.hra_share} onChange={set("hra_share")} /></Field>
          <button className="primary" disabled={!!busy} onClick={run}>Show the effect</button>
        </div>
      </div>
      {out && (
        <>
          {out.rule ? <Banner kind="info">Rule “{out.rule.title}” starts {out.rule.effective_from}. Comparing {out.before_label} (before) with {out.after_label} (after).</Banner>
            : <Banner kind="warn">No wage-definition rule is stored, so nothing changes.</Banner>}
          <div className="card">
            <div className="table-wrap"><table>
              <thead><tr><th>Option</th><th>Pay split</th><th className="num">PF wages before → after</th><th className="num">Employee PF before → after</th><th className="num">Take-home after</th><th className="num">Cost to company after</th></tr></thead>
              <tbody>{out.options.map((o, i) => (
                <tr key={i}>
                  <td><b>{i === 0 ? "As today" : `Option ${i}`}</b><div className="small">{o.title}</div><div className="small muted">{o.after.pf_reason}</div></td>
                  <td className="small">Basic {inr(o.components.basic)}<br />HRA {inr(o.components.hra)}<br />Special {inr(o.components.special_allowance)}</td>
                  <td className="num">{inr(o.before.pf_wage)} → {inr(o.after.pf_wage)}</td>
                  <td className="num">{inr(o.before.pf_employee)} → <b>{inr(o.after.pf_employee)}</b><div className="small">{signed(Number(o.after.pf_employee) - Number(o.before.pf_employee))}</div></td>
                  <td className="num">{inr(o.after.net_pay)}</td>
                  <td className="num">{inr(o.after.employer_cost)}</td>
                </tr>))}
              </tbody></table></div>
          </div>
        </>
      )}
    </div>
  );
}
