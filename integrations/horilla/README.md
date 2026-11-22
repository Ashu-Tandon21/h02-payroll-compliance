# Horilla

Horilla is one HR tool the agent reads. Nothing in the payroll system is specific to Horilla: the same
agent reads the bundled Demo HRMS, which names and lays out everything differently.

## Run Horilla with Brickline's data

Without Docker (tested, Nov 2026 Horilla, Python 3.12, SQLite):

```bash
./scripts/horilla.sh setup     # clone, install, admin/admin, enter Brickline data as it stood in June
./scripts/horilla.sh start     # http://localhost:8000
```

With Horilla's own Docker stack (`git clone https://github.com/horilla-opensource/horilla && cd horilla && docker compose up`):

```bash
docker compose exec web python manage.py createsuperuser --username admin --email admin@brickline.example
BRICKLINE_STAGE=june docker compose exec -T -e BRICKLINE_STAGE web python manage.py shell < /path/to/integrations/horilla/seed_brickline.py
```

## The July story

1. With Horilla at the June stage, read it from the payroll app and process April-June (`make demo-history`
   or open each month in the app).
2. HR enters Priya's raise in July: `./scripts/horilla.sh stage july` (adds a contract starting 1 May 2025).
3. Read Horilla again in the app. The field mapping confirmed the first time is reused without the AI.
   The raise is new, so it is recorded as first seen today: April-June stay as paid, and July pays arrears.

## How Brickline sits in Horilla

| Brickline | Horilla screen | Columns the agent maps |
|---|---|---|
| Employees | Employees (`/employee/employee-view/`) | `Employee` holds "Priya Sharma (BL001)"; the name and ID are split |
| Salary | Contracts (`/payroll/view-contract/?contract_status=...`) | `Start Date` → salary effective from; `Contract wage` → monthly salary (split 50/20/30 by the company's salary structure setting) |
| Leave | Leave Requests (`/leave/request-view/`) | `Leave Type`, `Start Date`, `End Date`, `Requested Days`, `Status` |

So Horilla looks like a real company's, the seed also enters, for display only (pay does not use them):

* **Organisation:** four departments (Design, Studio Ops, Leadership, Client Services), a job position and
  job role per person, reporting managers (Kavya at the top), employee type Full-time, and work types
  (Work from office / Hybrid). It also adds personal details: date of birth, address and phone.
* **Shift and holidays:** a Mon-Fri "General (09:30-18:30)" shift with an 8h30 minimum, and Karnataka 2025
  holidays (Mahavir Jayanti, Ambedkar Jayanti, Good Friday, May Day, ...).
* **Attendance:** one validated check-in/check-out per working day from 1 April to the end of the stage's
  month (June or July). Weekends, holidays and approved leave are skipped, so attendance agrees with
  the leave the payroll system reads. Sneha's rejected leave day shows as a normal working day. Times are
  random but the same on every run. Overtime counts only beyond one extra hour, so no day waits for
  overtime approval.

## The Horilla profile

What we know about Horilla is written down in `profile.json` (see GUIDE.md 6.1):

* its screens, including the contract list address with every status;
* its list screens and its Excel exports (Employees work info, Contracts, Leave Requests), with what each column
  means.

`fixtures/` holds real exports made from the seeded data, with every contract status and Leave Type ticked.
Uploading them, or reading the screens, needs no AI. The mapping is pre-filled and the maker confirms it once.

Quirks the profile handles:

* **Contracts export:** calls the contract wage "Basic Salary". It is the monthly wage and is mapped to monthly
  gross (the screen calls it "Contract wage").
* **Employees export:** has a "Basic Salary" that is 0. It is not used; pay comes from contracts.
* **Leave Requests export:** leaves "Leave Type" unticked by default. The profile asks for it, otherwise every
  leave would count as unpaid.

Things learned from the real screens:

* Horilla only lets a user sign in when an Employee is linked to it; the seed links `admin` to Kavya Reddy.
* Horilla creates an empty "<name>'s Contract" (wage 0) for every new employee; the seed removes them.
* The Contracts list shows only active and draft contracts unless asked. Arrears need the old contract,
  so the screen address asks for every status. If an old salary is still missing, the payroll system
  does not guess: it warns that arrears for that month could not be checked.
* The list screens show no "of N" total, so completeness is checked by other means (every employee has a
  salary, every leave row belongs to a known employee) and the maker reviews the counts.

Result of the full run against Horilla: all 41 July numbers of answer sheet A match.
