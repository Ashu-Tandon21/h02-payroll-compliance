"""Build data/brickline/brickline_hr_data.xlsx from data/brickline/seed.json.

The spreadsheet deliberately uses different column names from the demo HR tool
("Employee ID" vs "Emp Code", "Basic" vs "Basic Salary") to show that both ways in
end up in the same shape of data.
"""
import json
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

ROOT = Path(__file__).resolve().parent.parent
seed = json.loads((ROOT / "data/brickline/seed.json").read_text())

wb = Workbook()
ws = wb.active
ws.title = "Employees"
ws.append(["Employee ID", "Employee Name", "Gender", "Work Location", "Date of Joining", "Department", "Designation"])
for e in seed["employees"]:
    ws.append([e["code"], e["name"], {"F": "Female", "M": "Male"}[e["gender"]], e["location"], date.fromisoformat(e["doj"]),
               e["department"], e["designation"]])

ws = wb.create_sheet("Salary")
ws.append(["Employee ID", "Effective From", "Basic", "HRA", "Special Allowance", "Monthly Gross", "Recorded On"])
for s in seed["salaries"]:
    ws.append([s["code"], date.fromisoformat(s["effective_from"]), s["basic"], s["hra"], s["special"],
               s["basic"] + s["hra"] + s["special"], date.fromisoformat(s["recorded_on"])])

ws = wb.create_sheet("Leave")
ws.append(["Employee ID", "Leave Type", "From", "To", "Days", "Status"])
for lv in seed["leave"]:
    ws.append([lv["code"], lv["type"], date.fromisoformat(lv["from"]), date.fromisoformat(lv["to"]), lv["days"], lv["status"]])

for sheet in wb.worksheets:
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="E8EEF7")
    for column in sheet.columns:
        sheet.column_dimensions[column[0].column_letter].width = max(14, max(len(str(c.value or "")) for c in column) + 2)
        for c in column[1:]:
            if isinstance(c.value, date):
                c.number_format = "DD-MMM-YYYY"

out = ROOT / "data/brickline/brickline_hr_data.xlsx"
wb.save(out)
print(f"wrote {out}")
