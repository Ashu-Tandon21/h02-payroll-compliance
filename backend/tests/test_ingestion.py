"""Both ways in give the same data; field names differ between tools; values are read exactly."""
from datetime import date
from decimal import Decimal

import pytest

from payroll.ingestion.canonical import heuristic_mapping, month_column
from payroll.ingestion.normalize import build_snapshot, unpaid_days_for, validate_mapping
from payroll.ingestion.values import parse_amount, parse_date
from payroll_engine.periods import Period

STRUCTURE = {"components": [{"code": "basic", "share": "0.5"}, {"code": "hra", "share": "0.2"}, {"code": "special_allowance", "share": "remainder"}]}


@pytest.mark.parametrize("header", ["Basic", "Basic Salary", "Basic Pay", "BASIC (₹)", "basic_salary"])
def test_basic_pay_has_many_names(header):
    assert heuristic_mapping(["Emp Code", header])[header]["target"] == "comp.basic"


def test_horilla_like_and_keka_like_headers():
    m = heuristic_mapping(["Employee", "Leave Type", "Start Date", "End Date", "Requested Days", "Status"], "leave")
    assert m["Leave Type"]["target"] == "leave.type"
    assert m["Start Date"]["target"] == "leave.start_date"
    assert m["Requested Days"]["target"] == "leave.days"
    assert m["Status"]["target"] == "leave.status"


def test_employee_status_column_is_not_taken_for_leave():
    m = heuristic_mapping(["Emp Code", "Name", "Status"])
    assert m["Status"]["target"] == "ignore"


def test_month_columns():
    assert month_column("LOP days - July 2025", None) == "attendance.unpaid_days@2025-07"
    assert month_column("June unpaid leave", 2025) == "attendance.unpaid_days@2025-06"


@pytest.mark.parametrize("text,value", [("₹45,000.00", "45000"), ("Rs. 1,50,000", "150000"), ("10k", "10000"), ("1.5 lakh", "150000"), ("(500)", "-500")])
def test_amounts(text, value):
    assert parse_amount(text) == Decimal(value)


@pytest.mark.parametrize("text", ["2025-07-14", "14/07/2025", "14-Jul-2025", "Jul. 14, 2025", "July 14, 2025", "14 Jul 2025"])
def test_dates(text):
    assert parse_date(text) == date(2025, 7, 14)


def test_spreadsheet_with_one_row_per_employee_and_a_raise_column():
    rows = [{"Name": "Priya Sharma", "Salary": "80000", "New salary": "90000", "Raise from": "01/05/2025", "LOP Jul 2025": "0"},
            {"Name": "Arjun Mehta", "Salary": "18000", "New salary": "", "Raise from": "", "LOP Jul 2025": "3"}]
    mapping = {h: v["target"] for h, v in heuristic_mapping(list(rows[0])).items()}
    assert mapping == {"Name": "employee.name", "Salary": "comp.monthly_gross", "New salary": "revision.monthly_gross",
                       "Raise from": "revision.effective_from", "LOP Jul 2025": "attendance.unpaid_days@2025-07"}
    data, checks = build_snapshot([{"name": "Sheet1", "headers": list(rows[0]), "rows": rows, "mapping": mapping}], STRUCTURE, date(2025, 7, 10))
    priya = [c for c in data["compensation"] if c["employee_code"].endswith("priya-sharma")]
    assert [c["components"]["basic"] for c in priya] == ["40000", "45000"]
    assert priya[1]["recorded_on"] == "2025-07-10" and priya[1]["recorded_on_assumed"]
    arjun = next(e for e in data["employees"] if e["name"] == "Arjun Mehta")
    assert unpaid_days_for(data, arjun["code"], Period(2025, 7)) == 3


def test_leave_status_and_type_filters():
    rows = [{"ID": "E1", "Type": "Loss of Pay", "From": "2025-07-01", "To": "2025-07-02", "Days": "2", "Status": "Approved"},
            {"ID": "E1", "Type": "Loss of Pay", "From": "2025-07-10", "To": "2025-07-10", "Days": "1", "Status": "Rejected"},
            {"ID": "E1", "Type": "Casual Leave", "From": "2025-07-20", "To": "2025-07-20", "Days": "1", "Status": "Approved"},
            {"ID": "E1", "Type": "LWP", "From": "2025-07-30", "To": "2025-08-02", "Days": "4", "Status": "Approved"}]
    emp = [{"ID": "E1", "Name": "A", "Basic": "1000"}]
    tables = [{"name": "Emp", "headers": ["ID", "Name", "Basic"], "rows": emp, "mapping": {"ID": "employee.code", "Name": "employee.name", "Basic": "comp.basic"}},
              {"name": "Leave", "headers": list(rows[0]), "rows": rows, "dataset_hint": "leave",
               "mapping": {"ID": "employee.code", "Type": "leave.type", "From": "leave.start_date", "To": "leave.end_date", "Days": "leave.days", "Status": "leave.status"}}]
    data, _ = build_snapshot(tables, STRUCTURE, date(2025, 8, 1))
    assert unpaid_days_for(data, "E1", Period(2025, 7)) == 2 + 2  # 2 approved LOP + 2 of the 4 LWP days that fall in July
    assert unpaid_days_for(data, "E1", Period(2025, 8)) == 2


def test_completeness_checks_catch_missing_rows():
    tables = [{"name": "People", "headers": ["ID", "Name"], "rows": [{"ID": "1", "Name": "A"}], "read_info": {"total_shown": 2},
               "mapping": {"ID": "employee.code", "Name": "employee.name"}, "dataset_hint": "employees"}]
    _, checks = build_snapshot(tables, STRUCTURE, date(2025, 7, 1))
    codes = {c["code"] for c in checks if c["level"] == "error"}
    assert {"count_mismatch", "no_salary"} <= codes


def test_mapping_validation():
    assert validate_mapping({"A": "comp.basic", "B": "comp.basic"}, ["A", "B"])
    assert validate_mapping({"A": "comp.basic"}, ["A"])  # no employee identity


@pytest.mark.django_db
def test_spreadsheet_and_hr_tool_give_the_same_facts(demo):
    """The Demo HRMS shows the same people with different column names and formats."""
    import json

    from .conftest import REPO

    company, sheet_snapshot = demo
    seed = json.loads((REPO / "data/brickline/seed.json").read_text())
    names = {e["code"]: e["name"] for e in seed["employees"]}
    people = [{"Emp Code": e["code"], "Full Name": e["name"], "Gender": "Female" if e["gender"] == "F" else "Male", "Location": e["location"],
               "Joined On": date.fromisoformat(e["doj"]).strftime("%d %b %Y")} for e in seed["employees"]]
    pay = [{"Emp Code": s["code"], "Employee": names[s["code"]], "Basic Salary": f"₹{s['basic']:,}.00", "House Rent Allowance": f"₹{s['hra']:,}.00",
            "Special Allow.": f"₹{s['special']:,}.00", "Effective Date": date.fromisoformat(s["effective_from"]).strftime("%d %b %Y"),
            "Approved On": date.fromisoformat(s["recorded_on"]).strftime("%d %b %Y")} for s in seed["salaries"]]
    leave = [{"Emp Code": lv["code"], "Leave Type": lv["type"], "Start": lv["from"], "End": lv["to"], "No. of Days": str(lv["days"]), "Status": lv["status"]}
             for lv in seed["leave"]]
    tables = []
    for name, rows, hint in (("People", people, "employees"), ("Pay", pay, "compensation"), ("Time off", leave, "leave")):
        guess = heuristic_mapping(list(rows[0]), hint)
        tables.append({"name": name, "headers": list(rows[0]), "rows": rows, "dataset_hint": hint, "mapping": {h: g["target"] for h, g in guess.items()}})
    data, _ = build_snapshot(tables, STRUCTURE, date.today())
    from payroll.services.snapshots import content_of
    assert content_of(data) == content_of(sheet_snapshot.data)


def test_horilla_style_contract_list_gives_components_and_first_seen_dates():
    """Horilla lists contracts with the employee's name and a single wage; no 'recorded on' column."""
    contracts = [{"Contract": "Priya 2025", "Employee": "Priya Sharma", "Start Date": "Apr. 1, 2025", "Contract wage": "80000.0", "Status": "Expired"},
                 {"Contract": "Priya raise", "Employee": "Priya Sharma", "Start Date": "May 1, 2025", "Contract wage": "90000.0", "Status": "Active"}]
    people = [{"Badge ID": "BL001", "Employee": "Priya Sharma", "Work location": "Bengaluru, Karnataka"}]
    mapping_c = {"Contract": "ignore", "Employee": "employee.name", "Start Date": "comp.effective_from", "Contract wage": "comp.monthly_gross", "Status": "ignore"}
    mapping_p = {"Badge ID": "employee.code", "Employee": "employee.name", "Work location": "employee.work_state"}
    tables = [{"name": "Employees", "headers": list(people[0]), "rows": people, "mapping": mapping_p, "dataset_hint": "employees"},
              {"name": "Contracts", "headers": list(contracts[0]), "rows": contracts, "mapping": mapping_c, "dataset_hint": "compensation"}]
    data, checks = build_snapshot(tables, STRUCTURE, date(2025, 7, 3))
    comp = data["compensation"]
    assert [c["employee_code"] for c in comp] == ["BL001", "BL001"]
    assert comp[1]["components"] == {"basic": "45000", "hra": "18000", "special_allowance": "27000"}
    assert comp[1]["recorded_on"] == "2025-07-03" and comp[1]["recorded_on_assumed"]
    assert not [c for c in checks if c["level"] == "error"]


def test_remainder_of_gross_is_split_by_structure():
    rows = [{"ID": "E1", "Name": "A", "Basic": "9000", "Gross": "18000"}]
    mapping = {"ID": "employee.code", "Name": "employee.name", "Basic": "comp.basic", "Gross": "comp.monthly_gross"}
    data, _ = build_snapshot([{"name": "S", "headers": list(rows[0]), "rows": rows, "mapping": mapping}], STRUCTURE, date(2025, 7, 1))
    assert data["compensation"][0]["components"] == {"basic": "9000", "hra": "3600", "special_allowance": "5400"}


def test_horilla_default_columns_map_without_help():
    contracts = heuristic_mapping(["Contract", "Employee", "Start Date", "End Date", "Wage Type", "Contract wage", "Filing Status", "Salary Structure", "Status"], "compensation")
    assert contracts["Employee"]["target"] == "employee.name"
    assert contracts["Start Date"]["target"] == "comp.effective_from"
    assert contracts["Contract wage"]["target"] == "comp.monthly_gross"
    leave = heuristic_mapping(["Employee", "Leave Type", "Start Date", "End Date", "Requested Days", "Status"], "leave")
    assert leave["Start Date"]["target"] == "leave.start_date" and leave["End Date"]["target"] == "leave.end_date"


def test_name_with_badge_in_one_column():
    rows = [{"Employee": "Priya Sharma (BL001)", "Basic": "1"}]
    data, _ = build_snapshot([{"name": "E", "headers": ["Employee", "Basic"], "rows": rows,
                               "mapping": {"Employee": "employee.name", "Basic": "comp.basic"}}], STRUCTURE, date(2025, 7, 1))
    assert data["employees"][0] == {"code": "BL001", "name": "Priya Sharma"}
