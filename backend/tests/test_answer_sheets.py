"""Run every answer-sheet case in answer_sheets/ (A, B, and C when it is added) and require every number to match."""
from pathlib import Path

import pytest

from payroll.answer_sheet import format_report, run_case

ROOT = Path(__file__).resolve().parents[2] / "answer_sheets"
CASES = sorted(p for p in ROOT.iterdir() if (p / "choices.json").exists() and (p / "expected.csv").exists())


@pytest.mark.django_db(transaction=False)
@pytest.mark.parametrize("folder", CASES, ids=[p.name for p in CASES])
def test_answer_sheet(folder):
    report = run_case(folder, log=lambda *a: None)
    print(format_report(report))
    mismatches = [r for r in report["rows"] if r["status"] != "match"]
    assert not mismatches, format_report(report)
