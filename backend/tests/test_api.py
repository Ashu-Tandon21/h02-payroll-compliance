"""The screens' API: sign-in, roles, and a month from opening to final through HTTP (local workflow)."""
import pytest
from django.test import Client

from payroll.services import setup

pytestmark = pytest.mark.django_db


def login(username):
    c = Client()
    assert c.post("/api/login", {"username": username, "password": f"{username}123"}, content_type="application/json").status_code == 200
    return c


def test_month_through_the_api(demo):
    company, _ = demo
    for m in ("2025-04", "2025-05", "2025-06"):
        setup.run_month(company, m, log=lambda *a: None)
    assert Client().get(f"/api/companies/{company.pk}/overview").status_code in (401, 403)
    maker, checker = login("maker"), login("checker")
    run = maker.post(f"/api/companies/{company.pk}/runs", {"period": "2025-07"}, content_type="application/json").json()
    rid = run["id"]
    assert maker.post(f"/api/runs/{rid}/calculate", content_type="application/json").status_code == 400  # data not reviewed yet
    maker.post(f"/api/runs/{rid}/review-data", content_type="application/json")
    ch = maker.post(f"/api/runs/{rid}/instructions", {"text": "Give Priya a ₹10,000 one-time bonus this month"}, content_type="application/json").json()
    assert ch["proposal"]["items"][0]["amount"] == "10000"
    maker.post(f"/api/changes/{ch['id']}/confirm", content_type="application/json")
    out = maker.post(f"/api/runs/{rid}/calculate", content_type="application/json").json()
    assert out["run"]["status"] == "calculated"
    detail = maker.get(f"/api/runs/{rid}").json()
    priya = next(e for e in detail["comparison"]["employees"] if e["code"] == "BL001")
    assert {d["kind"] for d in priya["drivers"]} >= {"salary", "arrears", "one_time"}
    assert maker.post(f"/api/runs/{rid}/submit", content_type="application/json").status_code == 200
    assert maker.post(f"/api/runs/{rid}/approve", content_type="application/json").status_code == 403
    assert checker.post(f"/api/runs/{rid}/reject", {"note": ""}, content_type="application/json").status_code == 400
    out = checker.post(f"/api/runs/{rid}/approve", {"note": "ok"}, content_type="application/json").json()
    assert out["run"]["status"] == "finalized"
    files = out["run"]["files"]["files"]
    assert set(files) == {"register.csv", "pf.csv", "esi.csv", "professional_tax.csv", "tds.csv"}
    resp = checker.get(f"/api/runs/{rid}/files/pf.csv")
    assert resp.status_code == 200 and b"Employee share" in b"".join(resp.streaming_content)
    assert maker.post(f"/api/runs/{rid}/explain", content_type="application/json").json()["provider"] == "rule-based"
    policies = maker.get(f"/api/companies/{company.pk}/policies").json()
    assert any(g["key"] == "professional_tax" and g["jurisdiction"] == "IN-KA" for g in policies["groups"])


def test_screens_must_be_on_the_hr_tool_itself(demo):
    from payroll.models import Connector

    company, _ = demo
    conn = Connector.objects.create(company=company, name="Demo HRMS", kind="browser", system_name="Demo HRMS",
                                    base_url="http://localhost:8100/", username="hr.admin", password="demo123")
    maker = login("maker")
    other_site = {"employees": {"url": "http://localhost:8000/employee/employee-view/"}, "leave": {"url": "/time-off"}}
    r = maker.post(f"/api/connectors/{conn.pk}/confirm-screens", {"datasets": other_site}, content_type="application/json")
    assert r.status_code == 400 and "localhost:8000" in r.json()["detail"]
    conn.refresh_from_db()
    assert conn.recipe_confirmed_at is None
    same_site = {"employees": {"url": "/people"}, "leave": {"url": "http://localhost:8100/time-off"}}
    r = maker.post(f"/api/connectors/{conn.pk}/confirm-screens", {"datasets": same_site}, content_type="application/json")
    assert r.status_code == 200


def _sheet_file(sheet, as_csv=False):
    """One sheet of the Brickline workbook as its own file, the way a company keeps each list separately."""
    import csv
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from openpyxl import Workbook, load_workbook

    from .conftest import REPO

    rows = list(load_workbook(REPO / "data/brickline/brickline_hr_data.xlsx")[sheet].iter_rows(values_only=True))
    buf = io.StringIO() if as_csv else io.BytesIO()
    if as_csv:
        csv.writer(buf).writerows([[v.date().isoformat() if hasattr(v, "date") else v for v in r] for r in rows])
        return SimpleUploadedFile(f"{sheet.lower()}_july.csv", buf.getvalue().encode())
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    wb.save(buf)
    return SimpleUploadedFile(f"{sheet.lower()}_list.xlsx", buf.getvalue())


def _content(snapshot_id):
    from payroll.models import DataSnapshot
    from payroll.services.snapshots import content_of

    return content_of(DataSnapshot.objects.get(pk=snapshot_id).data)


def test_employees_salary_and_leave_can_be_separate_files(demo):
    from payroll.services.snapshots import content_of

    company, workbook_snapshot = demo
    maker = login("maker")
    job = maker.post(f"/api/companies/{company.pk}/uploads", {
        "employees": _sheet_file("Employees"), "compensation": _sheet_file("Salary", as_csv=True), "leave": _sheet_file("Leave"),
    }).json()
    assert {t["name"]: t["dataset_hint"] for t in job["tables"]} == {"Employees": "employees", "Salary": "compensation", "Leave": "leave"}
    assert [f["dataset"] for f in job["stats"]["files"]] == ["employees", "compensation", "leave"]
    assert job["status"] == "ready"  # same columns as the workbook: the confirmed mappings are reused
    snap = maker.post(f"/api/jobs/{job['id']}/build-snapshot").json()
    assert _content(snap["id"]) == content_of(workbook_snapshot.data)


def test_a_file_can_be_added_to_a_read_before_the_snapshot(demo):
    from payroll.services.snapshots import content_of

    company, workbook_snapshot = demo
    maker, checker = login("maker"), login("checker")
    job = maker.post(f"/api/companies/{company.pk}/uploads", {"employees": _sheet_file("Employees"), "compensation": _sheet_file("Salary")}).json()
    assert len(job["tables"]) == 2
    assert checker.post(f"/api/jobs/{job['id']}/files", {"leave": _sheet_file("Leave")}).status_code == 403
    job = maker.post(f"/api/jobs/{job['id']}/files", {"leave": _sheet_file("Leave")}).json()
    assert sorted(t["name"] for t in job["tables"]) == ["Employees", "Leave", "Salary"]
    snap = maker.post(f"/api/jobs/{job['id']}/build-snapshot").json()
    assert _content(snap["id"]) == content_of(workbook_snapshot.data)
    assert not any(c["code"] == "no_leave_data" for c in snap["checks"])
    late = maker.post(f"/api/jobs/{job['id']}/files", {"leave": _sheet_file("Leave")})
    assert late.status_code == 400 and "already built" in late.json()["detail"]


def test_missing_or_misplaced_files_are_flagged(demo):
    company, _ = demo
    maker = login("maker")
    job = maker.post(f"/api/companies/{company.pk}/uploads", {"employees": _sheet_file("Employees"), "compensation": _sheet_file("Leave")}).json()
    snap = maker.post(f"/api/jobs/{job['id']}/build-snapshot").json()
    codes = {c["code"] for c in snap["checks"] if c["level"] in ("warning", "error")}
    assert {"wrong_kind_of_data", "no_salary"} <= codes
    job = maker.post(f"/api/companies/{company.pk}/uploads", {"employees": _sheet_file("Employees"), "compensation": _sheet_file("Salary")}).json()
    snap = maker.post(f"/api/jobs/{job['id']}/build-snapshot").json()
    assert "no_leave_data" in {c["code"] for c in snap["checks"]}
    assert maker.post(f"/api/companies/{company.pk}/uploads", {"payslips": _sheet_file("Salary")}).status_code == 400


def test_the_same_file_is_not_added_twice_and_another_layout_is_not_a_changed_screen(demo):
    from openpyxl import Workbook
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile

    company, _ = demo
    maker = login("maker")
    job = maker.post(f"/api/companies/{company.pk}/uploads", {"employees": _sheet_file("Employees"), "compensation": _sheet_file("Salary")}).json()
    again = maker.post(f"/api/jobs/{job['id']}/files", {"compensation": _sheet_file("Salary")})
    assert again.status_code == 400 and "already in this read" in again.json()["detail"]
    # A leave export laid out nothing like the workbook's "Leave" sheet just needs its own mapping.
    wb = Workbook()
    wb.active.append(["Employee", "Start Date", "End Date", "Requested Days", "Status"])
    wb.active.append(["Arjun Mehta (BL002)", "2025-07-14", "2025-07-16", "3", "Approved"])
    buf = io.BytesIO()
    wb.save(buf)
    job = maker.post(f"/api/jobs/{job['id']}/files", {"leave": SimpleUploadedFile("Leave_requests.xlsx", buf.getvalue())}).json()
    leave = next(t for t in job["tables"] if t["name"] == "Leave")
    assert leave["mapping"] is None and not leave["read_info"].get("issues")


def test_a_column_that_is_zero_on_every_row_is_not_suggested(demo):
    from openpyxl import Workbook
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile

    company, _ = demo
    wb = Workbook()
    wb.active.append(["Badge ID", "First Name", "Basic Salary", "Date Joining"])
    wb.active.append(["BL001", "Priya", 0, "2021-06-14"])
    wb.active.append(["BL002", "Arjun", 0, "2024-02-01"])
    buf = io.BytesIO()
    wb.save(buf)
    maker = login("maker")
    job = maker.post(f"/api/companies/{company.pk}/uploads", {"employees": SimpleUploadedFile("employee_export.xlsx", buf.getvalue())}).json()
    job = maker.post(f"/api/jobs/{job['id']}/suggest-mapping").json()
    mapping = job["tables"][0]["mapping"]["mapping"]
    assert mapping["Basic Salary"] == "ignore" and mapping["Badge ID"] == "employee.code"
