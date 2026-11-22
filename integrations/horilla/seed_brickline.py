"""Enter Brickline Design Co.'s made-up data into Horilla.

Run inside Horilla (it uses Horilla's own models):
    python manage.py shell < /path/to/seed_brickline.py                  # local Horilla
    docker compose exec -T web python manage.py shell < seed_brickline.py  # Horilla's Docker stack

Set BRICKLINE_STAGE=june to enter the data as it stood before Priya's raise (to process April-June
first), then run again with BRICKLINE_STAGE=july (the default) to add the raise, as HR did in July.
Safe to run more than once.

How Brickline maps onto Horilla:
  * employee  -> Employee (badge id = BL001...) + Work information (joining date, location)
  * salary    -> Contract, "Contract wage" = the full monthly salary; the payroll system splits it
                 50/20/30 into basic/HRA/special with the company's salary-structure setting.
                 Priya's raise is a second contract starting 1 May 2025.
  * leave     -> Leave requests; "Loss of Pay" is an unpaid leave type.
  * org       -> Departments, job positions, job roles, employee/work types, reporting managers,
                 a Mon-Fri general shift and the company holidays (shown only; pay does not use them).
  * attendance-> One validated check-in/check-out per working day from 1 April to the end of the
                 stage's month, skipping weekends, holidays and approved leave, so attendance agrees
                 with the leave records the payroll system reads.
"""
import json
import os
import random
from datetime import date, time, timedelta

from attendance.models import Attendance, AttendanceValidationCondition
from base.models import (Company, Department, EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule,
                         EmployeeType, Holidays, JobPosition, JobRole, WorkType)
from employee.models import Employee, EmployeeWorkInformation
from leave.models import LeaveRequest, LeaveType
from payroll.models.models import Contract

SEED = {
    "employees": [
        ["BL001", "Priya", "Sharma", "female", "2021-06-14"],
        ["BL002", "Arjun", "Mehta", "male", "2024-02-01"],
        ["BL003", "Meera", "Iyer", "female", "2023-08-07"],
        ["BL004", "Kavya", "Reddy", "female", "2019-04-01"],
        ["BL005", "Ravi", "Kumar", "male", "2024-11-18"],
        ["BL006", "Sneha", "Rao", "female", "2022-01-10"],
    ],
    # badge, contract name, start, end, monthly wage, stage it appears in
    "contracts": [
        ["BL001", "Priya Sharma 2025", "2025-04-01", None, 80000, "june"],
        ["BL001", "Priya Sharma revised (raise approved July)", "2025-05-01", None, 90000, "july"],
        ["BL002", "Arjun Mehta 2025", "2025-04-01", None, 18000, "june"],
        ["BL003", "Meera Iyer 2025", "2025-04-01", None, 30000, "june"],
        ["BL004", "Kavya Reddy 2025", "2025-04-01", None, 150000, "june"],
        ["BL005", "Ravi Kumar 2025", "2025-04-01", None, 21000, "june"],
        ["BL006", "Sneha Rao 2025", "2025-04-01", None, 25000, "june"],
    ],
    "leave": [
        ["BL004", "Sick Leave", "2025-06-09", "2025-06-10", 2, "approved"],
        ["BL005", "Loss of Pay", "2025-06-16", "2025-06-16", 1, "approved"],
        ["BL002", "Loss of Pay", "2025-07-14", "2025-07-16", 3, "approved"],
        ["BL003", "Casual Leave", "2025-07-21", "2025-07-22", 2, "approved"],
        ["BL006", "Loss of Pay", "2025-07-28", "2025-07-28", 1, "rejected"],
    ],
    # badge: department, job position, job role, work type, reports to
    "org": {
        "BL001": ["Design", "Senior Designer", "Brand & Identity Lead", "Hybrid", "BL004"],
        "BL002": ["Studio Ops", "Studio Assistant", "Production Support", "Work from office", "BL005"],
        "BL003": ["Design", "Junior Designer", "Visual Designer", "Hybrid", "BL001"],
        "BL004": ["Leadership", "Design Director", "Creative Head", "Hybrid", None],
        "BL005": ["Studio Ops", "Office Coordinator", "Office Administration", "Work from office", "BL004"],
        "BL006": ["Client Services", "Account Executive", "Client Relationship", "Work from office", "BL004"],
    },
    # badge: date of birth, marital status, children, qualification, years of experience, address, PIN, phone
    "personal": {
        "BL001": ["1993-02-11", "married", 0, "B.Des, NID Ahmedabad", 9, "12th Main, HAL 2nd Stage, Indiranagar", "560038", "9845012001"],
        "BL002": ["2001-09-03", "single", 0, "Diploma in Fine Arts", 1, "6th Block, Koramangala", "560095", "9845012002"],
        "BL003": ["1999-12-20", "single", 0, "B.Des, Srishti Manipal", 2, "4th Block, Jayanagar", "560011", "9845012003"],
        "BL004": ["1986-05-08", "married", 1, "M.Des, IDC IIT Bombay", 16, "ITPL Main Road, Whitefield", "560066", "9845012004"],
        "BL005": ["1997-07-15", "single", 0, "B.Com, Bangalore University", 4, "2nd Stage, BTM Layout", "560076", "9845012005"],
        "BL006": ["1996-03-27", "married", 0, "MBA, Christ University", 5, "18th Cross, Malleshwaram", "560003", "9845012006"],
    },
    "holidays": [
        ["Mahavir Jayanti", "2025-04-10"],
        ["Ambedkar Jayanti", "2025-04-14"],
        ["Good Friday", "2025-04-18"],
        ["May Day", "2025-05-01"],
        ["Bakrid", "2025-06-07"],
        ["Muharram", "2025-07-06"],
        ["Independence Day", "2025-08-15"],
    ],
}
ATTENDANCE_FROM = date(2025, 4, 1)
ATTENDANCE_TO = {"june": date(2025, 6, 30), "july": date(2025, 7, 31)}
stage = os.environ.get("BRICKLINE_STAGE", "july")

company = Company.objects.filter(company="Brickline Design Co.").first()
if company is None:
    company = Company.objects.create(company="Brickline Design Co.", hq=True, address="Indiranagar", country="India",
                                     state="Karnataka", city="Bengaluru", zip="560038")

people = {}
for badge, first, last, gender, doj in SEED["employees"]:
    emp = Employee.objects.filter(badge_id=badge).first()
    if emp is None:
        emp = Employee(badge_id=badge, employee_first_name=first, employee_last_name=last, gender=gender,
                       email=f"{first.lower()}.{last.lower()}@brickline.example", phone="9000000000",
                       state="Karnataka", city="Bengaluru", country="India")
        emp.save()
    info, _ = EmployeeWorkInformation.objects.get_or_create(employee_id=emp)
    info.date_joining = date.fromisoformat(doj)
    info.location = "Bengaluru, Karnataka"
    info.company_id = company
    info.save()
    # Horilla creates an empty "<name>'s Contract" (wage 0, starting today) for a new employee; remove it.
    Contract.objects.filter(employee_id=emp, wage=0).delete()
    people[badge] = emp

# Horilla only lets a user sign in when an Employee is linked to it. Link the admin login to
# Kavya Reddy (design director) so the company still has exactly six people.
from django.contrib.auth import get_user_model  # noqa: E402

admin_user = get_user_model().objects.filter(username=os.environ.get("HORILLA_ADMIN", "admin")).first()
Contract.objects.filter(employee_id__badge_id="HR001").delete()  # left over from an older version of this script
Employee.objects.filter(badge_id="HR001").delete()
if admin_user and not Employee.objects.filter(employee_user_id=admin_user).exists():
    kavya = people["BL004"]
    previous_user = kavya.employee_user_id
    Employee.objects.filter(pk=kavya.pk).update(employee_user_id=admin_user)
    if previous_user and previous_user.pk != admin_user.pk:
        previous_user.delete()

for badge, name, start, end, wage, appears in SEED["contracts"]:
    if stage == "june" and appears == "july":
        continue
    if Contract.objects.filter(contract_name=name).exists():
        continue
    # Older contract of the same person ends the day before the new one starts.
    for older in Contract.objects.filter(employee_id=people[badge], contract_end_date__isnull=True):
        if older.contract_start_date < date.fromisoformat(start):
            Contract.objects.filter(pk=older.pk).update(contract_end_date=date.fromisoformat(start) - timedelta(days=1),
                                                         contract_status="expired")
    Contract.objects.bulk_create([Contract(
        contract_name=name, employee_id=people[badge], contract_start_date=date.fromisoformat(start),
        contract_end_date=date.fromisoformat(end) if end else None, wage_type="monthly", pay_frequency="monthly",
        wage=wage, contract_status="active",
    )])

types = {}
for name, payment in (("Loss of Pay", "unpaid"), ("Sick Leave", "paid"), ("Casual Leave", "paid")):
    types[name] = LeaveType.objects.filter(name=name).first() or LeaveType.objects.create(name=name, payment=payment, total_days=12)

for badge, leave_type, start, end, days, status in SEED["leave"]:
    exists = LeaveRequest.objects.filter(employee_id=people[badge], start_date=start, leave_type_id=types[leave_type]).exists()
    if not exists:
        LeaveRequest.objects.bulk_create([LeaveRequest(
            employee_id=people[badge], leave_type_id=types[leave_type], start_date=date.fromisoformat(start),
            end_date=date.fromisoformat(end), start_date_breakdown="full_day", end_date_breakdown="full_day",
            requested_days=days, status=status, description="Brickline demo data",
        )])

# ---------------------------------------------------------------- organisation
def one(model, **fields):
    obj = model.objects.filter(**fields).first() or model.objects.create(**fields)
    if hasattr(obj, "company_id") and hasattr(obj.company_id, "add"):
        obj.company_id.add(company)
    return obj


employee_type = one(EmployeeType, employee_type="Full-time")
for other in ("Part-time", "Intern", "Contract"):
    one(EmployeeType, employee_type=other)
work_types = {name: one(WorkType, work_type=name) for name in ("Work from office", "Hybrid", "Remote")}

shift = one(EmployeeShift, employee_shift="General (09:30-18:30)", weekly_full_time="42:30", full_time="180:00")
for day in EmployeeShiftDay.objects.all():
    if day.day in ("saturday", "sunday"):
        continue
    if not EmployeeShiftSchedule.objects.filter(shift_id=shift, day=day).exists():
        schedule = EmployeeShiftSchedule.objects.create(shift_id=shift, day=day, minimum_working_hour="08:30",
                                                        start_time=time(9, 30), end_time=time(18, 30))
        schedule.company_id.add(company)
# Staying a few minutes past the 8h30 minimum is not overtime; only time beyond an extra hour is.
if not AttendanceValidationCondition.objects.filter(shift_ids=shift).exists():
    condition = AttendanceValidationCondition.objects.create(
        validation_at_work="09:30", minimum_overtime_to_approve="01:00", overtime_cutoff="02:00")
    condition.shift_ids.add(shift)
    condition.company_id.add(company)

for badge, (dept_name, position_name, role_name, work_type, manager) in SEED["org"].items():
    dept = one(Department, department=dept_name)
    position = one(JobPosition, job_position=position_name, department_id=dept)
    role = one(JobRole, job_role=role_name, job_position_id=position)
    EmployeeWorkInformation.objects.filter(employee_id=people[badge]).update(
        department_id=dept, job_position_id=position, job_role_id=role, shift_id=shift,
        work_type_id=work_types[work_type], employee_type_id=employee_type,
        reporting_manager_id=people[manager] if manager else None,
        email=people[badge].email, mobile=SEED["personal"][badge][7],
    )

for badge, (dob, marital, children, qualification, years, address, pin, phone) in SEED["personal"].items():
    Employee.objects.filter(pk=people[badge].pk).update(
        dob=date.fromisoformat(dob), marital_status=marital, children=children, qualification=qualification,
        experience=years, address=address, zip=pin, phone=phone,
    )

holidays = set()
for name, day in SEED["holidays"]:
    day = date.fromisoformat(day)
    holidays.add(day)
    if not Holidays.objects.filter(start_date=day, company_id=company).exists():
        Holidays.objects.create(name=name, start_date=day, end_date=day, company_id=company)

# ---------------------------------------------------------------- attendance
on_leave = {}
for badge, _type, start, end, _days, status in SEED["leave"]:
    if status == "approved":
        day = date.fromisoformat(start)
        while day <= date.fromisoformat(end):
            on_leave.setdefault(badge, set()).add(day)
            day += timedelta(days=1)

def clock(minutes):
    return time(*divmod(minutes, 60))


day_rows = {d.day: d for d in EmployeeShiftDay.objects.all()}
last_day = ATTENDANCE_TO.get(stage, ATTENDANCE_TO["july"])
for badge, emp in people.items():
    # Attendance must agree with leave: nobody is marked present on a day of approved leave.
    Attendance.objects.filter(employee_id=emp, attendance_date__in=on_leave.get(badge, ())).delete()
    have = set(Attendance.objects.filter(employee_id=emp).values_list("attendance_date", flat=True))
    work_type = work_types[SEED["org"][badge][3]]
    day = ATTENDANCE_FROM
    while day <= last_day:
        working = day.weekday() < 5 and day not in holidays and day not in on_leave.get(badge, ())
        if working and day not in have:
            rng = random.Random(f"{badge}{day}")  # same times every run
            clock_in = 9 * 60 + rng.randint(0, 45)  # 09:00-09:45
            worked = 8 * 60 + rng.randint(40, 85)   # 8h40-9h25, always above the 8h30 minimum
            Attendance(
                employee_id=emp, attendance_date=day, shift_id=shift, work_type_id=work_type,
                attendance_day=day_rows[day.strftime("%A").lower()],
                attendance_clock_in_date=day, attendance_clock_in=clock(clock_in),
                attendance_clock_out_date=day, attendance_clock_out=clock(clock_in + worked),
                attendance_worked_hour="%02d:%02d" % divmod(worked, 60),
                minimum_hour="08:30", attendance_validated=True,
            ).save()
        day += timedelta(days=1)

print(json.dumps({"stage": stage, "employees": len(people), "contracts": Contract.objects.filter(employee_id__in=people.values()).count(),
                  "leave_requests": LeaveRequest.objects.filter(employee_id__in=people.values()).count(),
                  "departments": len({o[0] for o in SEED["org"].values()}), "holidays": len(holidays),
                  "attendance": Attendance.objects.filter(employee_id__in=people.values()).count()}))
