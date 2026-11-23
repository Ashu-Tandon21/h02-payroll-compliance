"""Arrears: months already paid, worked out again with what is known now, each with its own rules."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from . import policies as pol
from .components import order_components
from .earnings import compute_earnings, contracted_rate
from .money import ZERO, d, inr, round_amount, to_str
from .periods import Period, parse_date
from .pf import compute_pf


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
        _, _, backfilled = contracted_rate(emp.get("compensation") or [], past, parse_date(past_emp.get("date_of_joining")))
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
        late = d(basis.get("late_unpaid_days", "0"))
        if late:
            change = (f"{to_str(late)} more unpaid day{'s' if late != 1 else ''} reported after it was paid" if late > 0 else
                      f"{to_str(-late)} unpaid day{'s' if late != -1 else ''} fewer: counted by mistake when it was paid")
            explanation = (
                f"{past.label} worked out again with {change} ({to_str(earn.unpaid_days)} in all): earned "
                f"{inr(round_amount(new_gross, method))} instead of the {inr(round_amount(paid_gross, method))} already paid"
            )
        elif earn.unpaid_days:
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
