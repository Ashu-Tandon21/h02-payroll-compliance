"""TDS: estimate the year's pay, work out the year's tax, and spread what is left over the months left."""
from __future__ import annotations

from . import policies as pol
from .earnings import contracted_rate
from .explain import DEDUCTION, INFO, Line
from .money import ZERO, d, inr, round_amount
from .periods import Period, parse_date
from .tax import annual_tax


def compute_tds(emp, period, versions, gross, non_taxable, one_time_taxable):
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
