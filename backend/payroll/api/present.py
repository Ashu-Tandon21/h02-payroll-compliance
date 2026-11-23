"""Shape model objects into the JSON the screens use."""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from payroll_engine.diff import compare_results
from payroll_engine.money import to_str
from payroll_engine.periods import Period

from ..ai import client as ai_client
from ..ingestion.canonical import catalogue
from ..ingestion.normalize import unpaid_days_for
from ..models import ChangeRequest, PayrollRun, StatutoryDue
from ..services import dues
from ..services import runs as run_service
from ..services.common import is_in_group
from ..workflow import dispatch


def _user(u):
    if not u:
        return None
    return {"id": u.pk, "username": u.username, "name": (u.get_full_name() or u.username)}


def _dt(value):
    return timezone.localtime(value).isoformat() if value else None


def me(user):
    return {
        "authenticated": True,
        "user": _user(user),
        "roles": [r for r in ("maker", "checker") if is_in_group(user, r)],
        "ai": ai_client.describe(),
        "workflow_backend": settings.WORKFLOW_BACKEND,
    }


def company(c, detail=False):
    out = {"id": c.pk, "code": c.code, "name": c.name, "state": c.state, "city": c.city, "ai_mode": c.ai_mode,
           "pan": c.pan, "tan": c.tan, "pf_establishment_code": c.pf_establishment_code, "esi_employer_code": c.esi_employer_code,
           "pay_day": c.pay_day}
    if detail:
        out.update({"ai_mode_label": c.get_ai_mode_display(), "ai_mode_changed_by": _user(c.ai_mode_changed_by),
                    "ai_mode_changed_at": _dt(c.ai_mode_changed_at), "ai": ai_client.describe()})
    return out


def employee(e):
    return {"code": e.code, "name": e.name, "work_state": e.work_state, "gender": e.gender,
            "date_of_joining": e.date_of_joining and e.date_of_joining.isoformat(),
            "date_of_exit": e.date_of_exit and e.date_of_exit.isoformat(), "active": e.active}


def ai(i, full=False):
    if i is None:
        return None
    out = {
        "id": i.pk, "purpose": i.purpose, "purpose_label": i.get_purpose_display(), "provider": i.provider, "model": i.model,
        "status": i.status, "status_label": i.get_status_display(), "response": i.response, "validation": i.validation,
        "created_at": _dt(i.created_at), "reviewed_by": _user(i.reviewed_by), "reviewed_at": _dt(i.reviewed_at),
        "reused_saved_answer": getattr(i, "reused", False), "reuse_count": i.reuse_count, "latency_ms": i.latency_ms,
    }
    if full:
        out["request"] = i.request
        out["usage"] = i.usage
        out["input_hash"] = i.input_hash
    return out


def connector(c):
    return {
        "id": c.pk, "name": c.name, "kind": c.kind, "system_name": c.system_name, "profile": c.profile, "base_url": c.base_url, "username": c.username,
        "has_password": bool(c.password), "recipe": c.recipe, "recipe_confirmed_by": _user(c.recipe_confirmed_by),
        "recipe_confirmed_at": _dt(c.recipe_confirmed_at), "recipe_ai": ai(c.recipe_ai),
        "last_job": job(c.jobs.order_by("-id").first()) if c.jobs.exists() else None,
    }


def job(j, detail=False):
    if j is None:
        return None
    out = {
        "id": j.pk, "kind": j.kind, "source_system": j.source_system, "status": j.status, "status_label": j.get_status_display(),
        "original_filename": j.original_filename, "error": j.error, "created_at": _dt(j.created_at), "started_at": _dt(j.started_at),
        "finished_at": _dt(j.finished_at), "started_by": _user(j.started_by), "table_count": j.tables.count(),
        "snapshot_id": j.snapshots.order_by("-id").values_list("id", flat=True).first(),
    }
    if detail:
        out["log"] = j.log
        out["stats"] = {k: v for k, v in (j.stats or {}).items() if k != "heartbeat"}
        out["tables"] = [table(t) for t in j.tables.select_related("mapping", "mapping__suggested_by", "mapping__confirmed_by")]
        out["catalogue"] = catalogue()
    return out


def table(t):
    m = t.mapping
    return {
        "id": t.pk, "name": t.name, "dataset_hint": t.dataset_hint, "source_url": t.source_url, "headers": t.headers,
        "row_count": len(t.rows), "rows": t.rows[:200], "read_info": t.read_info,
        "mapping": None if m is None else {
            "id": m.pk, "mapping": m.mapping, "value_maps": m.value_maps, "options": m.options,
            "confirmed": bool(m.confirmed_at), "confirmed_by": _user(m.confirmed_by), "confirmed_at": _dt(m.confirmed_at),
            "suggestion": (m.options or {}).get("suggestion"), "suggested_by": ai(m.suggested_by),
            "reused": bool(m.confirmed_at and m.confirmed_at < t.job.created_at),
        },
    }


def snapshot_summary(s):
    if s is None:
        return None
    levels = [c.get("level") for c in s.checks]
    return {
        "id": s.pk, "source_kind": s.source_kind, "source_system": s.source_system, "status": s.status,
        "status_label": s.get_status_display(), "created_at": _dt(s.created_at), "reviewed_by": _user(s.reviewed_by),
        "reviewed_at": _dt(s.reviewed_at), "employee_count": len(s.data.get("employees", [])), "content_hash": s.content_hash,
        "errors": levels.count("error"), "warnings": levels.count("warning"), "job_id": s.job_id,
    }


def snapshot(s):
    out = snapshot_summary(s)
    data = s.data
    periods = sorted({r["period"] for r in data.get("attendance", [])} | {
        str(p) for r in data.get("leave", []) if r["unpaid"]
        for p in _periods(r["start_date"], r["end_date"])})
    out.update({
        "data": data,
        "checks": s.checks,
        "review_note": s.review_note,
        "unpaid_by_month": {
            "periods": periods,
            "rows": [{"code": e["code"], "name": e["name"],
                      "days": {p: to_str(unpaid_days_for(data, e["code"], Period.parse(p))) for p in periods}}
                     for e in data.get("employees", [])],
        },
        "used_by_runs": [run_summary(r) for r in s.runs.order_by("period")],
    })
    return out


def _periods(start, end):
    p, last = Period.parse(start[:7]), Period.parse(end[:7])
    while p <= last:
        yield p
        p = p.next()


def run_label(r: PayrollRun) -> str:
    label = Period.parse(r.period).label
    return f"{label} (draft #{r.pk})" if r.run_type == PayrollRun.TYPE_DRAFT else label


def _month_people(r: PayrollRun) -> list[dict]:
    from ..services.inputs import _in_period

    period = r.period_obj
    return [{"code": e["code"], "name": e.get("name", e["code"]), "work_state": e.get("work_state", "")}
            for e in (r.snapshot.data.get("employees", []) if r.snapshot_id else []) if _in_period(e, period)]


def run_summary(r: PayrollRun):
    totals = (r.result or {}).get("totals", {})
    return {
        "id": r.pk, "period": r.period, "label": run_label(r), "run_type": r.run_type, "status": r.status,
        "status_label": r.get_status_display(), "net_pay": totals.get("net_pay"), "gross": totals.get("gross"),
        "employer_cost": totals.get("employer_cost"),
        "employee_count": (r.result or {}).get("employee_count"), "calculated_at": _dt(r.calculated_at),
        "base_run_id": r.base_run_id, "policy_overrides": r.policy_overrides, "updated_at": _dt(r.updated_at),
        "workflow_backend": r.workflow_backend,
    }


def comparison_base(r: PayrollRun) -> PayrollRun | None:
    if r.run_type == PayrollRun.TYPE_DRAFT:
        return r.base_run if r.base_run and r.base_run.result else None
    prev = str(Period.parse(r.period).prev())
    candidates = PayrollRun.objects.filter(company=r.company, period=prev, run_type=PayrollRun.TYPE_REGULAR).exclude(
        status=PayrollRun.CANCELLED).exclude(result=None)
    return candidates.filter(status=PayrollRun.FINALIZED).first() or candidates.order_by("-id").first()


def change(c: ChangeRequest):
    return {
        "id": c.pk, "period": c.period, "run_id": c.run_id, "text": c.text, "status": c.status, "proposal": c.proposal,
        "ai": ai(c.ai), "created_by": _user(c.created_by), "created_at": _dt(c.created_at), "decided_by": _user(c.decided_by),
        "decided_at": _dt(c.decided_at),
    }


def policy(v, used: set[str]):
    return {
        "id": v.pk, "scope": v.scope, "key": v.key, "jurisdiction": v.jurisdiction, "effective_from": v.effective_from.isoformat(),
        "payload": v.payload, "title": v.title, "note": v.note, "created_by": _user(v.created_by), "created_at": _dt(v.created_at),
        "company_id": v.company_id, "used_by_final_month": str(v.pk) in used,
    }


def run_detail(r: PayrollRun, user, with_workflow: bool = True):
    out = run_summary(r)
    is_maker, is_checker = is_in_group(user, "maker"), is_in_group(user, "checker")
    status = r.status
    regular = r.run_type == PayrollRun.TYPE_REGULAR
    pending_changes = ChangeRequest.objects.filter(company=r.company, period=r.period, status=ChangeRequest.PROPOSED).count()
    out.update({
        "company": company(r.company),
        "period_label": Period.parse(r.period).label,
        "knowledge_date": r.knowledge_date.isoformat(),
        "note": r.note,
        "snapshot": snapshot_summary(r.snapshot),
        # Who is in this month's HR data (for picking a person before the month is calculated).
        "people": _month_people(r),
        # Months already paid, for an unpaid absence found after its month was paid.
        "paid_months": [{"period": x.period, "label": x.period_obj.label} for x in PayrollRun.objects.filter(
            company=r.company, run_type=PayrollRun.TYPE_REGULAR, status=PayrollRun.FINALIZED, period__lt=r.period).order_by("-period")],
        "data_reviewed_by": _user(r.data_reviewed_by), "data_reviewed_at": _dt(r.data_reviewed_at),
        "calculated_by": _user(r.calculated_by), "calculation_count": r.calculation_count,
        "input_hash": r.input_hash, "result_hash": r.result_hash,
        "submitted_by": _user(r.submitted_by), "submitted_at": _dt(r.submitted_at), "submitted_result_hash": r.submitted_result_hash,
        "decided_by": _user(r.decided_by), "decided_at": _dt(r.decided_at), "decision_note": r.decision_note,
        "finalized_at": _dt(r.finalized_at), "files": r.files, "last_error": r.last_error,
        "result": r.result,
        "events": [{"at": _dt(e.at), "actor": e.actor, "action": e.action, "message": e.message, "detail": e.detail}
                   for e in r.events.all()],
        "changes": [change(c) for c in ChangeRequest.objects.filter(company=r.company, period=r.period).exclude(
            status=ChangeRequest.REJECTED).order_by("id")],
        "pending_changes": pending_changes,
        "base_run": run_summary(r.base_run) if r.base_run else None,
    })
    base = comparison_base(r)
    out["comparison"] = None
    if base and r.result:
        out["comparison"] = compare_results(base.result, r.result, run_label(base), run_label(r))
        out["comparison"]["base_run_id"] = base.pk
        out["comparison"]["base_status"] = base.status
    unreviewed = not r.data_reviewed_at
    newer = run_service.newer_hr_data(r) if regular and status not in (PayrollRun.FINALIZED, PayrollRun.CANCELLED) else None
    out["newer_hr_data"] = snapshot_summary(newer) if newer else None
    if regular and status in (PayrollRun.CALCULATED, PayrollRun.REJECTED, PayrollRun.AWAITING_APPROVAL) and r.input_hash:
        out["freshness"] = run_service.freshness(r)
    else:
        out["freshness"] = None
    locked = r.is_locked
    out["can"] = {
        "review_data": is_maker and not locked and unreviewed,
        "add_changes": is_maker and not locked and regular,
        "calculate": is_maker and not locked and not unreviewed,
        "submit": is_maker and regular and status in (PayrollRun.CALCULATED, PayrollRun.REJECTED) and not pending_changes,
        "approve": is_checker and regular and status == PayrollRun.AWAITING_APPROVAL
                   and user.pk not in (r.submitted_by_id, r.calculated_by_id),
        "reject": is_checker and regular and status == PayrollRun.AWAITING_APPROVAL
                  and user.pk not in (r.submitted_by_id, r.calculated_by_id),
        "cancel": is_maker and status in (PayrollRun.DRAFT, PayrollRun.CALCULATED, PayrollRun.REJECTED),
        "draft_rerun": is_maker and bool(r.result),
        "use_latest_data": is_maker and not locked and bool(newer),
        "change_day_count": is_maker and not locked and regular,
    }
    out["day_count"] = run_service.effective_day_count(r)
    out["dues"] = [due(x) for x in r.dues.all()]
    out["four_eyes_note"] = ""
    if status == PayrollRun.AWAITING_APPROVAL and is_checker and user.pk in (r.submitted_by_id, r.calculated_by_id):
        out["four_eyes_note"] = "You prepared this month, so a different person must approve it."
    order = [PayrollRun.DRAFT, PayrollRun.CALCULATED, PayrollRun.AWAITING_APPROVAL, PayrollRun.APPROVED, PayrollRun.FINALIZED]
    rank = order.index(status) if status in order else (1 if status == PayrollRun.REJECTED else 0)
    out["steps"] = [
        {"key": "data", "label": "Review HR data", "done": not unreviewed},
        {"key": "changes", "label": "Changes", "done": not unreviewed and pending_changes == 0},
        {"key": "calculate", "label": "Calculate", "done": rank >= 1 and status != PayrollRun.DRAFT},
        {"key": "submit", "label": "Submit", "done": rank >= 2},
        {"key": "approve", "label": "Checker approves", "done": rank >= 3},
        {"key": "files", "label": "Register & files", "done": rank >= 4},
    ] if regular else []
    if with_workflow:
        out["workflow"] = dispatch.describe(r)
    else:
        out["workflow"] = {"backend": r.workflow_backend or "local", "workflow_id": r.workflow_id}
    return out



def due(x: StatutoryDue, today=None):
    return {
        "id": x.pk, "run_id": x.run_id, "period": x.period, "period_label": Period.parse(x.period).label, "kind": x.kind,
        "title": dues.title(x), "jurisdiction": x.jurisdiction, "employee_amount": to_str(x.employee_amount),
        "employer_amount": to_str(x.employer_amount), "total": to_str(x.total), "people": x.people,
        "due_date": x.due_date.isoformat(), "paid_on": x.paid_on and x.paid_on.isoformat(), "reference": x.reference,
        "paid_by": _user(x.paid_by), "paid_at": _dt(x.paid_at), "note": x.note, "status": dues.status_of(x, today),
    }
