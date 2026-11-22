"""Run answer-sheet cases end to end and report which numbers match (no AI involved)."""
import json
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from payroll.answer_sheet import format_report, run_case


class Command(BaseCommand):
    help = "Check the calculation against answer sheets, e.g. `check_answer_sheet answer_sheets/case_A answer_sheets/case_B`."

    def add_arguments(self, parser):
        parser.add_argument("cases", nargs="*", help="Case folders (default: every folder in answer_sheets/)")
        parser.add_argument("--expected", help="Use this CSV instead of the folder's expected.csv")
        parser.add_argument("--data", help="Use this HR spreadsheet instead of the one named in choices.json")
        parser.add_argument("--json", help="Also write the full report to this JSON file")

    def handle(self, *args, **opts):
        root = Path(settings.REPO_DIR) / "answer_sheets"
        cases = [Path(c) for c in opts["cases"]] or sorted(p for p in root.iterdir() if (p / "choices.json").exists())
        reports, failed = [], False
        for folder in cases:
            if not folder.is_absolute():
                folder = (Path.cwd() / folder) if (Path.cwd() / folder).exists() else Path(settings.REPO_DIR) / folder
            self.stdout.write(f"Running {folder.name} ...")
            report = run_case(folder, opts.get("expected"), opts.get("data"), log=self.stdout.write)
            reports.append(report)
            self.stdout.write(format_report(report))
            ok = report["matched"] == report["total"]
            failed = failed or not ok
            style = self.style.SUCCESS if ok else self.style.ERROR
            self.stdout.write(style(f"=> {report['matched']}/{report['total']} match\n"))
        if opts.get("json"):
            Path(opts["json"]).write_text(json.dumps(reports, indent=2))
        if failed:
            sys.exit(1)
