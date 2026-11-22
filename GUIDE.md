# Guide: how this payroll system works

This guide explains what was built for H-02, how the pieces fit, why they were designed this way, and how to
run the demo. Read it top to bottom once; afterwards use it as a reference.

---

## 1. The idea in one page

A payroll month is a **pure calculation** over three inputs:

1. **HR facts**: who works here, their salary (with start dates), and their unpaid leave.
2. **Rules and company choices that apply on that date**: PF, ESI, professional tax, income tax; how a day of
   unpaid leave is valued; whether PF is capped; how amounts are rounded…
3. **What was already paid** in earlier months (needed for arrears and for TDS).

Everything else exists to get those inputs right, to let people check them, and to make the month safe to
approve:

* An **agent** reads HR facts from any HR tool's screens (or a spreadsheet) and turns them into one standard
  shape. A person confirms what each column means **once**.
* **Rules and settings are data** with start dates. Changing a rule = adding a dated version.
* **AI** only helps with reading and understanding (screens, column names, typed instructions, summaries). It
  never produces a number, picks a rule or approves anything. Every AI answer is saved and confirmed by a person.
* The **engine** (`backend/payroll_engine`) is pure Python with `Decimal`: no database, no network, no clock, no
  AI. The same input gives the same result, byte for byte. Every number is a `Line` with a formula, a reason and
  the rule versions it used.
* A **Temporal workflow** runs each month: calculate → submit → wait for a second person → approve → finalise
  → write files. Waiting costs nothing and survives restarts of every service.

---

## 2. Folder map

```
backend/
  payroll_engine/          the calculation (pure)
    engine.py              calculate_month / calculate_employee: LOP, arrears, PF, ESI, PT, TDS, net, employer cost
    policies.py            rule/setting keys, defaults, validation, resolve() = which version applies to a month
    tax.py                 annual income tax from a slab table (rebate, marginal relief, surcharge, cess)
    diff.py                compare two results: what moved and why (salary, arrears, one-time, unpaid days, rule)
    money.py, periods.py   Decimal rounding, ₹ formatting; months and financial years
  payroll/                 the Django app
    models.py              Company, PolicyVersion, Employee, Connector, IngestionJob, RawTable, FieldMapping,
                           DataSnapshot, PayrollRun, ChangeRequest, RunEvent (audit), AIInteraction
    ingestion/             browser_agent.py (Playwright), spreadsheet.py, canonical.py (fields + rule-based
                           matcher), normalize.py (standard snapshot + completeness checks), jobs.py, values.py
    ai/                    client.py (the only door to the model), tasks.py (the five allowed AI tasks)
    services/              runs.py (month lifecycle), inputs.py (builds the engine input), changes.py,
                           snapshots.py, policies.py, exports.py (register + challans), setup.py
    workflow/              definitions.py (Temporal workflow), activities.py, dispatch.py
    api/                   views.py, present.py, urls.py
    answer_sheet.py        runs an answer-sheet case end to end and compares
    management/commands/   seed_demo, run_history, run_worker, check_answer_sheet, import_spreadsheet
  tests/                   70 tests
frontend/src/              React screens (pages/Run.jsx is the month screen)
mock_hrms/app.py           Demo HRMS: a second, different HR tool
data/rules/statutory_in.json   government rules as dated data
data/brickline/            seed.json (the made-up company) and brickline_hr_data.xlsx (made from it)
answer_sheets/case_A|B/    choices.json + expected.csv (hand-worked from the briefing)
integrations/horilla/      seed script and notes for Horilla
scripts/                   start.sh, stop.sh, horilla.sh
```

---

## 3. A month, step by step (and which code does it)

### 3.1 Getting HR data

**From an HR tool (the agent)** — screen *HR data → Read from an HR tool*.

1. **Find screens** (`BrowserAgent.discover`): the agent opens the tool, finds the password box on the login
   page (no tool-specific selectors), signs in, and collects every menu link. It then asks the AI which links
   lead to employees, salary and leave (`ai/tasks.py: discover_pages`). Without a key, a keyword matcher does
   it. The suggestion is saved; **a person confirms the screens** (they can edit the addresses, e.g. to add a
   filter). Confirmed screens are reused on every later read.
2. **Read** (`BrowserAgent.read_all`, runs in the background, live log on screen):
   * opens each confirmed screen; reads whatever looks like a table: real `<table>`s, ARIA grids
     (`role=table`), or repeated `<div>` rows with a header row (Horilla and many modern tools use these);
   * follows "Next" links/buttons until there are none (stops if a page repeats);
   * compares the rows read with any "Showing 1–4 of 6" text on screen;
   * if the login page appears mid-read (**session expired**), signs in again and continues from the same page;
   * if a screen has no table at all, asks the AI to read the visible text — and keeps the answer only if every
     value literally appears on the screen; otherwise reports "the screen may have changed";
   * screenshots every page for the audit trail.

**From spreadsheets** — any `.xlsx` (one table per sheet, header row detected) or `.csv`. Companies keep these
lists differently, so the upload takes either **one workbook with everything** or **a separate file per kind of
data** (Employees, Salary, Leave; any box can stay empty). All files of one upload become one read. A file
uploaded as Leave is called "Leave" whatever the file is named, so next month's `leave_august.csv` reuses the
confirmed mapping. A list that arrives later can be added to the read (**Add another file** on the read's page)
until its snapshot is built. API: `POST /api/companies/<id>/uploads` and `POST /api/jobs/<id>/files`, with form
fields `employees`, `compensation`, `leave` or `file` (anything).
Samples: `data/brickline/brickline_hr_data.xlsx` (everything) and the same data split into three files in
`data/brickline/separate_files/` (`employee_master.xlsx`, `salary_contracts.xlsx`, `leave_july.csv`).

Both produce **raw tables** (`RawTable`), and from here on there is one path.

### 3.2 Field mapping: "Basic" vs "Basic Salary"

Each raw table's columns are mapped onto standard payroll fields (`ingestion/canonical.py: TARGETS`), e.g.
`employee.code`, `comp.basic`, `comp.monthly_gross`, `comp.effective_from`, `leave.type`, `leave.days`,
`attendance.unpaid_days@2025-07`. Each field has known names ("Basic", "Basic Salary", "Basic Pay", …).

Where a suggestion comes from, in order (no AI unless the first three have no answer):

1. **This company's confirmed mapping** for the same columns: reused as it is.
2. **A known HR tool's profile** (`integrations/<system>/profile.json`, see 6.1): the mapping is pre-filled and
   labelled "From the Horilla profile", with the profile's notes. The maker still confirms it once.
3. **The rule-based matcher** (`canonical.heuristic_mapping`).
4. **AI**, only if the company allows it, and only after "Suggest mapping".

* The AI (or the rule-based matcher) **suggests** a mapping with a confidence and a reason.
* For leave, it also proposes which **leave types** cut pay ("Loss of Pay" → unpaid, "Casual Leave" → paid)
  and which **statuses** count ("Approved" counts, "Rejected" doesn't).
* A person **confirms** it on screen. The confirmed mapping is stored against a **fingerprint** of the column
  names. Next read with the same columns → reused automatically, **no AI asked**.
* If the screen later loses a mapped column, the read is flagged **"screen changed"** and the person must
  confirm again (tested by switching the Demo HRMS to `MOCK_HRMS_VARIANT=v2`).

### 3.3 The snapshot (standard shape) and the maker's review

`normalize.build_snapshot` turns mapped tables into one standard snapshot:
`employees`, `compensation` (salary records with `effective_from` and `recorded_on`), `leave`, `attendance`.
It handles things like "Priya Sharma (BL001)" in one cell, a total salary only (split by the company's salary
structure 50/20/30), a salary with only Basic listed (the rest is split by the structure), a raise on the same
row ("New salary", "Raise from"), month columns ("LOP days – July 2025"), leave spanning two months.

Then **checks** (shown in green/amber/red): rows read vs. the count on screen, every employee has a salary,
every salary/leave row belongs to a known employee, unreadable values, conflicting salaries, which leave types
count as unpaid, unpaid days per month, **no leave read at all** (e.g. the leave file was forgotten), and a file
uploaded as Salary or Leave that has no salary or leave columns. Any red check blocks the data from being used.

The snapshot is **immutable** and has a fingerprint of its facts (the same facts from the spreadsheet and from
an HR tool give the same fingerprint). The **maker** reviews it and marks it reviewed.

**When was a raise recorded?** Each salary record carries `recorded_on`. If the source has no such column, the
system uses the date it *first saw* the record. A month is calculated with the salary records known by its
"knowledge date" — so April–June (processed before the raise was known) keep ₹80,000, and July (which knows the
raise from 1 May) pays the difference as arrears. Months already paid are never rewritten.

### 3.4 Opening the month and typing changes

*Payroll months → Open*. The month pins the reviewed snapshot. The maker presses **I have reviewed the data**.

Changes in plain English (`services/changes.py`): "Give Priya a ₹10,000 one-time bonus this month".

1. The AI turns it into a proposal: type `one_time_earning`, employee "Priya", item `bonus`, amount **as written**
   "₹10,000". (Without a key, a rule-based parser does the same for common phrasings.)
2. Plain code checks it: the employee resolves to exactly one person (else "Priya could be Priya Sharma or
   Priya Nair"); the amount **must literally appear in the sentence** (the AI cannot compute "half a month's
   salary"); the month is this month.
3. The screen shows **"The system understood: Pay a one-time bonus of ₹10,000 to Priya Sharma (BL001) in July
   2025. It is taxable and is not part of PF wages."** The maker confirms or discards.

Supported changes: one-time earning (bonus, incentive, reimbursement…), one-time deduction (advance/loan
recovery…), set unpaid days, salary revision from a date (split by the salary structure).

### 3.5 Calculate

`services/inputs.build_run_input` gathers: the snapshot (filtered by knowledge date), confirmed changes, the
rule versions that have started by the 1st of the month, what-if overrides (drafts only), and for each employee
the **months already paid** (for TDS) and the **paid amounts of past months** (for arrears). The engine then
runs. Input and result are stored with SHA-256 fingerprints.

### 3.6 Submit → wait → approve → files

* **Submit** (maker): blocked if anything changed since the calculation (data, rules, changes, earlier months),
  if a proposed change is unconfirmed, or if the result has errors (e.g. negative net pay).
* The month **waits** for a checker. With Temporal, nothing runs while waiting; restart everything and it is
  still waiting.
* **Approve** (checker, a **different person** from the maker): the system re-checks freshness (if HR data was
  read again and differs, approval is refused with the reason), recalculates from the stored input to prove the
  result is identical, then approves.
* **Finalise** (automatic after approval): writes `register.csv`, `pf.csv`, `esi.csv`,
  `professional_tax.csv`, `tds.csv` (one row per employee, due dates from the rules, no government IDs), reads
  them back and reconciles every total with the register. A month can be final only once (database
  constraint + idempotent step).
* **Send back** (checker, with a reason): the maker fixes, recalculates, resubmits.

---

## 4. The calculation in detail (`payroll_engine/engine.py`)

All money is `Decimal`. Rounding is a company setting (`rounding.method`: nearest/up/down/nearest_even;
`rounding.earnings`: `per_component` rounds each part of pay, `gross_only` rounds only totals).

| Step | How | Example (July, case A) |
|---|---|---|
| Monthly rate | Salary record in force; if one starts mid-month, each part of the month at its own rate by calendar days | Priya ₹90,000 (45,000 / 18,000 / 27,000) |
| Unpaid days | Per-month figure if the source gives one; else approved unpaid leave in the month (spanning leave split by calendar days); plus days before joining / after leaving | Arjun 3 |
| Unpaid leave cut | rate ÷ divisor × days per part of pay. Divisor = setting `lop.divisor`: days in month, 30, 26… | 18,000 ÷ 31 × 3 = ₹1,742 |
| Arrears | Each paid month in the lookback window is **recalculated with its own stored rules** and today's known salary; difference minus arrears already paid later | May and June: ₹10,000 each |
| One-time items | From confirmed changes | Bonus ₹10,000 |
| Gross | regular + arrears + one-time | ₹1,20,000 |
| PF | 12% of PF wages (basic + DA after LOP [+ Labour Codes add-back]); `pf_policy.wage_base` capped at ₹15,000 or full; PF on arrears = recalculated PF of revised months − PF paid | min(45,000, 15,000) × 12% = ₹1,800 |
| ESI | If the eligibility wage (setting: gross this month / regular / contracted) ≤ ₹21,000: 0.75% (company 3.25%) of ESI wage, rounded **up** | Arjun 0.75% × 16,258 = 121.94 → ₹122 |
| PT | State rule by employee's work state (company state if missing): slab table with optional gender and month (February) slabs | ≥ ₹25,000 → ₹200 |
| TDS | Projected year = already paid this FY + this month + current salary × months left; − standard deduction; tax by slabs (87A rebate with marginal relief, surcharge, 4% cess); (year's tax − TDS already deducted) ÷ months left including this one | Kavya (1,50,800 − 37,701) ÷ 9 = ₹12,567 |
| Net | gross − PF − ESI − PT − TDS − other deductions | Priya ₹1,18,000 |
| Company cost | gross + company PF + company ESI | |

Each line carries `formula` (the arithmetic), `reason` (plain words), `rules` (key, version id, start date,
scope: government / company / what-if / default) and `children` (e.g. arrears per month, PF wages, TDS
projection, tax slabs). That is what the drawer shows when you click any number.

---

## 5. Rules and settings as data

Stored in `PolicyVersion` (seeded from `data/rules/statutory_in.json`). Fields: `key`, `jurisdiction` (`IN`,
`IN-KA`…; blank for company settings), `effective_from`, `payload` (JSON, validated), `title`, `note`.

* **Picking a version**: for a month, the latest version whose start date is on or before the **1st of that
  month** (`policies.resolve`). A rule starting 15 July therefore applies from August (the screen warns).
  Versions from April and August → July uses April's.
* **Never edited**: saving an existing version raises an error; a version used by a submitted/final month
  cannot be deleted.
* **Company settings** fall back to documented defaults when none is stored, and the result says so.

### Adding the "new professional tax rule" on demo day (target: under 5 minutes)

*Rules & settings → Professional tax · IN-KA → + New version* (or **+ Professional tax for a state** for a
new state). The form starts as a copy of the current rule, so only what changes needs typing:

1. **Starts on**: e.g. 01/06/2025 (rules are read as of the 1st of each pay month).
2. **Title** and **Note / source** (notification number, circular…).
3. In the slab table change **Tax** on the "₹25,000 or more" row from 200 to 300. Each row has: *when monthly
   pay is* (₹ or more / more than ₹), an optional *up to*, the *tax* (fixed ₹ or % of pay), *for* (everyone /
   men / women) and *in* (every month or one month, e.g. February only). **+ Add a slab** adds a row.
4. **Save version**.

Then open the paid month and use **What if? → Run a draft and compare**: the draft picks up the new rule and the
comparison shows which PT amounts changed and why. Months before the start date are unchanged (a draft re-run of
June gives the identical fingerprint — tested). Remove a demo version with **Remove** on its card.

Every rule and company setting has a form like this (percentages are typed as percentages, amounts in ₹), and
**Details** on a version shows it in words. No JSON is shown; **Edit as JSON (advanced)** at the bottom of the
form is there for anyone who prefers it.

### Company choices for a new case (case C)

Write them in `answer_sheets/case_C/choices.json` using these settings:

| Choice in the answer sheet | Setting |
|---|---|
| Unpaid day = pay ÷ days in month / 30 / 26 | `lop.divisor`: `"days_in_month"` / `30` / `26` |
| Day rate rounded first (e.g. to paise) | `lop.day_rate_rounding`: `{"unit": "0.01", "method": "nearest"}` |
| PF on first ₹15,000 / all of basic | `pf_policy.wage_base`: `"capped"` / `"full"` |
| PF on arrears or not | `pf_policy.arrears_contribution`: `"recompute_delta"` / `"none"` |
| Round each part of pay / only the total | `rounding.earnings`: `"per_component"` / `"gross_only"` |
| Rounding direction | `rounding.method`: `"nearest"` / `"up"` / `"down"` / `"nearest_even"` |
| ESI eligibility/contribution on gross, regular pay, or full salary | `esi_policy.eligibility_wage` / `contribution_wage`: `"gross_this_month"`, `"gross_excluding_one_time"`, `"regular_earned"`, `"contracted_monthly"` |
| PT on gross or regular pay | `pt_policy.basis`: same choices |
| Tax on a bonus spread or taken in the month | `tds_policy.one_time_income`: `"spread"` / `"tax_in_month"` |
| Annual tax rounded to ₹10 | `tds_policy.annual_tax_rounding_unit`: `10` |

---

## 6. AI: where, which model, and how it is kept in its lane

Claude is reached through `payroll/ai/client.py`, one of three ways (`AI_PROVIDER`):

* `claude-code` — **Claude Code in headless mode**, used automatically when there is no API key and the `claude`
  CLI is installed and signed in. Each call is
  `claude -p --output-format json --json-schema '<schema>' --tools "" --no-session-persistence --system-prompt '<task rules>'`
  with the question on stdin, run from a neutral folder. The CLI validates the answer against the schema and
  returns it as `structured_output`. Uses your Claude Code login; ~4–12 s per call. Not meant for production.
* `anthropic` — the API through the official SDK (needs `ANTHROPIC_API_KEY`), structured JSON output, low effort,
  server-side refusal fallback.
* `off` — rule-based suggesters with the same output shape (labelled "Rule-based" everywhere). Also the
  automatic fallback if a model call fails, with the failure recorded on the AI answer.

`auto` (default) picks API → Claude Code → rule-based. The sidebar, Overview and AI log show which is in use.
Claude Code is **refused when `DEBUG` is off** (unless `AI_ALLOW_CLI=1`): it runs on one person's login.

| Allowed task | Function | Person confirms | Plain-code check |
|---|---|---|---|
| Find the HR tool's screens | `discover_pages` | screens | URL must be a link on the page |
| Read a screen with no table | `extract_records` | data review | every value must appear on the screen |
| Suggest field mapping | `suggest_mapping` | mapping (once) | unknown fields/columns dropped; duplicate targets refused |
| Instruction → proposed change | `parse_instruction` | each change | amount/days must be written in the sentence; employee must be unambiguous; month must match |
| Explain what changed | `summarize_changes` | (informational) | every number in the text must be in the comparison |

* **Saved**: every answer (and failure) is an `AIInteraction` row: prompt, response, model, latency, status,
  checks, who reviewed it. See the **AI log** screen.
* **Asked once**: answers are keyed by a fingerprint of the question; asking again returns the saved answer
  (`reuse_count` goes up). A re-run of a month never calls the AI: it uses confirmed changes and mappings.
* **Never a number**: `payroll_engine` imports nothing from AI, Django or the network (enforced by
  `tests/test_ai_boundaries.py`).

### 6.1 Going to production: known HR tools first, AI per company

The AI is a fallback, not the way in. What makes this work for many companies is the **profile library**.

**Profiles (`integrations/<system>/profile.json`).** One file per HR tool:

* how to recognise it (`detect`, e.g. the page title after signing in);
* its screens (`screens`), with notes such as "ask for every contract status";
* every known layout of its screens and exports (`layouts`):
  * `match_on`: the columns that identify the layout;
  * `columns`: column → payroll field;
  * `needs`: a column that must be there, with how to fix a file without it;
  * `notes`: shown to the maker.

The Horilla profile (`integrations/horilla/profile.json`) covers:

* its list screens;
* its Employees (work info), Contracts and Leave Requests Excel exports. The real exports are in
  `integrations/horilla/fixtures/`.

`tests/test_profiles.py` reads those exports with no AI, and July matches answer sheet A on every number.
Profiles are checked when loaded: an unknown payroll field or a `match_on` column without a mapping stops the app.

How a profile is used:

* Adding a connection: pick the tool under **Which HR tool?** and its screens are filled in. **Find screens**
  also recognises a known tool after signing in, and then uses the profile instead of the AI.
* Uploads and reads: a sheet or screen that matches a layout is pre-filled. Columns the profile does not list
  are only matched when their name is an exact known name; otherwise they are left as "Not used" for the maker.
* Nothing is confirmed by a profile. The maker at each company confirms once, then it is reused.

**Learning new tools as we meet them.** Each company's confirmed mappings are already reused within that company.
To share across companies, run `manage.py profile_candidates`:

* it groups confirmed mappings that match no profile by tool and column set;
* it counts how many companies agree, and lists where they differ;
* it writes drafts to `var/profile_candidates/<system>.json`.

The drafts hold column names, payroll fields and leave type/status decisions, never row data. An engineer then
reviews a draft, adds a sample export with made-up data under `integrations/<system>/fixtures/` and a test, and
commits it as `profile.json`. No company's mapping reaches another company without that review.

**AI in production.**

* `AI_PROVIDER=anthropic` with `ANTHROPIC_API_KEY` (your organisation's key), or `AI_PROVIDER=off`. Use the
  Claude API through a cloud provider region if HR data has to stay in a region.
* **Per company:** Rules & settings → "AI for <company>": **allowed** or **off** (who changed it and when is
  kept). With AI off, nothing is sent to a model; profiles, rules and people do the work, and the AI log says
  "AI is turned off for this company".
* **What is sent when allowed:**
  * field mapping: column names, plus sample values **masked to their shape** ("Priya Sharma (BL001)" →
    "Aaaaa Aaaaaa (AA999)"). Category words like "Approved" and "Loss of Pay" are kept.
  * screen discovery: link texts.
  * reading a table-less screen: that screen's text.
  * instructions: the sentence and employee names.
  * summaries: the month-to-month differences.
* Every answer is still stored, reused, checked by plain code and confirmed by a person, as above.

---

## 7. The workflow (Temporal)

`workflow/definitions.py: PayrollMonthWorkflow` — one per regular month (id `payroll-<company>-<month>-run<id>`).
It loops: wait for a **signal** (`calculate`, `submit`, `approve`, `reject`, `cancel`), run the matching
**activity** (`workflow/activities.py` → `services/runs.py`), record the state. After `approve` it runs
`finalize` (retried until it succeeds) and completes.

* **Waiting days** costs nothing: the workflow is a record in Temporal's database, not a running process.
* **Restarts**: Temporal's dev server is started with `--db-filename var/temporal/temporal.db`, so its state is on
  disk; the worker replays the workflow history when it comes back. A calculation cut off mid-way is retried;
  it is safe because the step is deterministic and transactional.
* **Rules live in the services**, not in the workflow: every step re-checks state under a row lock (four-eyes,
  freshness, finalise-once). The API runs the same checks first so a person gets an instant answer.
* **Duplicates**: each button press carries a request id; the workflow ignores a repeat. Finalising twice is a
  no-op; a database constraint allows only one final run per month.
* `WORKFLOW_BACKEND=local` runs the same steps in-process (state in the database only) if Temporal is not available.

**Restart test**: submit July, then `./scripts/stop.sh && ./scripts/start.sh` (or kill Temporal and the worker
by hand), open July: still "Waiting for approval", workflow RUNNING in the Temporal UI (http://localhost:8233).
Approve as checker → final. (Verified while building this.)

---

## 8. The screens

* **Overview** — months as a timeline with status and net pay; a banner for the checker when a month waits.
* **HR data** — connect an HR tool (find screens → confirm → read), upload one workbook or a file per kind of data, recent reads,
  snapshots. A read shows the agent's live log, screenshots, the field mapping editor, and leave-type choices.
  A snapshot shows checks, unpaid days by month, and the data.
* **Month** (`#/runs/<id>`) — the stepper (Review data → Changes → Calculate → Submit → Checker approves →
  Files), then:
  1. HR data and the maker's review;
  2. changes in plain English with "the system understood…" and Confirm;
  3. **what changed since last month**: totals with deltas, each changed employee with the *why* (salary changed,
     arrears for May and June, bonus added, 3 unpaid days…), the amounts that moved, and an optional AI summary;
  4. the **register** — click any number for the line-by-line drawer (formula, reason, rule version, what it
     was last month);
  5. **approval** — what is being approved, maker vs. checker, "same result the maker submitted", "no input
     moved";
  6. **files** with checksums and the reconciliation;
  * **What if?** — draft re-run with today's rules and optional settings (LOP divisor, PF base, rounding…);
  * **Same inputs, same result** — recalculate from the stored input and compare fingerprints;
  * **Audit trail**.
* **Rules & settings** — every rule/setting with its versions on a timeline ("in force now", "starts later",
  "used"), add a version with validation.
* **AI log** — model in use, allowed/forbidden uses, every answer with prompt and response.
* **Labour Codes tool** — the 50% wage rule for a made-up employee with basic at 40%, before/after the rule's
  start date, and three ways to restructure pay (all numbers from the engine).

---

## 9. The 10-minute demo, mapped to the brief

Before the demo (once): `./scripts/start.sh`; for Horilla `./scripts/horilla.sh setup && ./scripts/horilla.sh start`.
Read Horilla in the app (HR data → Horilla → Find screens → Confirm → Read → confirm mapping → Build snapshot →
Data looks right), then `cd backend && ../.venv/bin/python manage.py run_history` (April–June as paid). Then
`./scripts/horilla.sh stage july` (HR enters Priya's raise).

1. **Connect to Horilla** — HR data → Horilla → *Read data now*. Watch the log; open the read: the mapping was
   confirmed earlier and is reused (no AI). Build the snapshot.
2. **Open July** — the maker reviews the data (checks, unpaid days by month: Arjun 3 in July) → *Data looks
   right* → Overview → *Open next month* → *I have reviewed the data*.
3. **Type** "Give Priya a ₹10,000 one-time bonus this month" → see *The system understood* → *Yes, that's right*.
4. **Calculate** — "What changed since June": Priya (salary 80k→90k, arrears ₹20,000 for May and June, bonus
   ₹10,000), Arjun (3 unpaid days), Ravi (no unpaid day this time).
5. **Click** Priya's *Why? Line by line* → arrears per month, PF stays ₹1,800 (cap), TDS ₹0 (₹10.05 lakh < ₹12
   lakh). Click any other number in the register.
6. **Submit**. Restart everything (`./scripts/stop.sh && ./scripts/start.sh`). July is still waiting (Temporal UI too).
7. Sign in as **checker** → *Approve July 2025* → register and payment files with the reconciliation.
8. **What if?** on the final July: choose "pay ÷ 30" → *Run a draft and compare* → Arjun's cut becomes ₹1,800,
   with the reason. Or add the new PT rule (section 5) and run a draft: the PT changes are listed; June stays the same.

Without Horilla, use the **Demo HRMS** connector the same way (or upload `data/brickline/brickline_hr_data.xlsx`).
"Loading July from the spreadsheet gives the same numbers": both routes produce the same snapshot fingerprint
for the same facts (tested) and the same July (Horilla run: 41/41 answer-sheet numbers).

---

## 10. Tests

`make test` runs:

* `test_engine.py` — every worked example of the briefing (Meera, Arjun, Kavya, Priya), LOP divisors and
  rounding choices, arrears (incl. not paying twice, not guessing a missing salary), PT slabs (gender,
  February), marginal relief, mid-month salary change and joiners, the Labour Codes rule, determinism.
* `test_ingestion.py` — "Basic"/"Basic Salary"/"Basic Pay", Horilla's and Keka-like headers, amounts/dates in
  many formats, one-row-per-employee sheets with a raise column, leave filters, completeness checks, and
  "spreadsheet and HR tool give the same facts".
* `test_ai_boundaries.py` — the engine imports no AI/DB/network/clock; amounts not in the instruction are
  refused; AI answers are reused; ambiguous names are not guessed; invented numbers in summaries are flagged.
* `test_workflow_rules.py` — full month with four-eyes, finalise-once, July twice = same fingerprint,
  rejection + stale HR data, setting change moves Arjun's pay with the reason, new PT rule as a data edit
  (approved July unchanged, June unchanged), used versions cannot be removed or edited.
* `test_api.py` — the month through HTTP with roles.
* `test_agent.py` — the real browser agent against the Demo HRMS: discovery, paging, three table layouts,
  session expiry, counts, mapping reuse without AI, changed screen detected.
* `test_answer_sheets.py` — every case folder in `answer_sheets/` must match 100%.

---

## 11. Answers to the brief's "questions to explore" (short)

* *Basic vs Basic Salary?* Known names per field + AI suggestion; a person confirms; stored per column-set fingerprint.
* *Confirm mapping once?* Confirmed mappings are reused by fingerprint, no AI; a changed screen asks again.
* *Read everything?* On-screen totals vs rows read; paging to the end; every employee has a salary; every
  row belongs to a known employee; unpaid days per month shown to the maker.
* *Screen changed / login expired?* Missing mapped columns → "screen changed", person confirms again; login page
  mid-read → sign in again and resume from the same page (limit 5).
* *Spreadsheet in the same shape?* Both become raw tables → same mapping → same snapshot builder.
* *New rule version without changing the old?* Append-only dated versions.
* *July with versions from April and August?* Latest started by 1 July → April's.
* *Arrears for May/June entered in July?* Recalculate May and June with their own stored rules and today's
  known salary; pay the difference minus anything already paid; PF on arrears as a recalculated difference.
* *Calculation free of screens/DB/AI?* `payroll_engine` takes a JSON input; enforced by a test.
* *Reason on each number?* `Line(formula, reason, rules, children)`.
* *Wait days without a busy process / restart?* Temporal workflow with on-disk state; signals; replay.
* *Re-run uses saved AI answers?* Confirmed changes/mappings feed the input; AI answers cached by fingerprint.
* *Checker rejects / HR changes data?* Send back with reason; approval refuses stale data and names the reason;
  maker switches to the new data, reviews, recalculates.
* *Finalised twice?* Idempotent step + unique constraint.
* *First thing the finance head sees?* Status, the next action for their role, and what changed since last month.

---

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| "The workflow engine (Temporal…) is not reachable" | `brew install temporal`, then `./scripts/start.sh`; or set `WORKFLOW_BACKEND=local` |
| Agent: "Could not start the browser" | `.venv/bin/playwright install chromium` |
| Horilla login fails | the login user needs a linked Employee (the seed links `admin` to Kavya) |
| "Every screen must be on …" when confirming screens | each connection reads one HR tool: screen addresses must be paths on its own web address (Demo HRMS: `/people`, `/compensation`, `/time-off`; Horilla has its own connection) |
| Signed out of the payroll app after using Horilla (or another Django app on localhost) | fixed: the app's cookies are named `payroll_sessionid` / `payroll_csrftoken`; sign in once more after updating |
| Arrears warning "No salary on record for April" | the HR data lacks the old salary; for Horilla make sure the Contracts screen address asks for all statuses |
| AI shows "Rule-based" | install Claude Code and sign in (`claude` once, interactively), then restart; or set `ANTHROPIC_API_KEY` |
| AI answers have "Claude was not used: …" | the `claude -p` call failed (not signed in, timeout); run `claude -p "hi"` in a terminal to see why |
| Want a clean start (keeps Horilla) | `./scripts/stop.sh && rm -rf var/payroll.sqlite3* var/temporal var/media && ./scripts/start.sh` |
