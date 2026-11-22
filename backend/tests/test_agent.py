"""The browser agent against the Demo HRMS (a different HR tool from Horilla, with its own field names).

Checks: login, pagination, three table layouts, session expiry mid-read, counts, saved mapping
reuse without AI, and a changed screen being caught. Skipped if Chromium is not installed.
"""
import json
import threading
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest
from django.utils import timezone

REPO = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def _chromium_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module")
def hrms():
    if not _chromium_available():
        pytest.skip("Playwright Chromium is not installed")
    import importlib.util
    spec = importlib.util.spec_from_file_location("mock_hrms_app", REPO / "mock_hrms" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.SEED.update(json.loads((REPO / "data/brickline/seed.json").read_text()))

    class Quiet(WSGIRequestHandler):
        def log_message(self, *a):
            pass

    server = make_server("127.0.0.1", 0, mod.app, handler_class=Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/", mod
    server.shutdown()


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
    assert len(tables["employees"].rows) == 6 and tables["employees"].read_info["pages"] == 2
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
