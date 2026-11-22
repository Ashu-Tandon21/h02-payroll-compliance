import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolate(settings, tmp_path):
    """Tests never call a real AI model, never need Temporal, and write files to a temp folder."""
    settings.AI_PROVIDER = "off"
    settings.WORKFLOW_BACKEND = "local"
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture
def statutory_versions():
    data = json.loads((REPO / "data/rules/statutory_in.json").read_text())["versions"]
    return [dict(v, id=str(i + 1), scope="statutory") for i, v in enumerate(data)]


def company_setting(key, payload, effective_from="2025-04-01", vid=None):
    return {"id": vid or f"c-{key}-{effective_from}", "key": key, "scope": "company", "jurisdiction": "",
            "effective_from": effective_from, "payload": payload, "title": key}


@pytest.fixture
def demo(db):
    """Seeded company with the Brickline spreadsheet imported and reviewed."""
    from payroll.services import setup

    setup.ensure_users()
    setup.load_statutory_rules()
    seed = json.loads((REPO / "data/brickline/seed.json").read_text())
    c = seed["company"]
    company = setup.ensure_company(c["code"], c["name"], c["state"], c["city"])
    choices = json.loads((REPO / "answer_sheets/case_A/choices.json").read_text())
    setup.set_company_settings(company, choices["settings"], choices["effective_from"])
    snapshot = setup.import_spreadsheet(company, REPO / "data/brickline/brickline_hr_data.xlsx", log=lambda *a: None)
    return company, snapshot


