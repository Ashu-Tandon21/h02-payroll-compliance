"""The payroll calculation.

``calculate_month(month_input) -> result`` is a pure function: no database, no network,
no clock, no AI. The same input always gives the same result, byte for byte, which is
what makes a month repeatable and explainable.

Month input (all amounts are strings so that JSON round-trips are exact)::

    {
      "company": {"id": 1, "name": "Brickline Design Co.", "state": "KA"},
      "period": "2025-07",
      "policy_versions": [ {id, key, scope, jurisdiction, effective_from, payload, title}, ... ],
      "employees": [{
        "code": "BL001", "name": "Priya Sharma", "work_state": "KA", "gender": "F",
        "date_of_joining": "2022-04-01", "date_of_exit": null,
        "pf_applicable": true, "esi_applicable": true,
        "compensation": [{"effective_from": "2025-04-01", "components": {"basic": "40000", ...}}],
        "unpaid_days": "3",
        "one_time": [{"ref": "change:4", "kind": "earning", "code": "bonus", "label": "Bonus",
                       "amount": "10000", "taxable": true}],
        "ytd": {"opening_gross": "0", "opening_tds": "0",
                "months": [{"period": "2025-04", "gross": "80000", "tds": "0"}]},
        "arrears_basis": [{"period": "2025-05", "policy_versions": [...], "unpaid_days": "0",
                            "date_of_joining": ..., "date_of_exit": ...,
                            "paid": {"earned": {"basic": "40000", ...},
                                     "pf_employee": "1800", "pf_employer": "1800"}}]
      }]
    }
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from . import policies as pol
from .explain import DEDUCTION, EARNING, EMPLOYER, INFO, TOTAL, Line, RuleRef
from .money import ZERO, d, inr, num, pct, round_amount, to_str
from .periods import Period, daterange, parse_date
from .states import state_code
from .tax import annual_tax

ENGINE_VERSION = "1.0.0"

COMPONENT_ORDER = ["basic", "da", "hra", "special_allowance", "conveyance", "medical", "lta", "other_allowance"]
COMPONENT_LABELS = {
    "basic": "Basic",
    "da": "Dearness allowance",
    "hra": "HRA",
    "special_allowance": "Special allowance",
    "conveyance": "Conveyance allowance",
    "medical": "Medical allowance",
    "lta": "Leave travel allowance",
    "other_allowance": "Other allowance",
}

WAGE_LABELS = {
    "gross_this_month": "gross pay this month",
    "gross_excluding_one_time": "gross pay without one-time items",
    "regular_earned": "regular pay earned this month (without arrears and one-time items)",
    "contracted_monthly": "full monthly salary (before unpaid leave)",
}


def component_label(code: str) -> str:
    return COMPONENT_LABELS.get(code) or code.replace("_", " ").capitalize()


def order_components(codes) -> list[str]:
    codes = set(codes)
    known = [c for c in COMPONENT_ORDER if c in codes]
    return known + sorted(codes - set(known))


class CalculationError(Exception):
    pass


# =========================================================================== salary for a month

@dataclass
class Earnings:
    period: Period
    rate: dict[str, Decimal]
    rate_note: str
    days_in_month: int
    lop_days: Decimal
    not_employed_days: int
    unpaid_days: Decimal
    divisor: Decimal
    divisor_text: str
    cut: dict[str, Decimal]
    earned: dict[str, Decimal]
    rounding_mode: str
    lop_ref: RuleRef
    rounding_ref: RuleRef
    total_cut: Decimal = ZERO
    regular_gross: Decimal = ZERO
    contracted_gross: Decimal = ZERO
    issues: list[dict] = field(default_factory=list)


def contracted_rate(compensation: list[dict], period: Period) -> tuple[dict[str, Decimal], str, bool]:
    """Monthly salary by component for ``period``.

    If a revision starts mid-month, each part of the month is paid at its own rate,
    weighted by calendar days. Returns (rates, note, backfilled).
    """
    if not compensation:
        return {}, "no salary on record", False
    revisions = sorted(
        ((parse_date(r.get("effective_from")) or date.min, r) for r in compensation),
        key=lambda pair: pair[0],
    )
    backfilled = False

    def active(day: date):
        nonlocal backfilled
        current = None
        for start, rev in revisions:
            if start <= day:
                current = (start, rev)
        if current is None:
            backfilled = True
            return revisions[0]
        return current

    segments: list[tuple[date, dict, int]] = []
    for day in daterange(period.start, period.end):
        start, rev = active(day)
        if segments and segments[-1][0] == start:
            segments[-1] = (start, rev, segments[-1][2] + 1)
        else:
            segments.append((start, rev, 1))

    if len(segments) == 1:
        rev = segments[0][1]
        rates = {k: d(v) for k, v in rev["components"].items()}
        start = segments[0][0]
        note = f"salary in force from {start.isoformat()}" if start != date.min else "salary on record"
        return rates, note, backfilled

    days = period.days
    rates: dict[str, Decimal] = {}
    parts = []
    for start, rev, count in segments:
        for k, v in rev["components"].items():
            rates[k] = rates.get(k, ZERO) + d(v) * count / days
        monthly = sum((d(v) for v in rev["components"].values()), ZERO)
        parts.append(f"{count} days at {inr(monthly)}/month")
    return rates, "salary changed during the month: " + ", ".join(parts), backfilled


def _divisor(lop_payload: dict, period: Period) -> tuple[Decimal, str]:
    divisor = lop_payload.get("divisor", "days_in_month")
    if divisor == "days_in_month":
        return Decimal(period.days), f"{period.days} days in {period.label.split()[0]}"
    value = d(divisor)
    return value, f"{to_str(value)} days (fixed by company setting)"


def compute_earnings(emp: dict, period: Period, versions: list[dict], unpaid_days) -> Earnings:
    lop = pol.resolve_company(versions, "lop", period)
    rounding = pol.resolve_company(versions, "rounding", period)
    method = rounding.get("method", "nearest")
    mode = rounding.get("earnings", "per_component")

    rate, rate_note, backfilled = contracted_rate(emp.get("compensation") or [], period)
    issues = []
    if not rate:
        issues.append({"level": "error", "message": "No salary found for this employee. Pay is zero until salary data is provided."})
    if backfilled:
        issues.append({"level": "warning", "message": "Salary record starts after the month began; the earliest salary was used for the earlier days."})

    days = period.days
    doj = parse_date(emp.get("date_of_joining"))
    doe = parse_date(emp.get("date_of_exit"))
    not_employed = 0
    for day in daterange(period.start, period.end):
        if (doj and day < doj) or (doe and day > doe):
            not_employed += 1

    lop_days = d(unpaid_days)
    if lop_days < 0:
        issues.append({"level": "error", "message": f"Unpaid days cannot be negative ({to_str(lop_days)}); treated as 0."})
        lop_days = ZERO
    max_lop = Decimal(days - not_employed)
    if lop_days > max_lop:
        issues.append({"level": "warning", "message": f"{to_str(lop_days)} unpaid days is more than the {to_str(max_lop)} days employed this month; capped."})
        lop_days = max_lop
    unpaid = lop_days + not_employed

    divisor, divisor_text = _divisor(lop.payload, period)
    day_rate_rule = lop.get("day_rate_rounding")

    cut_exact: dict[str, Decimal] = {}
    for comp, amount in rate.items():
        if unpaid == 0:
            cut_exact[comp] = ZERO
            continue
        if day_rate_rule:
            day_rate = round_amount(amount / divisor, day_rate_rule["method"], day_rate_rule["unit"])
            value = day_rate * unpaid
        else:
            value = amount * unpaid / divisor
        cut_exact[comp] = min(value, amount)

    cut: dict[str, Decimal] = {}
    earned: dict[str, Decimal] = {}
    if mode == "per_component":
        for comp, amount in rate.items():
            rate_r = round_amount(amount, method)
            cut[comp] = min(round_amount(cut_exact[comp], method), rate_r)
            earned[comp] = rate_r - cut[comp]
        total_cut = sum(cut.values(), ZERO)
        contracted = sum((round_amount(v, method) for v in rate.values()), ZERO)
        regular_gross = sum(earned.values(), ZERO)
    else:  # gross_only: components keep full precision, only the totals are rounded
        for comp, amount in rate.items():
            cut[comp] = cut_exact[comp]
            earned[comp] = amount - cut_exact[comp]
        contracted = round_amount(sum(rate.values(), ZERO), method)
        total_cut = round_amount(sum(cut_exact.values(), ZERO), method)
        regular_gross = contracted - total_cut

    return Earnings(
        period=period,
        rate=rate,
        rate_note=rate_note,
        days_in_month=days,
        lop_days=lop_days,
        not_employed_days=not_employed,
        unpaid_days=unpaid,
        divisor=divisor,
        divisor_text=divisor_text,
        cut=cut,
        earned=earned,
        rounding_mode=mode,
        lop_ref=lop.ref,
        rounding_ref=rounding.ref,
        total_cut=total_cut,
        regular_gross=regular_gross,
        contracted_gross=contracted,
        issues=issues,
    )


# =========================================================================== PF

@dataclass
class PFResult:
    applicable: bool
    pf_wage: Decimal
    wage_from_components: Decimal
    addback: Decimal
    employee_base: Decimal
    employer_base: Decimal
    employee: Decimal
    employer: Decimal
    eps: Decimal
    epf_employer: Decimal
    rule: pol.Resolved
    policy: pol.Resolved
    wage_code: pol.Resolved | None
    wage_code_text: str = ""


def compute_pf(emp: dict, earn: Earnings, versions: list[dict]) -> PFResult:
    period = earn.period
    rule = pol.resolve_statutory(versions, "pf", period, "IN")
    policy = pol.resolve_company(versions, "pf_policy", period)
    wage_components = rule.payload["wage_components"]
    wage = sum((earn.earned.get(c, ZERO) for c in wage_components), ZERO)
    from_components = wage

    addback = ZERO
    wage_code_text = ""
    wage_code = pol.resolve(versions, "wage_definition", period, "IN")
    if wage_code and wage_code.payload.get("enabled") and "pf" in wage_code.payload.get("applies_to", ["pf"]):
        total = sum(earn.earned.values(), ZERO)
        code_wages = sum((earn.earned.get(c, ZERO) for c in wage_code.payload["wage_components"]), ZERO)
        exclusions = total - code_wages
        share = d(wage_code.payload["max_exclusion_share"])
        limit = total * share
        if exclusions > limit:
            addback = exclusions - limit
            wage += addback
            wage_code_text = (
                f"Labour Codes: parts of pay that are not wages ({inr(round_amount(exclusions))}) are more than "
                f"{pct(share)} of total pay ({inr(round_amount(limit))}), so the extra {inr(round_amount(addback))} counts as wages for PF"
            )
        else:
            wage_code_text = (
                f"Labour Codes: parts of pay that are not wages ({inr(round_amount(exclusions))}) are within "
                f"{pct(share)} of total pay, so nothing is added"
            )

    ceiling = d(rule.payload["wage_ceiling"])
    emp_base = min(wage, ceiling) if policy.get("wage_base") == "capped" else wage
    er_mode = policy.get("employer_wage_base", "same")
    if er_mode == "same":
        er_base = emp_base
    elif er_mode == "capped":
        er_base = min(wage, ceiling)
    else:
        er_base = wage
    method = rule.payload.get("rounding", "nearest")
    applicable = emp.get("pf_applicable", True) is not False
    if not applicable:
        employee = employer = eps = ZERO
    else:
        employee = round_amount(emp_base * d(rule.payload["employee_rate"]), method)
        employer = round_amount(er_base * d(rule.payload["employer_rate"]), method)
        eps_rate = d(rule.payload.get("eps_rate", 0))
        eps_ceiling = d(rule.payload.get("eps_wage_ceiling", rule.payload["wage_ceiling"]))
        eps = min(round_amount(min(wage, eps_ceiling) * eps_rate, method), employer) if eps_rate else ZERO
    return PFResult(
        applicable=applicable,
        pf_wage=wage,
        wage_from_components=from_components,
        addback=addback,
        employee_base=emp_base,
        employer_base=er_base,
        employee=employee,
        employer=employer,
        eps=eps,
        epf_employer=employer - eps,
        rule=rule,
        policy=policy,
        wage_code=wage_code,
        wage_code_text=wage_code_text,
    )


# =========================================================================== arrears

@dataclass
class ArrearsMonth:
    period: Period
    components: dict[str, Decimal]
    total: Decimal
    pf_employee: Decimal
    pf_employer: Decimal
    explanation: str
    rate_before: Decimal
    rate_after: Decimal


def compute_arrears(emp: dict, period: Period, versions: list[dict]) -> tuple[list[ArrearsMonth], pol.Resolved, list[dict]]:
    """Recalculate each already-paid month with the salary as known now and pay the difference.

    Each past month is recalculated with *its own* rules and settings (stored with that
    month), so arrears never re-open a rule decision that was made at the time.
    """
    policy = pol.resolve_company(versions, "arrears_policy", period)
    pf_policy = pol.resolve_company(versions, "pf_policy", period)
    if not policy.get("enabled", True):
        return [], policy, []
    lookback = int(policy.get("lookback_months", 12))
    out: list[ArrearsMonth] = []
    warnings: list[dict] = []
    for basis in sorted(emp.get("arrears_basis") or [], key=lambda b: b["period"]):
        past = Period.parse(basis["period"])
        if not (past < period) or past.months_until(period) > lookback:
            continue
        past_emp = dict(emp)
        past_emp["date_of_joining"] = basis.get("date_of_joining", emp.get("date_of_joining"))
        past_emp["date_of_exit"] = basis.get("date_of_exit", emp.get("date_of_exit"))
        past_versions = basis["policy_versions"]
        _, _, backfilled = contracted_rate(emp.get("compensation") or [], past)
        if backfilled:
            # The salary for that month is not in the HR data, so it cannot be recalculated.
            # Paying "arrears" against a guessed salary would be wrong: skip and say so.
            warnings.append({"level": "warning", "message": f"No salary on record for {past.label}; arrears for that month were not "
                             "checked. If its salary changed, make sure the HR data includes the earlier salary."})
            continue
        earn = compute_earnings(past_emp, past, past_versions, basis.get("unpaid_days", "0"))
        paid = basis["paid"]
        paid_earned = {k: d(v) for k, v in (paid.get("earned") or {}).items()}
        deltas = {}
        for comp in order_components(set(earn.earned) | set(paid_earned)):
            delta = earn.earned.get(comp, ZERO) - paid_earned.get(comp, ZERO)
            if delta != 0:
                deltas[comp] = delta
        pf_emp = pf_er = ZERO
        if pf_policy.get("arrears_contribution", "recompute_delta") == "recompute_delta":
            pf = compute_pf(past_emp, earn, past_versions)
            pf_emp = pf.employee - d(paid.get("pf_employee"))
            pf_er = pf.employer - d(paid.get("pf_employer"))
        if not deltas and pf_emp == 0 and pf_er == 0:
            continue
        method = pol.resolve_company(versions, "rounding", period).get("method", "nearest")
        total = round_amount(sum(deltas.values(), ZERO), method)
        paid_gross = sum(paid_earned.values(), ZERO)
        new_gross = sum(earn.earned.values(), ZERO)
        explanation = (
            f"{past.label} recalculated with the salary as known now: earned {inr(round_amount(new_gross, method))} "
            f"instead of the {inr(round_amount(paid_gross, method))} already paid"
        )
        if earn.unpaid_days:
            explanation += f" (same {to_str(earn.unpaid_days)} unpaid days as before)"
        out.append(
            ArrearsMonth(
                period=past,
                components=deltas,
                total=total,
                pf_employee=pf_emp,
                pf_employer=pf_er,
                explanation=explanation,
                rate_before=round_amount(paid_gross, method),
                rate_after=round_amount(new_gross, method),
            )
        )
    return out, policy, warnings


# =========================================================================== professional tax

def select_pt_slab(slabs: list[dict], basis: Decimal, month: int, gender: str | None):
    best = None
    best_key = None
    for slab in slabs:
        if slab.get("gender") and slab["gender"] != (gender or ""):
            continue
        if slab.get("months") and month not in slab["months"]:
            continue
        if "min" in slab:
            lower = d(slab["min"])
            if basis < lower:
                continue
        else:
            lower = d(slab["above"])
            if basis <= lower:
                continue
        if slab.get("max") is not None and basis > d(slab["max"]):
            continue
        specificity = (2 if slab.get("months") else 0) + (1 if slab.get("gender") else 0)
        key = (specificity, lower)
        if best_key is None or key > best_key:
            best, best_key = slab, key
    return best


def _slab_text(slab: dict) -> str:
    if "min" in slab:
        text = f"{inr(d(slab['min']))} or more"
    else:
        text = f"more than {inr(d(slab['above']))}"
    if slab.get("max") is not None:
        text += f" up to {inr(d(slab['max']))}"
    if slab.get("gender"):
        text += {"F": " (women)", "M": " (men)", "O": " (others)"}[slab["gender"]]
    if slab.get("months"):
        text += " in month " + ", ".join(str(m) for m in slab["months"])
    return text


# =========================================================================== one employee

def _wage(choice: str, regular: Decimal, arrears: Decimal, one_time: Decimal, contracted: Decimal) -> Decimal:
    if choice == "gross_this_month":
        return regular + arrears + one_time
    if choice == "gross_excluding_one_time":
        return regular + arrears
    if choice == "regular_earned":
        return regular
    if choice == "contracted_monthly":
        return contracted
    raise CalculationError(f"unknown wage choice {choice!r}")


def calculate_employee(emp: dict, period: Period, versions: list[dict], company: dict) -> dict:
    lines: list[Line] = []
    issues: list[dict] = []
    amounts: dict[str, Decimal] = {}
    unpaid_input = emp.get("unpaid_days", "0")

    # ---------------------------------------------------------------- regular pay
    earn = compute_earnings(emp, period, versions, unpaid_input)
    issues += earn.issues
    method = pol.resolve_company(versions, "rounding", period).get("method", "nearest")
    comps = order_components(earn.rate)
    two_dp = earn.rounding_mode == "gross_only"

    def money_text(value):
        return inr(value, 2 if (two_dp and value != value.to_integral_value()) else None)

    for comp in comps:
        rate_amount = earn.rate[comp]
        amounts[f"rate.{comp}"] = round_amount(rate_amount, method) if not two_dp else rate_amount
    amounts["fixed_gross"] = earn.contracted_gross
    amounts["days_in_month"] = Decimal(earn.days_in_month)
    amounts["unpaid_days"] = earn.lop_days
    amounts["not_employed_days"] = Decimal(earn.not_employed_days)

    earning_children = []
    for comp in comps:
        earned = earn.earned[comp]
        amounts[f"earned.{comp}"] = earned
        rate_amount = earn.rate[comp]
        if earn.cut.get(comp):
            formula = (
                f"{money_text(rate_amount)} − ({money_text(rate_amount)} ÷ {to_str(earn.divisor)} × {to_str(earn.unpaid_days)} unpaid days) "
                f"= {money_text(earned)}"
            )
            reason = f"{component_label(comp)} for the month, less {to_str(earn.unpaid_days)} unpaid days"
        else:
            formula = f"{money_text(earned)} (full month)"
            reason = f"{component_label(comp)} for the month; {earn.rate_note}"
        earning_children.append(
            Line(
                code=f"earned.{comp}",
                label=component_label(comp),
                amount=earned,
                category=EARNING,
                formula=formula,
                reason=reason,
                rules=[earn.lop_ref, earn.rounding_ref] if earn.cut.get(comp) else [],
                inputs={"monthly_rate": rate_amount, "unpaid_days": earn.unpaid_days, "divisor": earn.divisor},
            )
        )
    lines.extend(earning_children)

    lop_children = []
    if earn.unpaid_days:
        for comp in comps:
            if earn.cut.get(comp):
                lop_children.append(
                    Line(
                        code=f"lop.{comp}",
                        label=f"{component_label(comp)} cut",
                        amount=earn.cut[comp],
                        category=INFO,
                        formula=f"{money_text(earn.rate[comp])} ÷ {to_str(earn.divisor)} × {to_str(earn.unpaid_days)} = {money_text(earn.cut[comp])}",
                    )
                )
        parts = []
        if earn.lop_days:
            parts.append(f"{to_str(earn.lop_days)} unpaid leave days")
        if earn.not_employed_days:
            parts.append(f"{earn.not_employed_days} days before joining or after leaving")
        lines.append(
            Line(
                code="lop_deduction",
                label="Unpaid leave cut",
                amount=earn.total_cut,
                category=INFO,
                formula=(
                    f"{inr(earn.contracted_gross)} ÷ {earn.divisor_text.split(' (')[0]} × {to_str(earn.unpaid_days)} days = {inr(earn.total_cut)}"
                ),
                reason=(
                    f"One day of pay is the monthly pay divided by {earn.divisor_text}. "
                    f"Cut for {' and '.join(parts)}. "
                    + ("Each part of pay is cut and rounded separately." if earn.rounding_mode == "per_component" else "Only the total cut is rounded.")
                ),
                rules=[earn.lop_ref, earn.rounding_ref],
                inputs={"unpaid_days": earn.unpaid_days, "divisor": earn.divisor, "monthly_pay": earn.contracted_gross},
                children=lop_children,
            )
        )
    amounts["lop_deduction"] = earn.total_cut
    regular_gross = earn.regular_gross

    # ---------------------------------------------------------------- arrears
    arrears_months, arrears_policy, arrears_warnings = compute_arrears(emp, period, versions)
    issues += arrears_warnings
    arrears_total = sum((a.total for a in arrears_months), ZERO)
    arrears_pf_emp = sum((a.pf_employee for a in arrears_months), ZERO)
    arrears_pf_er = sum((a.pf_employer for a in arrears_months), ZERO)
    if arrears_months:
        children = []
        for a in arrears_months:
            comp_text = ", ".join(f"{component_label(c)} {money_text(v)}" for c, v in a.components.items())
            children.append(
                Line(
                    code=f"arrears.{a.period}",
                    label=f"Arrears for {a.period.label}",
                    amount=a.total,
                    category=INFO,
                    formula=f"{inr(a.rate_after)} − {inr(a.rate_before)} already paid = {inr(a.total)}",
                    reason=a.explanation + (f" ({comp_text})" if comp_text else ""),
                    inputs={"components": a.components, "pf_employee": a.pf_employee, "pf_employer": a.pf_employer},
                )
            )
        months_txt = ", ".join(a.period.label.split()[0] for a in arrears_months)
        lines.append(
            Line(
                code="arrears",
                label="Arrears (back pay)",
                amount=arrears_total,
                category=EARNING,
                formula=" + ".join(inr(a.total) for a in arrears_months) + f" = {inr(arrears_total)}",
                reason=f"Salary for {months_txt} was revised after it was paid. Each month is worked out again with the revised salary and the difference is paid now.",
                rules=[arrears_policy.ref],
                children=children,
            )
        )
    amounts["arrears"] = arrears_total

    # ---------------------------------------------------------------- one-time items
    one_time_earnings = ZERO
    one_time_taxable = ZERO
    other_deductions = ZERO
    deduction_lines = []
    for item in sorted(emp.get("one_time") or [], key=lambda i: (i.get("kind", ""), i.get("code", ""), i.get("ref", ""))):
        amount = d(item["amount"])
        code = item.get("code") or "other"
        label = item.get("label") or component_label(code)
        source = item.get("source_text")
        reason = "One-time item confirmed by the payroll maker"
        if source:
            reason += f' from the instruction "{source}"'
        if item.get("kind") == "deduction":
            other_deductions += amount
            amounts[f"deduction.{code}"] = amounts.get(f"deduction.{code}", ZERO) + amount
            deduction_lines.append(
                Line(code=f"deduction.{code}", label=label, amount=amount, category=DEDUCTION, formula=inr(amount), reason=reason,
                     inputs={"ref": item.get("ref", "")})
            )
        else:
            one_time_earnings += amount
            if item.get("taxable", True):
                one_time_taxable += amount
            amounts[f"one_time.{code}"] = amounts.get(f"one_time.{code}", ZERO) + amount
            lines.append(
                Line(code=f"one_time.{code}", label=label, amount=amount, category=EARNING, formula=inr(amount),
                     reason=reason + ("" if item.get("taxable", True) else " (not taxable)"), inputs={"ref": item.get("ref", "")})
            )

    gross = regular_gross + arrears_total + one_time_earnings
    gross_parts = [f"{inr(regular_gross)} regular pay"]
    if arrears_total:
        gross_parts.append(f"{inr(arrears_total)} arrears")
    if one_time_earnings:
        gross_parts.append(f"{inr(one_time_earnings)} one-time")
    lines.append(
        Line(
            code="gross",
            label="Gross pay",
            amount=gross,
            category=TOTAL,
            formula=" + ".join(gross_parts) + f" = {inr(gross)}" if len(gross_parts) > 1 else f"{inr(gross)}",
            reason="Everything earned this month before deductions",
        )
    )
    amounts["regular_gross"] = regular_gross
    amounts["gross"] = gross

    # ---------------------------------------------------------------- PF
    pf = compute_pf(emp, earn, versions)
    pf_rate = d(pf.rule.payload["employee_rate"])
    ceiling = d(pf.rule.payload["wage_ceiling"])
    if not pf.applicable:
        pf_formula = "Not applicable"
        pf_reason = "This employee is not a PF member (marked in HR data)."
    elif pf.policy.get("wage_base") == "capped":
        capped = pf.pf_wage > ceiling
        pf_formula = (
            f"{pct(pf_rate)} of {inr(pf.employee_base, 2 if pf.employee_base != pf.employee_base.to_integral_value() else None)}"
            + (f" ({inr(ceiling)} cap)" if capped else "")
            + f" = {inr(pf.employee)}"
        )
        pf_reason = (
            f"PF is worked out on the first {inr(ceiling)} of PF wages (company setting). "
            + (f"PF wages this month are {money_text(pf.pf_wage)}, above the cap, so the cap is used." if capped
               else f"PF wages this month are {money_text(pf.pf_wage)}, below the cap, so all of it is used.")
        )
    else:
        pf_formula = f"{pct(pf_rate)} of {money_text(pf.employee_base)} = {inr(pf.employee)}"
        pf_reason = f"PF is worked out on all of the PF wages (company setting): {money_text(pf.pf_wage)}."
    if pf.wage_code_text:
        pf_reason += " " + pf.wage_code_text + "."
    pf_children = []
    wage_parts = " + ".join(
        f"{component_label(c)} {money_text(earn.earned.get(c, ZERO))}" for c in pf.rule.payload["wage_components"] if c in earn.earned
    ) or "no PF wage components"
    pf_children.append(
        Line(code="pf_wage", label="PF wages", amount=pf.pf_wage, category=INFO,
             formula=wage_parts + (f" + Labour Codes add-back {money_text(pf.addback)}" if pf.addback else "") + f" = {money_text(pf.pf_wage)}",
             reason="PF wages are the parts of pay listed in the PF rule, after unpaid leave",
             rules=[pf.rule.ref] + ([pf.wage_code.ref] if pf.wage_code else []))
    )
    pf_employee_total = pf.employee + (arrears_pf_emp if pf.applicable else ZERO)
    if arrears_pf_emp and pf.applicable:
        pf_children.append(
            Line(code="pf_employee.current", label="PF on this month's pay", amount=pf.employee, category=INFO, formula=pf_formula)
        )
        pf_children.append(
            Line(code="pf_employee.arrears", label="PF on arrears", amount=arrears_pf_emp, category=INFO,
                 formula=" + ".join(f"{a.period.short_label}: {inr(a.pf_employee)}" for a in arrears_months if a.pf_employee) + f" = {inr(arrears_pf_emp)}",
                 reason="PF for the revised months worked out again with the rules of those months, minus PF already paid")
        )
        pf_formula = f"{inr(pf.employee)} this month + {inr(arrears_pf_emp)} on arrears = {inr(pf_employee_total)}"
    elif arrears_months and pf.applicable:
        pf_reason += " The arrears add no PF: PF for the revised months comes out the same as what was already paid."
    lines.append(
        Line(code="pf_employee", label="PF (employee 12%)", amount=pf_employee_total, category=DEDUCTION,
             formula=pf_formula, reason=pf_reason, rules=[pf.rule.ref, pf.policy.ref] + ([pf.wage_code.ref] if pf.wage_code and pf.addback else []),
             inputs={"pf_wage": pf.pf_wage, "base": pf.employee_base, "rate": pf_rate, "ceiling": ceiling},
             children=pf_children)
    )
    amounts["pf_wage"] = pf.pf_wage
    amounts["pf_employee"] = pf_employee_total

    # ---------------------------------------------------------------- ESI
    esi_rule = pol.resolve_statutory(versions, "esi", period, "IN")
    esi_policy = pol.resolve_company(versions, "esi_policy", period)
    threshold = d(esi_rule.payload["threshold"])
    elig_choice = esi_policy.get("eligibility_wage")
    contrib_choice = esi_policy.get("contribution_wage")
    elig_wage = _wage(elig_choice, regular_gross, arrears_total, one_time_earnings, earn.contracted_gross)
    esi_wage = _wage(contrib_choice, regular_gross, arrears_total, one_time_earnings, earn.contracted_gross)
    esi_method = esi_rule.payload.get("rounding", "up")
    esi_applicable = emp.get("esi_applicable", True) is not False
    eligible = esi_applicable and elig_wage <= threshold and esi_wage > 0
    if eligible:
        esi_emp = round_amount(esi_wage * d(esi_rule.payload["employee_rate"]), esi_method)
        esi_er = round_amount(esi_wage * d(esi_rule.payload["employer_rate"]), esi_method)
        esi_formula = f"{pct(d(esi_rule.payload['employee_rate']))} of {inr(esi_wage)} = {num(esi_wage * d(esi_rule.payload['employee_rate']))}, rounded {'up' if esi_method == 'up' else esi_method} = {inr(esi_emp)}"
        esi_reason = f"ESI applies because {WAGE_LABELS[elig_choice]} ({inr(elig_wage)}) is {inr(threshold)} or less."
    else:
        esi_emp = esi_er = ZERO
        esi_formula = "Not applicable"
        if not esi_applicable:
            esi_reason = "This employee is not covered by ESI (marked in HR data)."
        else:
            esi_reason = f"No ESI: {WAGE_LABELS[elig_choice]} ({inr(elig_wage)}) is above {inr(threshold)}."
    lines.append(
        Line(code="esi_employee", label="ESI (employee 0.75%)", amount=esi_emp, category=DEDUCTION, formula=esi_formula,
             reason=esi_reason, rules=[esi_rule.ref, esi_policy.ref],
             inputs={"eligibility_wage": elig_wage, "esi_wage": esi_wage, "threshold": threshold})
    )
    amounts["esi_wage"] = esi_wage if eligible else ZERO
    amounts["esi_employee"] = esi_emp

    # ---------------------------------------------------------------- professional tax
    state = state_code(emp.get("work_state")) or state_code(company.get("state"))
    pt_policy = pol.resolve_company(versions, "pt_policy", period)
    pt_basis_choice = pt_policy.get("basis")
    pt_basis = _wage(pt_basis_choice, regular_gross, arrears_total, one_time_earnings, earn.contracted_gross)
    pt_rule = pol.resolve(versions, "professional_tax", period, f"IN-{state}") if state else None
    pt_rules = [pt_policy.ref]
    if pt_rule is None:
        pt = ZERO
        pt_formula = "Not applicable"
        pt_reason = f"No professional tax rule is stored for {state or 'this employee’s state'} on {period.start.isoformat()}."
        if state:
            issues.append({"level": "info", "message": f"No professional tax rule for state {state}; PT is 0."})
    else:
        pt_rules.insert(0, pt_rule.ref)
        slab = select_pt_slab(pt_rule.payload["slabs"], pt_basis, period.month, emp.get("gender"))
        if slab is None:
            pt = ZERO
            pt_formula = "No slab matches"
            pt_reason = f"No slab of the {state} rule matches {inr(pt_basis)}"
            if any(s.get("gender") for s in pt_rule.payload["slabs"]) and not emp.get("gender"):
                issues.append({"level": "warning", "message": "Professional tax depends on gender in this state, but gender is missing."})
        else:
            if "rate" in slab:
                pt = round_amount(pt_basis * d(slab["rate"]), "nearest")
                pt_formula = f"{pct(d(slab['rate']))} of {inr(pt_basis)} = {inr(pt)}"
            else:
                pt = d(slab["amount"])
                pt_formula = f"{inr(pt)} (slab: {_slab_text(slab)})"
            pt_reason = f"{state} professional tax on {WAGE_LABELS[pt_basis_choice]} of {inr(pt_basis)}: the slab for {_slab_text(slab)} applies."
    lines.append(
        Line(code="pt", label="Professional tax", amount=pt, category=DEDUCTION, formula=pt_formula, reason=pt_reason, rules=pt_rules,
             inputs={"basis": pt_basis, "state": state})
    )
    amounts["pt_basis"] = pt_basis
    amounts["pt"] = pt

    # ---------------------------------------------------------------- TDS
    tds_line, tds_amounts = _tds(emp, period, versions, gross, one_time_earnings - one_time_taxable, one_time_taxable)
    lines.append(tds_line)
    amounts.update(tds_amounts)
    tds = tds_amounts["tds"]

    lines.extend(deduction_lines)

    total_deductions = pf_employee_total + esi_emp + pt + tds + other_deductions
    lines.append(
        Line(code="total_deductions", label="Total deductions", amount=total_deductions, category=TOTAL,
             formula=" + ".join(inr(x) for x in [pf_employee_total, esi_emp, pt, tds] + ([other_deductions] if other_deductions else []))
             + f" = {inr(total_deductions)}",
             reason="PF + ESI + professional tax + TDS" + (" + other deductions" if other_deductions else ""))
    )
    net = gross - total_deductions
    lines.append(
        Line(code="net_pay", label="Net pay", amount=net, category=TOTAL,
             formula=f"{inr(gross)} − {inr(total_deductions)} = {inr(net)}",
             reason="Gross pay minus deductions: what reaches the employee's bank account")
    )
    amounts["total_deductions"] = total_deductions
    amounts["net_pay"] = net
    if net < 0:
        issues.append({"level": "error", "message": f"Net pay is negative ({inr(net)}). Check deductions before approving."})

    # ---------------------------------------------------------------- employer side
    pf_er_total = pf.employer + (arrears_pf_er if pf.applicable else ZERO)
    eps_rate = d(pf.rule.payload.get("eps_rate", 0))
    lines.append(
        Line(code="pf_employer", label="PF (company 12%)", amount=pf_er_total, category=EMPLOYER,
             formula=f"{pct(d(pf.rule.payload['employer_rate']))} of {money_text(pf.employer_base)} = {inr(pf.employer)}"
             + (f" + {inr(arrears_pf_er)} on arrears = {inr(pf_er_total)}" if arrears_pf_er and pf.applicable else ""),
             reason=f"Paid by the company on top of salary. Of this, {inr(pf.eps)} goes to the pension scheme "
             f"({pct(eps_rate)} of PF wages up to {inr(d(pf.rule.payload.get('eps_wage_ceiling', ceiling)))}) and the rest to the PF account.",
             rules=[pf.rule.ref, pf.policy.ref],
             children=[
                 Line(code="eps_employer", label="Pension (EPS)", amount=pf.eps, category=INFO),
                 Line(code="epf_employer", label="PF account (EPF)", amount=pf_er_total - pf.eps, category=INFO),
             ])
    )
    amounts["pf_employer"] = pf_er_total
    amounts["eps_employer"] = pf.eps
    amounts["epf_employer"] = pf_er_total - pf.eps
    lines.append(
        Line(code="esi_employer", label="ESI (company 3.25%)", amount=esi_er, category=EMPLOYER,
             formula=(f"{pct(d(esi_rule.payload['employer_rate']))} of {inr(esi_wage)} = {num(esi_wage * d(esi_rule.payload['employer_rate']))}, rounded up = {inr(esi_er)}"
                      if eligible else "Not applicable"),
             reason="Paid by the company on top of salary" if eligible else esi_reason, rules=[esi_rule.ref, esi_policy.ref])
    )
    amounts["esi_employer"] = esi_er
    employer_cost = gross + pf_er_total + esi_er
    lines.append(
        Line(code="employer_cost", label="Cost to company this month", amount=employer_cost, category=TOTAL,
             formula=f"{inr(gross)} + {inr(pf_er_total)} + {inr(esi_er)} = {inr(employer_cost)}",
             reason="Gross pay plus the company's own PF and ESI")
    )
    amounts["employer_cost"] = employer_cost

    facts = {
        "earned": {c: to_str(v) for c, v in earn.earned.items()},
        "pf_employee_regular": to_str(pf.employee),
        "pf_employer_regular": to_str(pf.employer),
        "arrears_detail": [
            {"period": str(a.period), "components": {k: to_str(v) for k, v in a.components.items()}, "total": to_str(a.total),
             "pf_employee": to_str(a.pf_employee if pf.applicable else ZERO), "pf_employer": to_str(a.pf_employer if pf.applicable else ZERO)}
            for a in arrears_months
        ],
        "gross": to_str(gross),
        "taxable_gross": to_str(gross - (one_time_earnings - one_time_taxable)),
        "tds": to_str(tds),
        "unpaid_days": to_str(earn.lop_days),
        "contracted_gross": to_str(earn.contracted_gross),
    }

    return {
        "code": emp["code"],
        "name": emp.get("name", ""),
        "work_state": state,
        "lines": [line.to_dict() for line in lines],
        "amounts": {k: to_str(v) for k, v in amounts.items()},
        "facts": facts,
        "issues": issues,
    }


def _tds(emp, period, versions, gross, non_taxable, one_time_taxable):
    rule = pol.resolve_statutory(versions, "income_tax", period, "IN")
    policy = pol.resolve_company(versions, "tds_policy", period)
    unit = policy.get("annual_tax_rounding_unit", 1)
    monthly_method = policy.get("monthly_rounding", "nearest")

    ytd = emp.get("ytd") or {}
    months = [m for m in ytd.get("months", []) if Period.parse(m["period"]).same_fy(period) and Period.parse(m["period"]) < period]
    paid_gross = d(ytd.get("opening_gross")) + sum((d(m.get("taxable_gross", m["gross"])) for m in months), ZERO)
    tds_paid = d(ytd.get("opening_tds")) + sum((d(m["tds"]) for m in months), ZERO)

    doe = parse_date(emp.get("date_of_exit"))
    last = period.fy_last
    if doe and Period.of(doe) < last:
        last = max(Period.of(doe), period)
    future_total = ZERO
    future_desc = []
    p = period.next()
    while p <= last:
        rate, _, _ = contracted_rate(emp.get("compensation") or [], p)
        monthly = sum(rate.values(), ZERO)
        future_total += monthly
        future_desc.append((p, monthly))
        p = p.next()
    future_total = round_amount(future_total, "nearest")
    months_left = period.months_until(last) + 1

    current_taxable = gross - non_taxable
    projected = paid_gross + current_taxable + future_total
    std = d(rule.payload["standard_deduction"])
    taxable = max(ZERO, projected - std)
    trace = annual_tax(taxable, rule.payload, unit)

    if policy.get("one_time_income") == "tax_in_month" and one_time_taxable:
        base_trace = annual_tax(max(ZERO, projected - one_time_taxable - std), rule.payload, unit)
        regular = max(ZERO, round_amount((base_trace.total - tds_paid) / months_left, monthly_method))
        extra = trace.total - base_trace.total
        tds = regular + extra
        formula = (f"({inr(base_trace.total)} tax without the one-time pay − {inr(tds_paid)} already deducted) ÷ {months_left} months "
                   f"= {inr(regular)}, plus {inr(extra)} tax on the one-time pay = {inr(tds)}")
    else:
        remaining = trace.total - tds_paid
        tds = max(ZERO, round_amount(remaining / months_left, monthly_method))
        formula = f"({inr(trace.total)} − {inr(tds_paid)} already deducted) ÷ {months_left} months left = {inr(tds)}"
        if trace.total == 0 and tds_paid == 0:
            formula = "Tax for the year is ₹0, so ₹0 this month"
        elif remaining < 0:
            formula = f"Tax already deducted ({inr(tds_paid)}) covers the year's tax ({inr(trace.total)}), so ₹0"

    future_text = ""
    if future_desc:
        distinct = sorted({m for _, m in future_desc})
        if len(distinct) == 1:
            future_text = f"{len(future_desc)} more months at {inr(round_amount(distinct[0]))} = {inr(future_total)}"
        else:
            future_text = f"{len(future_desc)} more months at the salary in force for each = {inr(future_total)}"
    projection_parts = [f"{inr(paid_gross)} paid so far this year", f"{inr(current_taxable)} this month"]
    if future_text:
        projection_parts.append(future_text)
    children = [
        Line(code="tds.projection", label="Estimated pay for the year", amount=projected, category=INFO,
             formula=" + ".join(projection_parts) + f" = {inr(projected)}",
             reason=f"{period.fy_label}: pay already paid, plus this month, plus the current salary for the months left"),
        Line(code="tds.taxable", label="Taxable income", amount=taxable, category=INFO,
             formula=f"{inr(projected)} − {inr(std)} standard deduction = {inr(taxable)}"),
        Line(code="tds.annual_tax", label="Tax for the year", amount=trace.total, category=INFO,
             formula="; ".join(trace.lines()), reason=f"{rule.payload.get('regime', 'new').capitalize()} tax regime slabs from the stored rule"),
        Line(code="tds.paid", label="Tax already deducted this year", amount=tds_paid, category=INFO),
    ]
    if trace.total == 0:
        reason = f"No TDS: estimated taxable income for the year is {inr(taxable)}, and the tax on it is ₹0 under the {rule.payload.get('regime', 'new')} regime."
    else:
        reason = "Estimate the year's pay, work out the year's tax, subtract the tax already taken, and spread the rest over the months left."
    line = Line(code="tds", label="Income tax (TDS)", amount=tds, category=DEDUCTION, formula=formula, reason=reason,
                rules=[rule.ref, policy.ref], children=children,
                inputs={"projected_income": projected, "taxable_income": taxable, "annual_tax": trace.total,
                        "tds_paid": tds_paid, "months_left": months_left})
    return line, {
        "tds": tds,
        "tds_projected_income": projected,
        "tds_taxable_income": taxable,
        "tds_annual_tax": trace.total,
        "tds_paid_before": tds_paid,
    }


# =========================================================================== month

TOTAL_KEYS = [
    "fixed_gross", "lop_deduction", "arrears", "regular_gross", "gross", "pf_employee", "esi_employee", "pt", "tds",
    "total_deductions", "net_pay", "pf_employer", "eps_employer", "epf_employer", "esi_employer", "employer_cost",
]


def calculate_month(month_input: dict) -> dict:
    period = Period.parse(month_input["period"])
    versions = month_input.get("policy_versions") or []
    company = month_input.get("company") or {}
    employees = sorted(month_input.get("employees") or [], key=lambda e: e["code"])
    seen = set()
    results = []
    for emp in employees:
        if emp["code"] in seen:
            raise CalculationError(f"employee code {emp['code']} appears twice")
        seen.add(emp["code"])
        try:
            results.append(calculate_employee(emp, period, versions, company))
        except pol.PolicyMissing as exc:
            raise CalculationError(str(exc)) from exc

    totals: dict[str, Decimal] = {}
    for r in results:
        for key, value in r["amounts"].items():
            if key in TOTAL_KEYS or key.startswith(("one_time.", "deduction.")):
                totals[key] = totals.get(key, ZERO) + d(value)
    used: dict[str, dict] = {}
    for r in results:
        for line in _walk(r["lines"]):
            for ref in line["rules"]:
                used[f"{ref['key']}|{ref['jurisdiction']}|{ref['version_id']}"] = ref
    return {
        "engine_version": ENGINE_VERSION,
        "period": str(period),
        "company": company,
        "employee_count": len(results),
        "employees": results,
        "totals": {k: to_str(v) for k, v in sorted(totals.items())},
        "rules_used": sorted(used.values(), key=lambda r: (r["key"], r["jurisdiction"], r["effective_from"])),
        "issues": [{"employee": r["code"], **i} for r in results for i in r["issues"]],
    }


def _walk(lines):
    for line in lines:
        yield line
        yield from _walk(line.get("children", []))
