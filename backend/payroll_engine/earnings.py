"""Salary for a month: the rate in force each day, unpaid days, and what is earned after the unpaid-leave cut."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from . import policies as pol
from .explain import RuleRef
from .money import ZERO, d, inr, round_amount, to_str
from .periods import Period, daterange, parse_date


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


def contracted_rate(compensation: list[dict], period: Period, employed_from: date | None = None) -> tuple[dict[str, Decimal], str, bool]:
    """Monthly salary by component for ``period``.

    If a revision starts mid-month, each part of the month is paid at its own rate,
    weighted by calendar days. Returns (rates, note, backfilled). ``backfilled`` means a day
    the person was employed (on or after ``employed_from``) has no salary record; days before
    joining are not paid, so a salary that starts on the joining date is not backfilled.
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
            if employed_from is None or day >= employed_from:
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
    return value, f"{to_str(value)} days ({lop_payload.get('source') or 'fixed by company setting'})"


def compute_earnings(emp: dict, period: Period, versions: list[dict], unpaid_days) -> Earnings:
    lop = pol.resolve_company(versions, "lop", period)
    rounding = pol.resolve_company(versions, "rounding", period)
    method = rounding.get("method", "nearest")
    mode = rounding.get("earnings", "per_component")

    rate, rate_note, backfilled = contracted_rate(emp.get("compensation") or [], period, parse_date(emp.get("date_of_joining")))
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
