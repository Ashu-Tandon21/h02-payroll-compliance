import React, { useEffect, useState } from "react";
import { api } from "./api.js";
import { Banner, Field, Loading, Toasts } from "./ui.jsx";
import Overview from "./pages/Overview.jsx";
import { DataSources, JobPage, SnapshotPage } from "./pages/HRData.jsx";
import Months from "./pages/Months.jsx";
import RunPage from "./pages/Run.jsx";
import Rules from "./pages/Rules.jsx";
import AILog from "./pages/AILog.jsx";
import WageCode from "./pages/WageCode.jsx";
import Dues from "./pages/Dues.jsx";
import Onboarding from "./pages/Onboarding.jsx";

function useHashRoute() {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const on = () => { setHash(window.location.hash || "#/"); window.scrollTo(0, 0); };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash.replace(/^#/, "") || "/";
}

export function go(path) { window.location.hash = path; }

function Login({ onDone }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const submit = async (e) => {
    e.preventDefault();
    setError("");
    try {
      onDone(await api.post("/login", { username, password }));
    } catch (err) {
      setError(err.message);
    }
  };
  return (
    <div className="login">
      <form className="card stack" onSubmit={submit}>
        <div>
          <h1>Payroll Compliance</h1>
          <p className="muted">Sign in to prepare or approve a payroll month.</p>
        </div>
        {error && <Banner kind="bad">{error}</Banner>}
        <Field label="Username"><input autoFocus value={username} onChange={(e) => setUsername(e.target.value)} /></Field>
        <Field label="Password"><input type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></Field>
        <button className="primary" type="submit">Sign in</button>
        <div className="muted small">
          Demo accounts: <b>maker</b> / maker123 prepares the month · <b>checker</b> / checker123 approves it.
        </div>
      </form>
    </div>
  );
}

const NAV = [
  ["/", "Overview", "◎"],
  ["/months", "Payroll months", "▦"],
  ["/dues", "Government dues", "⌛"],
  ["/data", "HR data", "⇩"],
  ["/rules", "Rules & settings", "§"],
  ["/ai", "AI log", "✦"],
  ["/tools/wage-code", "Labour Codes tool", "⚖"],
];

export default function App() {
  const route = useHashRoute();
  const [me, setMe] = useState(null);
  const [companies, setCompanies] = useState(null);
  const [companyId, setCompanyId] = useState(() => { try { return Number(window.localStorage.getItem("company")) || null; } catch { return null; } });
  const [error, setError] = useState("");
  const pick = (id) => { setCompanyId(id); try { window.localStorage.setItem("company", String(id)); } catch { /* not remembered */ } };
  const loadCompanies = () => api.get("/companies").then(setCompanies).catch((e) => setError(e.message));

  useEffect(() => {
    (async () => {
      try {
        await api.init();
        setMe(await api.get("/me"));
      } catch (e) {
        setError(e.message);
      }
    })();
  }, []);

  useEffect(() => {
    if (me?.authenticated) loadCompanies();
  }, [me]);

  const parts = route.split("/").filter(Boolean);
  // Setting up a company switches to it.
  const wanted = parts[0] === "onboarding" && parts[1] ? Number(parts[1]) : null;
  useEffect(() => { if (wanted && wanted !== companyId) pick(wanted); }, [wanted]);

  if (error) return <div className="main"><Banner kind="bad" title="Cannot reach the server.">{error}</Banner></div>;
  if (!me) return <Loading />;
  if (!me.authenticated) return <><Login onDone={setMe} /><Toasts /></>;
  if (companies === null) return <Loading />;

  const company = companies.find((c) => c.id === (wanted || companyId)) || companies[0] || null;
  const created = (c) => { setCompanies([...companies, c]); pick(c.id); go(`/onboarding/${c.id}`); };
  const changed = (c) => setCompanies(companies.map((x) => (x.id === c.id ? { ...x, ...c } : x)));
  const onboarding = <Onboarding companyId={parts[0] === "onboarding" && parts[1] ? Number(parts[1]) : null} me={me}
    onCompanyCreated={created} onCompanyChanged={changed} />;
  if (!company) return <div className="main">{onboarding}<Toasts /></div>;

  const logout = async () => { await api.post("/logout"); setMe({ authenticated: false }); };
  const switchTo = (value) => {
    if (value === "new") { go("/onboarding"); return; }
    pick(Number(value));
    go("/");
  };
  let page;
  if (parts[0] === "onboarding") page = onboarding;
  else if (route === "/") page = <Overview company={company} me={me} />;
  else if (parts[0] === "months") page = <Months company={company} me={me} />;
  else if (parts[0] === "runs" && parts[1]) page = <RunPage key={parts[1]} runId={Number(parts[1])} me={me} />;
  else if (parts[0] === "data" && parts[1] === "job") page = <JobPage key={parts[2]} jobId={Number(parts[2])} me={me} />;
  else if (parts[0] === "data" && parts[1] === "snapshot") page = <SnapshotPage key={parts[2]} snapshotId={Number(parts[2])} me={me} />;
  else if (parts[0] === "data") page = <DataSources company={company} me={me} />;
  else if (parts[0] === "dues") page = <Dues company={company} me={me} />;
  else if (parts[0] === "rules") page = <Rules company={company} me={me} />;
  else if (parts[0] === "ai") page = <AILog company={company} />;
  else if (parts[0] === "tools") page = <WageCode company={company} />;
  else page = <Banner kind="warn">Page not found.</Banner>;

  const active = (path) => (path === "/" ? route === "/" : route.startsWith(path) || (path === "/months" && route.startsWith("/runs")));
  return (
    <div className="shell">
      <nav className="side">
        <div className="brand">₹ Payroll Compliance</div>
        <div className="company">
          <select aria-label="Company" value={parts[0] === "onboarding" && !parts[1] ? "new" : company.id} onChange={(e) => switchTo(e.target.value)}>
            {companies.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            {me.roles.includes("maker") && <option value="new">+ Add a company…</option>}
          </select>
          <div>{company.city || company.state}{company.tan ? ` · TAN ${company.tan}` : ""}</div>
        </div>
        {NAV.map(([path, label, icon]) => (
          <a key={path} href={"#" + path} className={active(path) ? "active" : ""}><span>{icon}</span>{label}</a>
        ))}
        <div className="foot">
          <div style={{ color: "#fff" }}>{me.user.name}</div>
          <div>{me.roles.length ? me.roles.join(" & ") : "no role"}</div>
          <div style={{ marginTop: 6 }}>AI: {me.ai.provider === "rule-based" ? "rule-based (no model)" : me.ai.provider === "claude-code" ? "Claude Code (headless)" : me.ai.model}</div>
          <div>Workflow: {me.workflow_backend}</div>
          <button className="ghost small" style={{ color: "#9fc1e6", paddingLeft: 0, marginTop: 8 }} onClick={logout}>Sign out</button>
        </div>
      </nav>
      <main className="main">{page}</main>
      <Toasts />
    </div>
  );
}
