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
  ["/data", "HR data", "⇩"],
  ["/rules", "Rules & settings", "§"],
  ["/ai", "AI log", "✦"],
  ["/tools/wage-code", "Labour Codes tool", "⚖"],
];

export default function App() {
  const route = useHashRoute();
  const [me, setMe] = useState(null);
  const [company, setCompany] = useState(null);
  const [error, setError] = useState("");

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
    if (!me?.authenticated) return;
    api.get("/companies").then((cs) => setCompany(cs[0] || false)).catch((e) => setError(e.message));
  }, [me]);

  if (error) return <div className="main"><Banner kind="bad" title="Cannot reach the server.">{error}</Banner></div>;
  if (!me) return <Loading />;
  if (!me.authenticated) return <><Login onDone={setMe} /><Toasts /></>;
  if (company === null) return <Loading />;
  if (company === false) return <div className="main"><Banner kind="warn">No company yet. Run <code>python manage.py seed_demo</code>.</Banner></div>;

  const logout = async () => { await api.post("/logout"); setMe({ authenticated: false }); };
  const parts = route.split("/").filter(Boolean);
  let page;
  if (route === "/") page = <Overview company={company} me={me} />;
  else if (parts[0] === "months") page = <Months company={company} me={me} />;
  else if (parts[0] === "runs" && parts[1]) page = <RunPage key={parts[1]} runId={Number(parts[1])} me={me} />;
  else if (parts[0] === "data" && parts[1] === "job") page = <JobPage key={parts[2]} jobId={Number(parts[2])} me={me} />;
  else if (parts[0] === "data" && parts[1] === "snapshot") page = <SnapshotPage key={parts[2]} snapshotId={Number(parts[2])} me={me} />;
  else if (parts[0] === "data") page = <DataSources company={company} me={me} />;
  else if (parts[0] === "rules") page = <Rules company={company} me={me} />;
  else if (parts[0] === "ai") page = <AILog company={company} />;
  else if (parts[0] === "tools") page = <WageCode company={company} />;
  else page = <Banner kind="warn">Page not found.</Banner>;

  const active = (path) => (path === "/" ? route === "/" : route.startsWith(path) || (path === "/months" && route.startsWith("/runs")));
  return (
    <div className="shell">
      <nav className="side">
        <div className="brand">₹ Payroll Compliance</div>
        <div className="company">{company.name} · {company.city || company.state}</div>
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
