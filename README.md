# Dynamic Payroll Compliance — H-02 V0

Payroll that handles changes: late raises (arrears), unpaid leave, one-off bonuses, and rule changes from a date —
with a reason for every number, maker/checker approval in a durable workflow, and payment files for PF, ESI,
professional tax and TDS. It reads HR data from **any** HR tool's screens with a browser agent (tested against
Horilla and a second, deliberately different HR tool), or from a spreadsheet.

> New here? Read **[GUIDE.md](GUIDE.md)**: how everything works, the 10-minute demo script, and how to add case C or a new PT rule.

## Start it (one command)

```bash
./scripts/start.sh          # macOS/Linux: Python 3.10+, Node 18+, and `brew install temporal`
open http://localhost:8080  # maker / maker123 prepares · checker / checker123 approves
```

`start.sh` creates the virtualenv, installs packages and Chromium, builds the screens, migrates, seeds Brickline
Design Co., and starts four processes: **Temporal** (state on disk in `var/temporal`), the **workflow worker**,
the **Demo HRMS** (:8100) and the **web app** (:8080). `./scripts/stop.sh` stops them; all state stays in `var/`.

Docker: `docker compose up --build` (same services plus Postgres). Horilla: `./scripts/horilla.sh setup && ./scripts/horilla.sh start` (see [integrations/horilla](integrations/horilla/README.md)).

Prepare the history before a demo (April–June processed as already paid):

```bash
cd backend && ../.venv/bin/python manage.py seed_demo --with-history   # from the spreadsheet
# or: read Horilla / Demo HRMS in the app first, then:  ../.venv/bin/python manage.py run_history
```

## Tests and the answer sheets

```bash
make test                     # 70 tests: engine, ingestion, AI boundaries, workflow rules, API, browser agent, answer sheets
make answers                  # human-readable report: which numbers match, per case
```

`answer_sheets/case_X/` holds `choices.json` (the company choices as setting values) and `expected.csv`.
For case C: create `answer_sheets/case_C/`, put the CSV there as `expected.csv`, write its choices into
`choices.json` (copy case A's and change values), then `make answers`. The CSV reader accepts long format
(month, employee, item, expected) or wide format (one column per item) and common column/item names.
The bundled A/B sheets were worked out **by hand** from the briefing; replace them with the official ones.

## The parts and how they connect

```
 HR tool screens ──(Playwright agent)──┐
                                       ├─► raw tables ─► field mapping (confirmed once) ─► snapshot (standard shape, checked)
 Spreadsheet upload ───────────────────┘                                                     │ maker reviews
                                                                                             ▼
 Rules & settings (dated versions) ─► input builder ◄─ confirmed changes (plain English → AI proposes → maker confirms)
                                         │   + months already paid (for arrears and TDS)
                                         ▼
                                  payroll_engine (pure: no DB, no AI, no clock) ─► result, every line with formula + reason + rule version
                                         │
             Temporal workflow per month: calculate → submit → wait for checker (signal) → approve → finalise → files
                                         ▼
                      register.csv · pf.csv · esi.csv · professional_tax.csv · tds.csv (reconciled to the register)
```

| Part | Where | Notes |
|---|---|---|
| Calculation | `backend/payroll_engine/` | Pure Python + `Decimal`. `calculate_month(input) -> result`. Same input, same bytes. |
| Rules & settings | `PolicyVersion` rows; seed in `data/rules/statutory_in.json` | Append-only, each with `effective_from`. Picked as of the 1st of the pay month. |
| Ingestion | `backend/payroll/ingestion/` | Agent (`browser_agent.py`), spreadsheet, mapping (`canonical.py`), standard snapshot + checks (`normalize.py`). |
| AI | `backend/payroll/ai/` | One gateway (`client.py`) that stores every answer; five allowed tasks (`tasks.py`). |
| Workflow | `backend/payroll/workflow/` | Temporal workflow (`definitions.py`), activities, dispatcher; `WORKFLOW_BACKEND=local` fallback. |
| Services | `backend/payroll/services/` | Month lifecycle with four-eyes, freshness and finalise-once checks; exports; changes. |
| API | `backend/payroll/api/` | Django REST Framework, session auth, roles via groups `maker` / `checker`. |
| Screens | `frontend/` | React (Vite), served by Django at `/`. |
| Demo HRMS | `mock_hrms/app.py` | A second HR tool: different names, three table layouts, paging, sessions that expire. |

## Where AI is used, and which model

Model: **Claude**, reached one of two ways (`AI_PROVIDER=auto` picks the first that is available):

1. **Claude Code in headless mode** (`claude -p`) — the default on a machine with no API key. If the `claude`
   CLI is installed and signed in, every AI call runs `claude -p --output-format json --json-schema <schema>
   --tools "" --no-session-persistence` from a neutral folder: no tools, no saved session, and the CLI checks the
   answer against the JSON schema. It uses your Claude Code login (model: the CLI's default, `claude-opus-5-5`
   here; override with `CLAUDE_CODE_MODEL`). Meant for demos, not production. Each call takes about 4–12 s.
2. **The Anthropic API** (`claude-opus-5-5` through the `anthropic` SDK) when `ANTHROPIC_API_KEY` is set.

If neither is available, or a call fails, a **rule-based** suggester fills the same five roles (clearly
labelled), so everything still works; the automated tests use that path so they never call a model.

AI is used only to: (1) find an HR tool's employee/salary/leave screens, (2) read records from a screen that
has no recognisable table, (3) suggest which source column means which payroll field, (4) turn a plain-English
instruction into a proposed change, (5) explain in plain words what changed since last month.

Guards (all plain code): every answer is stored in `AIInteraction` and shown in the **AI log**; the same
question is never asked twice (answers are reused by input fingerprint); a person confirms every mapping,
screen choice and change; an amount the AI proposes must literally appear in the typed instruction; values the
AI reads from a screen must appear on that screen; numbers in an AI summary are checked against the comparison.
`payroll_engine` cannot import AI, Django or the network — a test enforces it.

## Rules and company settings

Government rules (`pf`, `esi`, `professional_tax` per state, `income_tax`, `wage_definition`) and company
settings (`lop`, `pf_policy`, `rounding`, `esi_policy`, `pt_policy`, `tds_policy`, `arrears_policy`,
`salary_structure`) are JSON payloads in versioned rows with a start date. A change is a new row; a version a
submitted or final month used can never be edited or deleted. Each result line records the rule version it
used, so a paid month is explainable with the rules of its time. A draft re-run applies today's rules and
optional what-if settings to a month without touching the approved one.

## What does not work yet

* **Docker Compose was not run here** (no Docker on the build machine). Everything else was run end to end on macOS.
* Claude was run live **through Claude Code headless mode** (all five AI tasks and a full July). The direct API path (`ANTHROPIC_API_KEY`) follows the SDK's documented API but was not called (no key on the build machine).
* Income tax: new regime only (no old regime, declarations, previous-employer income, perquisites).
* Not modelled: ESI contribution-period continuation, EPS eligibility by age/joining date, EDLI and admin charges, half-yearly or annually capped PT states, gratuity/bonus acts.
* A browser read runs inside the web process; if the server restarts mid-read the read is marked interrupted and must be started again (months themselves are durable).
* The agent handles tables, ARIA grids and repeated div rows with paging; infinite scroll, CAPTCHA and MFA logins are not supported.
* HR-tool passwords are stored in the database for the demo; use a secret store in production.
* Writing changes back into Horilla (stretch goal) is not built. The Labour Codes rule and a second state's PT are.
