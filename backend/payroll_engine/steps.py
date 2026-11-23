"""One employee's month, as a list of steps.

Each step reads what the earlier steps worked out (``EmployeeCalc``) and adds its own lines (the amount with its
formula, reason and rules), amounts and issues. ``STEPS`` gives the order, which is also the order of the lines on
a payslip. A new statutory deduction or company contribution is a new step: it registers itself with
``add_deduction`` or ``add_employer_contribution`` and the totals include it, without changing the other steps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from . import policies as pol
from .arrears import ArrearsMonth, compute_arrears
from .components import WAGE_LABELS, component_label, order_components, wage_for
from .earnings import Earnings, compute_earnings
from .explain import DEDUCTION, EARNING, EMPLOYER, INFO, TOTAL, Line
from .money import ZERO, d, inr, num, pct, round_amount, to_str
from .periods import Period
from .pf import PFResult, compute_pf
from .professional_tax import _slab_text, select_pt_slab
from .states import state_code
from .tds import compute_tds


@dataclass
class EmployeeCalc:
    emp: dict
    period: Period
    versions: list[dict]
    company: dict
    lines: list[Line] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    amounts: dict[str, Decimal] = field(default_factory=dict)
    # worked out by the steps, in order
    earn: Earnings | None = None
    arrears_months: list[ArrearsMonth] = field(default_factory=list)
    arrears_total: Decimal = ZERO
    arrears_pf_employee: Decimal = ZERO
    arrears_pf_employer: Decimal = ZERO
    one_time_earnings: Decimal = ZERO
    one_time_taxable: Decimal = ZERO
    other_deductions: Decimal = ZERO
    deduction_lines: list[Line] = field(default_factory=list)
    gross: Decimal = ZERO
    pf: PFResult | None = None
    esi: dict = field(default_factory=dict)
    state: str = ""
    tds: Decimal = ZERO
    # what the totals add up: (name in the reason, amount), in payslip order
    deductions: list[tuple[str, Decimal]] = field(default_factory=list)
    employer_contributions: list[tuple[str, Decimal]] = field(default_factory=list)

    def add_deduction(self, name: str, amount: Decimal):
        self.deductions.append((name, amount))

    def add_employer_contribution(self, name: str, amount: Decimal):
        self.employer_contributions.append((name, amount))

    def money_text(self, value: Decimal) -> str:
        """Whole rupees, or paise when parts of pay are not rounded (rounding setting "gross only")."""
        two_dp = self.earn.rounding_mode == "gross_only"
        return inr(value, 2 if (two_dp and value != value.to_integral_value()) else None)

    def wage(self, choice: str) -> Decimal:
        return wage_for(choice, self.earn.regular_gross, self.arrears_total, self.one_time_earnings, self.earn.contracted_gross)


# --------------------------------------------------------------------------- regular pay

def regular_pay(c: EmployeeCalc):
    earn = compute_earnings(c.emp, c.period, c.versions, c.emp.get("unpaid_days", "0"))
    c.earn = earn
    c.issues += earn.issues
    method = pol.resolve_company(c.versions, "rounding", c.period).get("method", "nearest")
    comps = order_components(earn.rate)
    two_dp = earn.rounding_mode == "gross_only"
    money_text = c.money_text

    for comp in comps:
        rate_amount = earn.rate[comp]
        c.amounts[f"rate.{comp}"] = round_amount(rate_amount, method) if not two_dp else rate_amount
    c.amounts["fixed_gross"] = earn.contracted_gross
    c.amounts["days_in_month"] = Decimal(earn.days_in_month)
    c.amounts["unpaid_days"] = earn.lop_days
    c.amounts["not_employed_days"] = Decimal(earn.not_employed_days)

    for comp in comps:
        earned = earn.earned[comp]
        c.amounts[f"earned.{comp}"] = earned
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
        c.lines.append(
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

    if earn.unpaid_days:
        lop_children = []
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
        c.lines.append(
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
    c.amounts["lop_deduction"] = earn.total_cut


# --------------------------------------------------------------------------- arrears

def arrears(c: EmployeeCalc):
    months, policy, warnings = compute_arrears(c.emp, c.period, c.versions)
    c.issues += warnings
    c.arrears_months = months
    c.arrears_total = total = sum((a.total for a in months), ZERO)
    c.arrears_pf_employee = sum((a.pf_employee for a in months), ZERO)
    c.arrears_pf_employer = sum((a.pf_employer for a in months), ZERO)
    if months:
        children = []
        for a in months:
            comp_text = ", ".join(f"{component_label(k)} {c.money_text(v)}" for k, v in a.components.items())
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
        months_txt = ", ".join(a.period.label.split()[0] for a in months)
        late_days = any("reported after it was paid" in a.explanation or "counted by mistake" in a.explanation for a in months)
        c.lines.append(
            Line(
                code="arrears",
                label="Arrears (back pay)" if total >= 0 else "Recovery for earlier months",
                amount=total,
                category=EARNING,
                formula=" + ".join(inr(a.total) for a in months) + f" = {inr(total)}",
                reason=(f"{months_txt} {'was' if len(months) == 1 else 'were'} worked out again after being paid "
                        "(a salary change or unpaid days reported late); the difference is paid now, or taken back if it is negative."
                        if late_days or total < 0 else
                        f"Salary for {months_txt} was revised after it was paid. Each month is worked out again with the revised salary and the difference is paid now."),
                rules=[policy.ref],
                children=children,
            )
        )
    c.amounts["arrears"] = total


# --------------------------------------------------------------------------- one-time items

def one_time_items(c: EmployeeCalc):
    for item in sorted(c.emp.get("one_time") or [], key=lambda i: (i.get("kind", ""), i.get("code", ""), i.get("ref", ""))):
        amount = d(item["amount"])
        code = item.get("code") or "other"
        label = item.get("label") or component_label(code)
        source = item.get("source_text")
        reason = "One-time item confirmed by the payroll maker"
        if source:
            reason += f' from the instruction "{source}"'
        if item.get("kind") == "deduction":
            c.other_deductions += amount
            c.amounts[f"deduction.{code}"] = c.amounts.get(f"deduction.{code}", ZERO) + amount
            # shown after the statutory deductions (see other_deductions)
            c.deduction_lines.append(
                Line(code=f"deduction.{code}", label=label, amount=amount, category=DEDUCTION, formula=inr(amount), reason=reason,
                     inputs={"ref": item.get("ref", "")})
            )
        else:
            c.one_time_earnings += amount
            if item.get("taxable", True):
                c.one_time_taxable += amount
            c.amounts[f"one_time.{code}"] = c.amounts.get(f"one_time.{code}", ZERO) + amount
            c.lines.append(
                Line(code=f"one_time.{code}", label=label, amount=amount, category=EARNING, formula=inr(amount),
                     reason=reason + ("" if item.get("taxable", True) else " (not taxable)"), inputs={"ref": item.get("ref", "")})
            )


def gross_pay(c: EmployeeCalc):
    regular = c.earn.regular_gross
    c.gross = gross = regular + c.arrears_total + c.one_time_earnings
    parts = [f"{inr(regular)} regular pay"]
    if c.arrears_total:
        parts.append(f"{inr(c.arrears_total)} arrears")
    if c.one_time_earnings:
        parts.append(f"{inr(c.one_time_earnings)} one-time")
    c.lines.append(
        Line(
            code="gross",
            label="Gross pay",
            amount=gross,
            category=TOTAL,
            formula=" + ".join(parts) + f" = {inr(gross)}" if len(parts) > 1 else f"{inr(gross)}",
            reason="Everything earned this month before deductions",
        )
    )
    c.amounts["regular_gross"] = regular
    c.amounts["gross"] = gross


# --------------------------------------------------------------------------- deductions

def pf_employee(c: EmployeeCalc):
    earn, money_text = c.earn, c.money_text
    pf = c.pf = compute_pf(c.emp, earn, c.versions)
    pf_rate = d(pf.rule.payload["employee_rate"])
    ceiling = d(pf.rule.payload["wage_ceiling"])
    if not pf.applicable:
        formula = "Not applicable"
        reason = "This employee is not a PF member (marked in HR data)."
    elif pf.policy.get("wage_base") == "capped":
        capped = pf.pf_wage > ceiling
        formula = (
            f"{pct(pf_rate)} of {inr(pf.employee_base, 2 if pf.employee_base != pf.employee_base.to_integral_value() else None)}"
            + (f" ({inr(ceiling)} cap)" if capped else "")
            + f" = {inr(pf.employee)}"
        )
        reason = (
            f"PF is worked out on the first {inr(ceiling)} of PF wages (company setting). "
            + (f"PF wages this month are {money_text(pf.pf_wage)}, above the cap, so the cap is used." if capped
               else f"PF wages this month are {money_text(pf.pf_wage)}, below the cap, so all of it is used.")
        )
    else:
        formula = f"{pct(pf_rate)} of {money_text(pf.employee_base)} = {inr(pf.employee)}"
        reason = f"PF is worked out on all of the PF wages (company setting): {money_text(pf.pf_wage)}."
    if pf.wage_code_text:
        reason += " " + pf.wage_code_text + "."
    wage_parts = " + ".join(
        f"{component_label(k)} {money_text(earn.earned.get(k, ZERO))}" for k in pf.rule.payload["wage_components"] if k in earn.earned
    ) or "no PF wage components"
    children = [
        Line(code="pf_wage", label="PF wages", amount=pf.pf_wage, category=INFO,
             formula=wage_parts + (f" + Labour Codes add-back {money_text(pf.addback)}" if pf.addback else "") + f" = {money_text(pf.pf_wage)}",
             reason="PF wages are the parts of pay listed in the PF rule, after unpaid leave",
             rules=[pf.rule.ref] + ([pf.wage_code.ref] if pf.wage_code else []))
    ]
    on_arrears = c.arrears_pf_employee
    total = pf.employee + (on_arrears if pf.applicable else ZERO)
    if on_arrears and pf.applicable:
        children.append(Line(code="pf_employee.current", label="PF on this month's pay", amount=pf.employee, category=INFO, formula=formula))
        children.append(
            Line(code="pf_employee.arrears", label="PF on arrears", amount=on_arrears, category=INFO,
                 formula=" + ".join(f"{a.period.short_label}: {inr(a.pf_employee)}" for a in c.arrears_months if a.pf_employee) + f" = {inr(on_arrears)}",
                 reason="PF for the revised months worked out again with the rules of those months, minus PF already paid")
        )
        formula = f"{inr(pf.employee)} this month + {inr(on_arrears)} on arrears = {inr(total)}"
    elif c.arrears_months and pf.applicable:
        reason += " The arrears add no PF: PF for the revised months comes out the same as what was already paid."
    c.lines.append(
        Line(code="pf_employee", label="PF (employee 12%)", amount=total, category=DEDUCTION,
             formula=formula, reason=reason, rules=[pf.rule.ref, pf.policy.ref] + ([pf.wage_code.ref] if pf.wage_code and pf.addback else []),
             inputs={"pf_wage": pf.pf_wage, "base": pf.employee_base, "rate": pf_rate, "ceiling": ceiling},
             children=children)
    )
    c.amounts["pf_wage"] = pf.pf_wage
    c.amounts["pf_employee"] = total
    c.add_deduction("PF", total)


def esi_employee(c: EmployeeCalc):
    rule = pol.resolve_statutory(c.versions, "esi", c.period, "IN")
    policy = pol.resolve_company(c.versions, "esi_policy", c.period)
    threshold = d(rule.payload["threshold"])
    elig_choice = policy.get("eligibility_wage")
    elig_wage = c.wage(elig_choice)
    esi_wage = c.wage(policy.get("contribution_wage"))
    method = rule.payload.get("rounding", "up")
    covered = c.emp.get("esi_applicable", True) is not False
    eligible = covered and elig_wage <= threshold and esi_wage > 0
    if eligible:
        employee = round_amount(esi_wage * d(rule.payload["employee_rate"]), method)
        employer = round_amount(esi_wage * d(rule.payload["employer_rate"]), method)
        formula = f"{pct(d(rule.payload['employee_rate']))} of {inr(esi_wage)} = {num(esi_wage * d(rule.payload['employee_rate']))}, rounded {'up' if method == 'up' else method} = {inr(employee)}"
        reason = f"ESI applies because {WAGE_LABELS[elig_choice]} ({inr(elig_wage)}) is {inr(threshold)} or less."
    else:
        employee = employer = ZERO
        formula = "Not applicable"
        if not covered:
            reason = "This employee is not covered by ESI (marked in HR data)."
        else:
            reason = f"No ESI: {WAGE_LABELS[elig_choice]} ({inr(elig_wage)}) is above {inr(threshold)}."
    c.lines.append(
        Line(code="esi_employee", label="ESI (employee 0.75%)", amount=employee, category=DEDUCTION, formula=formula,
             reason=reason, rules=[rule.ref, policy.ref],
             inputs={"eligibility_wage": elig_wage, "esi_wage": esi_wage, "threshold": threshold})
    )
    c.amounts["esi_wage"] = esi_wage if eligible else ZERO
    c.amounts["esi_employee"] = employee
    c.esi = {"rule": rule, "policy": policy, "wage": esi_wage, "eligible": eligible, "employer": employer, "reason": reason}
    c.add_deduction("ESI", employee)


def professional_tax(c: EmployeeCalc):
    period, versions = c.period, c.versions
    state = c.state = state_code(c.emp.get("work_state")) or state_code(c.company.get("state"))
    policy = pol.resolve_company(versions, "pt_policy", period)
    basis_choice = policy.get("basis")
    basis = c.wage(basis_choice)
    rule = pol.resolve(versions, "professional_tax", period, f"IN-{state}") if state else None
    rules = [policy.ref]
    if rule is None:
        pt = ZERO
        formula = "Not applicable"
        reason = f"No professional tax rule is stored for {state or 'this employee’s state'} on {period.start.isoformat()}."
        if state:
            c.issues.append({"level": "info", "message": f"No professional tax rule for state {state}; PT is 0."})
    else:
        rules.insert(0, rule.ref)
        slab = select_pt_slab(rule.payload["slabs"], basis, period.month, c.emp.get("gender"))
        if slab is None:
            pt = ZERO
            formula = "No slab matches"
            reason = f"No slab of the {state} rule matches {inr(basis)}"
            if any(s.get("gender") for s in rule.payload["slabs"]) and not c.emp.get("gender"):
                c.issues.append({"level": "warning", "message": "Professional tax depends on gender in this state, but gender is missing."})
        else:
            if "rate" in slab:
                pt = round_amount(basis * d(slab["rate"]), "nearest")
                formula = f"{pct(d(slab['rate']))} of {inr(basis)} = {inr(pt)}"
            else:
                pt = d(slab["amount"])
                formula = f"{inr(pt)} (slab: {_slab_text(slab)})"
            reason = f"{state} professional tax on {WAGE_LABELS[basis_choice]} of {inr(basis)}: the slab for {_slab_text(slab)} applies."
    c.lines.append(
        Line(code="pt", label="Professional tax", amount=pt, category=DEDUCTION, formula=formula, reason=reason, rules=rules,
             inputs={"basis": basis, "state": state})
    )
    c.amounts["pt_basis"] = basis
    c.amounts["pt"] = pt
    c.add_deduction("professional tax", pt)


def income_tax(c: EmployeeCalc):
    line, amounts = compute_tds(c.emp, c.period, c.versions, c.gross, c.one_time_earnings - c.one_time_taxable, c.one_time_taxable)
    c.lines.append(line)
    c.amounts.update(amounts)
    c.tds = amounts["tds"]
    c.add_deduction("TDS", c.tds)


def other_deductions(c: EmployeeCalc):
    """One-time deductions (advance or loan recovery…) come after the statutory ones."""
    c.lines.extend(c.deduction_lines)
    if c.other_deductions:
        c.add_deduction("other deductions", c.other_deductions)


def net_pay(c: EmployeeCalc):
    total = sum((amount for _, amount in c.deductions), ZERO)
    c.lines.append(
        Line(code="total_deductions", label="Total deductions", amount=total, category=TOTAL,
             formula=" + ".join(inr(amount) for _, amount in c.deductions) + f" = {inr(total)}",
             reason=" + ".join(name for name, _ in c.deductions))
    )
    net = c.gross - total
    c.lines.append(
        Line(code="net_pay", label="Net pay", amount=net, category=TOTAL,
             formula=f"{inr(c.gross)} − {inr(total)} = {inr(net)}",
             reason="Gross pay minus deductions: what reaches the employee's bank account")
    )
    c.amounts["total_deductions"] = total
    c.amounts["net_pay"] = net
    if net < 0:
        c.issues.append({"level": "error", "message": f"Net pay is negative ({inr(net)}). Check deductions before approving."})


# --------------------------------------------------------------------------- paid by the company on top

def pf_employer(c: EmployeeCalc):
    pf = c.pf
    on_arrears = c.arrears_pf_employer
    total = pf.employer + (on_arrears if pf.applicable else ZERO)
    eps_rate = d(pf.rule.payload.get("eps_rate", 0))
    ceiling = d(pf.rule.payload["wage_ceiling"])
    c.lines.append(
        Line(code="pf_employer", label="PF (company 12%)", amount=total, category=EMPLOYER,
             formula=f"{pct(d(pf.rule.payload['employer_rate']))} of {c.money_text(pf.employer_base)} = {inr(pf.employer)}"
             + (f" + {inr(on_arrears)} on arrears = {inr(total)}" if on_arrears and pf.applicable else ""),
             reason=f"Paid by the company on top of salary. Of this, {inr(pf.eps)} goes to the pension scheme "
             f"({pct(eps_rate)} of PF wages up to {inr(d(pf.rule.payload.get('eps_wage_ceiling', ceiling)))}) and the rest to the PF account.",
             rules=[pf.rule.ref, pf.policy.ref],
             children=[
                 Line(code="eps_employer", label="Pension (EPS)", amount=pf.eps, category=INFO),
                 Line(code="epf_employer", label="PF account (EPF)", amount=total - pf.eps, category=INFO),
             ])
    )
    c.amounts["pf_employer"] = total
    c.amounts["eps_employer"] = pf.eps
    c.amounts["epf_employer"] = total - pf.eps
    c.add_employer_contribution("PF", total)


def esi_employer(c: EmployeeCalc):
    esi = c.esi
    rule, employer = esi["rule"], esi["employer"]
    c.lines.append(
        Line(code="esi_employer", label="ESI (company 3.25%)", amount=employer, category=EMPLOYER,
             formula=(f"{pct(d(rule.payload['employer_rate']))} of {inr(esi['wage'])} = {num(esi['wage'] * d(rule.payload['employer_rate']))}, rounded up = {inr(employer)}"
                      if esi["eligible"] else "Not applicable"),
             reason="Paid by the company on top of salary" if esi["eligible"] else esi["reason"], rules=[rule.ref, esi["policy"].ref])
    )
    c.amounts["esi_employer"] = employer
    c.add_employer_contribution("ESI", employer)


def cost_to_company(c: EmployeeCalc):
    cost = c.gross + sum((amount for _, amount in c.employer_contributions), ZERO)
    names = [name for name, _ in c.employer_contributions]
    c.lines.append(
        Line(code="employer_cost", label="Cost to company this month", amount=cost, category=TOTAL,
             formula=" + ".join([inr(c.gross)] + [inr(amount) for _, amount in c.employer_contributions]) + f" = {inr(cost)}",
             reason="Gross pay plus the company's own " + (", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else "".join(names)))
    )
    c.amounts["employer_cost"] = cost


STEPS = (
    regular_pay,
    arrears,
    one_time_items,
    gross_pay,
    pf_employee,
    esi_employee,
    professional_tax,
    income_tax,
    other_deductions,
    net_pay,
    pf_employer,
    esi_employer,
    cost_to_company,
)


def facts(c: EmployeeCalc) -> dict:
    """What later months need from this one: earned pay and PF (for arrears), gross and TDS (for the year's tax)."""
    pf = c.pf
    return {
        "earned": {k: to_str(v) for k, v in c.earn.earned.items()},
        "pf_employee_regular": to_str(pf.employee),
        "pf_employer_regular": to_str(pf.employer),
        "arrears_detail": [
            {"period": str(a.period), "components": {k: to_str(v) for k, v in a.components.items()}, "total": to_str(a.total),
             "pf_employee": to_str(a.pf_employee if pf.applicable else ZERO), "pf_employer": to_str(a.pf_employer if pf.applicable else ZERO)}
            for a in c.arrears_months
        ],
        "gross": to_str(c.gross),
        "taxable_gross": to_str(c.gross - (c.one_time_earnings - c.one_time_taxable)),
        "tds": to_str(c.tds),
        "unpaid_days": to_str(c.earn.lop_days),
        "contracted_gross": to_str(c.earn.contracted_gross),
    }
