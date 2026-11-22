"""Gather everything one month's calculation needs into a single JSON input.

This is where the database is read. The engine then gets a plain dict and nothing else,
so a stored input can be calculated again at any time and must give the same result.
"""
from __future__ import annotations

import copy
from datetime import date

from payroll_engine import policies as engine_policies
from payroll_engine.money import d, to_str
from payroll_engine.periods import Period, parse_date

from ..ingestion.normalize import unpaid_days_for
from ..models import ChangeRequest, PayrollRun
from .policies import versions_for


def _in_period(emp: dict, period: Period) -> bool:
    doj = parse_date(emp.get("date_of_joining"))
    doe = parse_date(emp.get("date_of_exit"))
    if doj and doj > period.end:
        return False
    if doe and doe < period.start:
        return False
    return True


def _known_compensation(rows: list[dict], knowledge_date: date) -> list[dict]:
    """Salary records HR had recorded by ``knowledge_date``.

    This is how a late raise turns into arrears: May was paid before the raise was recorded,
    so May's stored result has the old salary, and July (which knows the raise) pays the gap.
    """
    known = [r for r in rows if not r.get("recorded_on") or parse_date(r["recorded_on"]) <= knowledge_date]
    if not known and rows:
        known = [min(rows, key=lambda r: r.get("effective_from") or "")]
    return [{"effective_from": r.get("effective_from"), "components": r["components"]} for r in known]


def override_versions(run: PayrollRun, versions: list[dict]) -> list[dict]:
    """What-if setting values of a draft re-run, as extra versions that beat stored ones for this month only."""
    out = []
    period = run.period_obj
    for key, values in sorted((run.policy_overrides or {}).items()):
        current = engine_policies.resolve_company(versions, key, period)
        payload = copy.deepcopy(current.payload)
        payload.update(values)
        out.append({
            "id": f"what-if:{key}",
            "key": key,
            "scope": engine_policies.OVERRIDE,
            "jurisdiction": "",
            "effective_from": period.start.isoformat(),
            "payload": payload,
            "title": engine_policies.COMPANY_KEYS.get(key, key),
            "recorded_at": "",
        })
    return out


def confirmed_changes(run: PayrollRun) -> list[ChangeRequest]:
    return list(
        ChangeRequest.objects.filter(company=run.company, period=run.period, status=ChangeRequest.CONFIRMED).order_by("id")
    )


def _final_runs(run: PayrollRun) -> list[PayrollRun]:
    return list(
        PayrollRun.objects.filter(
            company=run.company, run_type=PayrollRun.TYPE_REGULAR, status=PayrollRun.FINALIZED, period__lt=run.period
        ).order_by("period")
    )


def build_run_input(run: PayrollRun) -> dict:
    company = run.company
    period = run.period_obj
    data = run.snapshot.data
    all_versions = versions_for(company)
    # Versions that start after this month cannot apply to it; leaving them out means a rule
    # added for August does not disturb a July that is waiting for approval.
    versions = [v for v in all_versions if parse_date(v["effective_from"]) <= period.start]
    versions += override_versions(run, versions)
    arrears_policy = engine_policies.resolve_company(versions, "arrears_policy", period)
    lookback = int(arrears_policy.get("lookback_months", 12))

    changes = confirmed_changes(run)
    finals = _final_runs(run)
    final_emp = {r.period: {e["code"]: e for e in (r.result or {}).get("employees", [])} for r in finals}
    final_inputs = {r.period: {e["code"]: e for e in (r.input or {}).get("employees", [])} for r in finals}
    final_versions = {r.period: (r.input or {}).get("policy_versions", []) for r in finals}

    employees = []
    for emp in data["employees"]:
        if not _in_period(emp, period):
            continue
        code = emp["code"]
        comp_rows = [r for r in data["compensation"] if r["employee_code"] == code]
        compensation = _known_compensation(comp_rows, run.knowledge_date)
        unpaid = unpaid_days_for(data, code, period)
        one_time = []
        applied = []
        for ch in changes:
            for item in ch.proposal.get("items", []):
                if item.get("employee_code") != code:
                    continue
                kind = item["type"]
                if kind in ("one_time_earning", "one_time_deduction"):
                    one_time.append({
                        "ref": f"change:{ch.pk}",
                        "kind": "earning" if kind == "one_time_earning" else "deduction",
                        "code": item["item"],
                        "label": item["label"],
                        "amount": item["amount"],
                        "taxable": item.get("taxable", True),
                        "source_text": ch.text,
                    })
                elif kind == "unpaid_days":
                    unpaid = d(item["unpaid_days"])
                elif kind == "salary_revision":
                    compensation = [r for r in compensation if r.get("effective_from") != item["effective_from"]]
                    compensation.append({"effective_from": item["effective_from"], "components": item["components"]})
                applied.append(f"change:{ch.pk}")

        ytd_months = []
        arrears_basis = []
        for past in finals:
            past_period = past.period_obj
            paid = final_emp[past.period].get(code)
            if not paid:
                continue
            facts = paid["facts"]
            if past_period.same_fy(period):
                ytd_months.append({"period": past.period, "gross": facts["gross"], "taxable_gross": facts.get("taxable_gross", facts["gross"]),
                                   "tds": facts["tds"]})
            if past_period.months_until(period) > lookback:
                continue
            earned = {k: d(v) for k, v in facts["earned"].items()}
            pf_emp = d(facts["pf_employee_regular"])
            pf_er = d(facts["pf_employer_regular"])
            # Arrears for this month already paid in later months count as paid.
            for later in finals:
                if later.period <= past.period:
                    continue
                later_emp = final_emp[later.period].get(code)
                for a in (later_emp or {}).get("facts", {}).get("arrears_detail", []):
                    if a["period"] != past.period:
                        continue
                    for k, v in a["components"].items():
                        earned[k] = earned.get(k, d(0)) + d(v)
                    pf_emp += d(a["pf_employee"])
                    pf_er += d(a["pf_employer"])
            past_input = final_inputs[past.period].get(code, {})
            arrears_basis.append({
                "period": past.period,
                "policy_versions": final_versions[past.period],
                "unpaid_days": past_input.get("unpaid_days", "0"),
                "date_of_joining": past_input.get("date_of_joining", emp.get("date_of_joining")),
                "date_of_exit": past_input.get("date_of_exit", emp.get("date_of_exit")),
                "paid": {"earned": {k: to_str(v) for k, v in earned.items()}, "pf_employee": to_str(pf_emp), "pf_employer": to_str(pf_er)},
            })

        employees.append({
            "code": code,
            "name": emp.get("name", code),
            "work_state": emp.get("work_state", ""),
            "gender": emp.get("gender", ""),
            "date_of_joining": emp.get("date_of_joining"),
            "date_of_exit": emp.get("date_of_exit"),
            "pf_applicable": emp.get("pf_applicable", True),
            "esi_applicable": emp.get("esi_applicable", True),
            "compensation": sorted(compensation, key=lambda r: r.get("effective_from") or ""),
            "unpaid_days": to_str(unpaid),
            "one_time": one_time,
            "ytd": {"opening_gross": "0", "opening_tds": "0", "months": ytd_months},
            "arrears_basis": arrears_basis,
            "applied_changes": applied,
        })

    return {
        "period": str(period),
        "company": {"id": company.pk, "code": company.code, "name": company.name, "state": company.state},
        "policy_versions": versions,
        "employees": employees,
        "knowledge_date": run.knowledge_date.isoformat(),
        "snapshot": {"id": run.snapshot_id, "hash": run.snapshot.content_hash},
        "changes": [ch.pk for ch in changes],
        "history_months": [r.period for r in finals],
    }
