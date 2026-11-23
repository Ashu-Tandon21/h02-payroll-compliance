"""The browser agent against the Demo HRMS (a different HR tool from Horilla, with its own field names).

Checks: login, pagination, three table layouts, session expiry mid-read, counts, saved mapping
reuse without AI, and a changed screen being caught. Skipped if Chromium is not installed.
"""
import json
import threading

import pytest
from django.utils import timezone

from .conftest import REPO, _chromium_available
pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def _read(conn, maker):
    from payroll.ingestion import jobs
    from payroll.models import IngestionJob

    job = IngestionJob.objects.create(company=conn.company, connector=conn, kind="browser", source_system=conn.system_name,
                                      status="queued", started_by=maker)
    jobs._run_browser_job(job.pk)
    job.refresh_from_db()
    return job


def test_agent_reads_demo_hrms_like_a_person(hrms, monkeypatch):
    url, mod = hrms
    monkeypatch.setenv("MOCK_HRMS_SESSION_PAGES", "4")
    from payroll.ingestion import jobs
    from payroll.ingestion.browser_agent import BrowserAgent
    from payroll.models import AIInteraction, Connector
    from payroll.services import setup, snapshots

    setup.ensure_users()
    setup.load_statutory_rules()
    company = setup.ensure_company("t", "Test Co", "KA")
    maker = setup.system_user("maker")
    conn = Connector.objects.create(company=company, name="Demo HRMS", kind="browser", system_name="Demo HRMS", base_url=url,
                                    username="hr.admin", password="demo123")

    found = BrowserAgent(conn, lambda *a, **k: None).discover(company)
    assert found["profile"] is None and found["interaction"] is not None  # an HR tool the system has no profile for
    picks = {k: v["url"] for k, v in found["datasets"].items() if v}
    assert picks["employees"].endswith("/people") and picks["compensation"].endswith("/compensation") and picks["leave"].endswith("/time-off")
    conn.recipe = {"datasets": {k: {"url": u} for k, u in picks.items()}}
    conn.recipe_confirmed_at = timezone.now()
    conn.save()

    job = _read(conn, maker)
    assert job.status == "needs_mapping", job.error
    tables = {t.dataset_hint: t for t in job.tables.all()}
    people = json.loads((REPO / "data/brickline/seed.json").read_text())["employees"]
    assert len(tables["employees"].rows) == len(people) and tables["employees"].read_info["pages"] == -(-len(people) // 20)
    assert tables["compensation"].read_info["method"] == "aria grid"
    assert tables["leave"].read_info["method"] == "repeated rows"
    assert any("session expired" in e["message"] for e in job.log)

    jobs.suggest(job, maker)
    for t in job.tables.all():
        t.refresh_from_db()
        jobs.confirm(t, maker, t.mapping.mapping, t.mapping.value_maps)
    job.refresh_from_db()
    snap = snapshots.create_from_job(job, maker)
    assert not snap.has_blocking_issues
    assert {c["code"] for c in snap.checks if c["level"] == "ok"} >= {"count_matches", "salary_for_all"}

    # Second read: the confirmed mapping is reused and no AI is asked.
    asked = AIInteraction.objects.count()
    job2 = _read(conn, maker)
    assert job2.status == "ready"
    assert AIInteraction.objects.count() == asked

    # The HR tool changes its salary screen: the agent notices and asks a person again.
    monkeypatch.setenv("MOCK_HRMS_VARIANT", "v2")
    job3 = _read(conn, maker)
    salary = job3.tables.get(dataset_hint="compensation")
    assert salary.mapping is None
    assert any(i["code"] == "screen_changed" for i in salary.read_info.get("issues", []))


def test_agent_never_opens_a_screen_on_another_site(hrms):
    url, _ = hrms
    from payroll.models import Connector
    from payroll.services import setup

    setup.ensure_users()
    company = setup.ensure_company("t", "Test Co", "KA")
    maker = setup.system_user("maker")
    conn = Connector.objects.create(company=company, name="Demo HRMS", kind="browser", system_name="Demo HRMS", base_url=url,
                                    username="hr.admin", password="demo123", recipe_confirmed_at=timezone.now(),
                                    recipe={"datasets": {"employees": {"url": "/people"},
                                                         "leave": {"url": "http://localhost:8000/leave/request-view/"}}})
    job = _read(conn, maker)
    assert job.status == "needs_mapping", job.error
    assert {t.dataset_hint for t in job.tables.all()} == {"employees"}
    assert any("Not opening the Leave screen" in e["message"] for e in job.log)
    assert not any("Opening the Leave screen" in e["message"] for e in job.log)


@pytest.mark.django_db
def test_a_known_hr_tool_is_recognised_and_its_screens_come_from_its_profile(hrms, monkeypatch, tmp_path, settings):
    """Pretend the Demo HRMS were a known tool: after signing in, the agent recognises it and asks no AI."""
    import json as _json

    from payroll.ai import client
    from payroll.ingestion import profiles
    from payroll.ingestion.browser_agent import BrowserAgent
    from payroll.models import AIInteraction, Connector
    from payroll.services import setup

    url, _ = hrms
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo/profile.json").write_text(_json.dumps({
        "system": "demo", "name": "Demo HRMS", "verified_on": "2026-11-22", "detect": {"title_contains": ["Demo HRMS"]},
        "screens": {"datasets": {"employees": {"url": "/people"}, "compensation": {"url": "/compensation"}, "leave": {"url": "/time-off"}}},
    }))
    known = profiles.load_profiles(str(tmp_path))
    monkeypatch.setattr(profiles, "load_profiles", lambda directory=None: known)
    settings.AI_PROVIDER = "anthropic"
    monkeypatch.setattr(client, "_call_claude", lambda *a, **k: (_ for _ in ()).throw(AssertionError("AI was asked")))

    setup.ensure_users()
    company = setup.ensure_company("t2", "Test Co", "KA")
    conn = Connector.objects.create(company=company, name="HR", kind="browser", system_name="HR", base_url=url,
                                    username="hr.admin", password="demo123")
    messages = []
    found = BrowserAgent(conn, lambda msg, *a, **k: messages.append(msg)).discover(company)
    assert found["profile"].system == "demo" and found["interaction"] is None
    assert found["datasets"]["compensation"]["url"] == "/compensation"
    assert any("Demo HRMS profile (no AI)" in m for m in messages)
    assert AIInteraction.objects.count() == 0


# A list screen built the hard way, as some HR tools do: page arrows that are icons with no text, a page-number
# box that says how many pages there are, rows that arrive a moment after the click, a guided-tour overlay over
# the whole page (closed with Escape), and a chevron in the sidebar that is not "next".
TRICKY_LIST = """<!doctype html><html><head><title>People</title></head><body>
<aside><button class="closeSidemenu"><i class="fa fa-angle-right"></i></button></aside>
<div class="pagination"><button class="cursor-not-allowed"><i class="fa fa-angle-left"></i></button>
<input type="number" name="page" min="1" max="3" value="1"><button id="nx"><i class="fa fa-angle-right"></i></button>
<button><i class="fa fa-angles-right"></i></button></div>
<table><thead><tr><th>Employee ID</th><th>Employee Name</th></tr></thead><tbody id="rows"></tbody></table>
<div class="tour-overlay" style="position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:999"></div>
<script>
const people = Array.from({length: 45}, (_, i) => ['T' + String(i + 1).padStart(3, '0'), 'Person ' + (i + 1)]);
let pageNo = 1;
const draw = () => { document.getElementById('rows').innerHTML = people.slice((pageNo - 1) * 20, pageNo * 20)
  .map(p => '<tr onclick="window.location.href=\\'/people/' + p[0] + '\\'"><td>' + p[0] + '</td><td>' + p[1] + '</td></tr>').join(''); document.querySelector('[name=page]').value = pageNo; };
draw();
document.addEventListener('keydown', e => { if (e.key === 'Escape') document.querySelectorAll('.tour-overlay').forEach(o => o.remove()); });
document.getElementById('nx').addEventListener('click', () => { if (BROKEN) return; if (pageNo < 3) { pageNo++; setTimeout(draw, 700); } });
</script></body></html>"""


@pytest.mark.parametrize("broken", [False, True], ids=["pages-through", "broken-next-is-flagged"])
def test_agent_pages_through_icon_arrows_behind_a_tour(broken, settings):
    if not _chromium_available():
        pytest.skip("Playwright Chromium is not installed")
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from payroll.ingestion.browser_agent import BrowserAgent
    from payroll.models import Connector
    from payroll.services import setup

    body = TRICKY_LIST.replace("BROKEN", "true" if broken else "false").encode()

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        company = setup.ensure_company("t3", "Test Co", "KA")
        conn = Connector.objects.create(company=company, name="Tricky", kind="browser", system_name="Tricky",
                                        base_url=f"http://127.0.0.1:{server.server_port}/")
        messages = []
        agent = BrowserAgent(conn, lambda msg, *a, **k: messages.append(msg))
        page = agent._browser()
        try:
            table = agent.read_dataset(page, "employees", {"url": "/people"})
        finally:
            agent._close()
            agent.shutdown()
    finally:
        server.shutdown()
    info = table["read_info"]
    if not broken:
        assert [r["Employee ID"] for r in table["rows"]] == [f"T{i:03d}" for i in range(1, 46)]
        assert info["pages"] == 3 and info["pages_shown"] == 3 and info["complete"] is True
        # rows that open a page when clicked count as links to that page
        assert table["_links"][0].endswith("/people/T001") and table["_links"][44].endswith("/people/T045")
    else:
        # The next arrow does nothing: never hand back page 1 as if it were the whole list.
        assert len(table["rows"]) == 20 and info["complete"] is False and info["pages_shown"] == 3
        assert any(i["code"] == "pages_missing" for i in info["issues"])
