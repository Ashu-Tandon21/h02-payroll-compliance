"""Plain-English changes: the AI proposes, plain code checks, a person confirms, the engine calculates."""
from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone

from payroll_engine import policies as engine_policies
from payroll_engine.money import inr, to_str
from payroll_engine.periods import Period

from ..ai import tasks as ai_tasks
from ..ingestion.normalize import _split_gross
from ..ingestion.values import ValueError_, parse_amount, parse_date, parse_number
from ..models import ChangeRequest, PayrollRun
from .common import RuleViolation, record, require_role
from .policies import versions_for

ITEM_LABELS = {
    "bonus": "Bonus", "incentive": "Incentive", "commission": "Commission", "reimbursement": "Reimbursement",
    "overtime": "Overtime", "other_earning": "Other earning", "advance_recovery": "Advance recovery",
    "loan_recovery": "Loan recovery", "other_deduction": "Other deduction",
}
EARNING_ITEMS = {"bonus", "incentive", "commission", "reimbursement", "overtime", "other_earning"}


def _employees(run: PayrollRun) -> list[dict]:
    return [{"code": e["code"], "name": e.get("name", e["code"])} for e in run.snapshot.data["employees"]]


def resolve_employee(written: str, employees: list[dict]) -> tuple[dict | None, str]:
    w = re.sub(r"[’']s\b", "", (written or "").strip().lower())
    w = re.sub(r"\s+", " ", w)
    if not w:
        return None, "The instruction does not say which employee."
    for e in employees:
        if e["code"].lower() == w or e["name"].lower() == w:
            return e, ""
    tokens = [t for t in re.split(r"[^a-z0-9]+", w) if t]
    matches = [e for e in employees if tokens and all(t in re.split(r"[^a-z0-9]+", e["name"].lower()) for t in tokens)]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, f"'{written}' could be " + " or ".join(f"{m['name']} ({m['code']})" for m in matches) + ". Please use the full name or ID."
    return None, f"No employee called '{written}' in this month's data."


def _amount_values_in(text: str) -> set[str]:
    values = set()
    for written in ai_tasks.amounts_written(text):
        try:
            values.add(to_str(parse_amount(written)))
        except ValueError_:
            continue
    return values


def validate_proposal(raw: dict, text: str, run: PayrollRun) -> dict:
    """Check every proposed change with plain code. Problems block confirmation."""
    employees = _employees(run)
    period = run.period_obj
    items, problems = [], []
    written_amounts = _amount_values_in(text)
    numbers_in_text = {to_str(parse_number(n)) for n in re.findall(r"\b\d+(?:\.\d+)?\b", text)}
    for change in raw.get("changes", []):
        kind = change.get("type")
        emp, why = resolve_employee(change.get("employee_as_written", ""), employees)
        if not emp:
            problems.append(why)
            continue
        month = change.get("month")
        if month:
            try:
                target = Period.parse(month)
            except ValueError:
                problems.append(f"Could not read the month '{month}'.")
                continue
            if target != period:
                problems.append(f"This change is for {target.label}, but you are preparing {period.label}. Open {target.label} to add it.")
                continue
        item = {"type": kind, "employee_code": emp["code"], "employee_name": emp["name"]}
        if kind in ("one_time_earning", "one_time_deduction", "salary_revision"):
            written = change.get("amount_as_written")
            if not written:
                problems.append(f"No amount is written in the instruction for {emp['name']}. Amounts must be typed, never worked out by AI.")
                continue
            try:
                amount = parse_amount(written)
            except ValueError_:
                problems.append(f"Could not read the amount '{written}'.")
                continue
            if to_str(amount) not in written_amounts:
                problems.append(f"The amount {inr(amount)} does not appear in the instruction, so it was not used. Amounts must be typed, never worked out by AI.")
                continue
            if amount <= 0:
                problems.append("The amount must be more than zero.")
                continue
            item["amount"] = to_str(amount)
        if kind in ("one_time_earning", "one_time_deduction"):
            code = change.get("item") or ("other_earning" if kind == "one_time_earning" else "other_deduction")
            if (code in EARNING_ITEMS) != (kind == "one_time_earning"):
                code = "other_earning" if kind == "one_time_earning" else "other_deduction"
            item["item"] = code
            item["label"] = ITEM_LABELS.get(code, code)
            taxable = change.get("taxable")
            item["taxable"] = (code != "reimbursement") if taxable is None else bool(taxable)
            verb = "Pay" if kind == "one_time_earning" else "Deduct"
            item["summary"] = (
                f"{verb} a one-time {item['label'].lower()} of {inr(amount)} {'to' if verb == 'Pay' else 'from'} "
                f"{emp['name']} ({emp['code']}) in {period.label}."
                + (" It is taxable and is not part of PF wages." if kind == "one_time_earning" and item["taxable"] else "")
                + (" It is not taxable." if kind == "one_time_earning" and not item["taxable"] else "")
            )
        elif kind == "unpaid_days":
            written = change.get("unpaid_days_as_written")
            try:
                days = parse_number(written)
            except (ValueError_, TypeError):
                problems.append("The number of unpaid days is not written in the instruction.")
                continue
            if to_str(days) not in numbers_in_text:
                problems.append(f"{to_str(days)} days does not appear in the instruction, so it was not used.")
                continue
            if days < 0 or days > period.days:
                problems.append(f"{to_str(days)} unpaid days is not possible in {period.label}.")
                continue
            item["unpaid_days"] = to_str(days)
            item["summary"] = f"Set {emp['name']}'s ({emp['code']}) unpaid days in {period.label} to {to_str(days)} (replaces the figure from the HR data)."
        elif kind == "salary_revision":
            start = change.get("effective_from")
            try:
                start_date = parse_date(start) if start else None
            except ValueError_:
                start_date = None
            if not start_date:
                problems.append(f"The instruction does not say from which date {emp['name']}'s new salary starts.")
                continue
            if start_date.isoformat() not in text and not re.search(r"\b\d{1,2}(st|nd|rd|th)?\b", text):
                problems.append("The start date must be written in the instruction.")
                continue
            structure = engine_policies.resolve_company(versions_for(run.company), "salary_structure", Period.of(start_date))
            components = {k: to_str(v) for k, v in _split_gross(amount, structure.payload).items()}
            item.update({"effective_from": start_date.isoformat(), "components": components})
            parts = ", ".join(f"{k.replace('_', ' ')} {inr(v)}" for k, v in components.items())
            item["summary"] = (f"Change {emp['name']}'s ({emp['code']}) monthly salary to {inr(amount)} from {start_date.isoformat()} "
                               f"(split by the company salary structure: {parts}). Past months already paid get arrears.")
        else:
            problems.append(f"Unknown kind of change '{kind}'.")
            continue
        items.append(item)
    notes = []
    if not items and not problems:
        problems.append(raw.get("unclear") or "Could not find a change in this instruction.")
    elif raw.get("unclear"):
        # An AI remark next to a change that passed every check is shown to the maker, not a blocker.
        (notes if items else problems).append(raw["unclear"])
    return {"items": items, "problems": problems, "notes": notes}


def _check_month_open(run: PayrollRun):
    regular = PayrollRun.objects.filter(company=run.company, period=run.period, run_type=PayrollRun.TYPE_REGULAR).exclude(
        status=PayrollRun.CANCELLED).first()
    if regular and regular.is_locked:
        raise RuleViolation(f"{run.period_obj.label} is {regular.get_status_display().lower()}; changes are closed.")


@transaction.atomic
def propose(run: PayrollRun, text: str, user, force: bool = False) -> ChangeRequest:
    require_role(user, "maker", "enter changes")
    text = (text or "").strip()
    if not text:
        raise RuleViolation("Type an instruction first.")
    if len(text) > 1000:
        raise RuleViolation("Please keep an instruction under 1000 characters.")
    _check_month_open(run)
    interaction = ai_tasks.parse_instruction(company=run.company, text=text, period=run.period, employees=_employees(run), force=force)
    checked = validate_proposal(interaction.response or {}, text, run)
    checked["ai"] = {"provider": interaction.provider, "model": interaction.model, "interaction_id": interaction.pk,
                     "reused_saved_answer": getattr(interaction, "reused", False), "raw": interaction.response}
    change = ChangeRequest.objects.create(company=run.company, period=run.period, run=run, text=text, ai=interaction,
                                          proposal=checked, created_by=user)
    record(run, user, "change_proposed", f'Instruction entered: "{text}"', change_id=change.pk, problems=checked["problems"])
    return change


@transaction.atomic
def confirm(change: ChangeRequest, user) -> ChangeRequest:
    require_role(user, "maker", "confirm changes")
    change = ChangeRequest.objects.select_for_update().get(pk=change.pk)
    if change.status != ChangeRequest.PROPOSED:
        raise RuleViolation(f"This change is already {change.status}.")
    if change.proposal.get("problems"):
        raise RuleViolation("This change has problems and cannot be confirmed. Reword the instruction.")
    if not change.proposal.get("items"):
        raise RuleViolation("Nothing to confirm.")
    run = change.run
    _check_month_open(run)
    change.status = ChangeRequest.CONFIRMED
    change.decided_by = user
    change.decided_at = timezone.now()
    change.save()
    if change.ai_id:
        change.ai.status = "accepted"
        change.ai.reviewed_by = user
        change.ai.reviewed_at = timezone.now()
        change.ai.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    _invalidate_open_runs(run, user, "a change was confirmed")
    record(run, user, "change_confirmed", "Confirmed: " + " ".join(i["summary"] for i in change.proposal["items"]), change_id=change.pk)
    return change


@transaction.atomic
def reject(change: ChangeRequest, user) -> ChangeRequest:
    require_role(user, "maker", "discard changes")
    change = ChangeRequest.objects.select_for_update().get(pk=change.pk)
    if change.status == ChangeRequest.CONFIRMED:
        _check_month_open(change.run)
        _invalidate_open_runs(change.run, user, "a confirmed change was removed")
    change.status = ChangeRequest.REJECTED
    change.decided_by = user
    change.decided_at = timezone.now()
    change.save()
    if change.ai_id and change.ai.status == "pending_review":
        change.ai.status = "rejected"
        change.ai.reviewed_by = user
        change.ai.reviewed_at = timezone.now()
        change.ai.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    record(change.run, user, "change_discarded", f'Discarded: "{change.text}"', change_id=change.pk)
    return change


def _invalidate_open_runs(run: PayrollRun, user, why: str):
    for other in PayrollRun.objects.filter(company=run.company, period=run.period,
                                           status__in=[PayrollRun.CALCULATED, PayrollRun.REJECTED]):
        other.status = PayrollRun.DRAFT
        other.save(update_fields=["status", "updated_at"])
        record(other, user, "needs_recalculation", f"Needs to be calculated again because {why}.")

