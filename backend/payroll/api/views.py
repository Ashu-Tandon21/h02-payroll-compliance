"""REST API used by the screens. Views stay thin: rules live in services, numbers in the engine."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from payroll_engine import policies as engine_policies
from payroll_engine.diff import compare_results
from payroll_engine.engine import calculate_employee
from payroll_engine.periods import Period

from ..ai import client as ai_client
from ..ai import tasks as ai_tasks
from ..ingestion import jobs as ingestion_jobs
from ..ingestion import profiles
from ..ingestion.canonical import GROUP_LABELS, catalogue
from ..ingestion.normalize import default_value_maps
from ..models import (AIInteraction, ChangeRequest, Company, Connector, DataSnapshot, IngestionJob, PayrollRun, PolicyVersion,
                      RawTable)
from ..services import changes as change_service
from ..services import exports
from ..services import policies as policy_service
from ..services import runs as run_service
from ..services import snapshots as snapshot_service
from ..services.common import RuleViolation, is_in_group
from ..workflow import dispatch
from . import present as P

# =========================================================================== auth


@api_view(["GET"])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def csrf(request):
    return Response({"ok": True})


@api_view(["POST"])
@permission_classes([AllowAny])
def login_view(request):
    user = authenticate(request, username=request.data.get("username", ""), password=request.data.get("password", ""))
    if user is None:
        return Response({"detail": "Wrong username or password."}, status=400)
    login(request, user)
    return Response(P.me(user))


@api_view(["POST"])
def logout_view(request):
    logout(request)
    return Response({"ok": True})


@api_view(["GET"])
@permission_classes([AllowAny])
def me(request):
    if not request.user or not request.user.is_authenticated:
        return Response({"authenticated": False})
    return Response(P.me(request.user))


# =========================================================================== company


@api_view(["GET"])
def companies(request):
    return Response([P.company(c) for c in Company.objects.order_by("name")])


@api_view(["GET", "PATCH"])
def company_detail(request, company_id):
    """The company, and whether its HR data may be sent to an AI model (a maker decides; who and when is kept)."""
    company = get_object_or_404(Company, pk=company_id)
    if request.method == "PATCH":
        if not is_in_group(request.user, "maker"):
            raise RuleViolation("Only a maker can change whether AI is used.", code="forbidden")
        mode = request.data.get("ai_mode")
        if mode not in (Company.AI_ALLOWED, Company.AI_OFF):
            raise RuleViolation("ai_mode must be 'allowed' or 'off'.")
        if mode != company.ai_mode:
            company.ai_mode, company.ai_mode_changed_by, company.ai_mode_changed_at = mode, request.user, timezone.now()
            company.save(update_fields=["ai_mode", "ai_mode_changed_by", "ai_mode_changed_at"])
    return Response(P.company(company, detail=True))


@api_view(["GET"])
def hr_tool_profiles(request):
    """HR tools the system already knows: their screens and export layouts are mapped without AI."""
    return Response(profiles.catalogue())


@api_view(["GET"])
def company_overview(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    runs = list(PayrollRun.objects.filter(company=company).exclude(status=PayrollRun.CANCELLED).select_related("snapshot"))
    months = {}
    for r in sorted(runs, key=lambda r: (r.period, r.id)):
        entry = months.setdefault(r.period, {"period": r.period, "label": Period.parse(r.period).label, "regular": None, "drafts": []})
        if r.run_type == PayrollRun.TYPE_REGULAR:
            entry["regular"] = P.run_summary(r)
        else:
            entry["drafts"].append(P.run_summary(r))
    latest_snapshot = DataSnapshot.objects.filter(company=company).order_by("-id").first()
    finals = sorted(r.period for r in runs if r.status == PayrollRun.FINALIZED and r.run_type == PayrollRun.TYPE_REGULAR)
    next_period = str(Period.parse(finals[-1]).next()) if finals else ""
    return Response({
        "company": P.company(company),
        "months": [months[k] for k in sorted(months, reverse=True)],
        "next_period": next_period,
        "latest_snapshot": P.snapshot_summary(latest_snapshot) if latest_snapshot else None,
        "approved_snapshot": P.snapshot_summary(company.snapshots.filter(status="approved").order_by("-reviewed_at", "-id").first())
        if company.snapshots.filter(status="approved").exists() else None,
        "employee_count": company.employees.filter(active=True).count(),
        "awaiting_approval": [P.run_summary(r) for r in runs if r.status == PayrollRun.AWAITING_APPROVAL],
        "ai": ai_client.describe(),
        "workflow_backend": settings.WORKFLOW_BACKEND,
        "temporal_ui": settings.TEMPORAL_UI_URL if settings.WORKFLOW_BACKEND == "temporal" else "",
    })


@api_view(["GET"])
def employees(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    return Response([P.employee(e) for e in company.employees.order_by("code")])


# =========================================================================== data in: connectors & jobs


@api_view(["GET", "POST"])
def connectors(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    if request.method == "POST":
        if not is_in_group(request.user, "maker"):
            raise RuleViolation("Only a maker can add an HR tool.", code="forbidden")
        data = request.data
        if not data.get("name") or not data.get("base_url"):
            raise RuleViolation("Give the HR tool a name and its web address.")
        known = profiles.profile_for(data.get("profile"))
        if data.get("profile") and not known:
            raise RuleViolation(f"Unknown HR tool profile: {data['profile']}")
        conn = Connector.objects.create(company=company, name=data["name"], kind=Connector.KIND_BROWSER,
                                        system_name=data.get("system_name") or (known.name if known else data["name"]),
                                        base_url=data["base_url"], username=data.get("username", ""), password=data.get("password", ""),
                                        profile=known.system if known else "",
                                        # A known tool's screens are suggested straight away; a person still confirms them.
                                        recipe={"datasets": profiles.screens_for(known)} if known else {})
        return Response(P.connector(conn), status=201)
    return Response([P.connector(c) for c in company.connectors.order_by("id")])


@api_view(["PATCH", "DELETE"])
def connector_detail(request, connector_id):
    conn = get_object_or_404(Connector, pk=connector_id)
    if not is_in_group(request.user, "maker"):
        raise RuleViolation("Only a maker can change an HR tool connection.", code="forbidden")
    if request.method == "DELETE":
        conn.delete()
        return Response(status=204)
    for field in ("name", "system_name", "base_url", "username"):
        if field in request.data:
            setattr(conn, field, request.data[field])
    if request.data.get("password"):
        conn.password = request.data["password"]
    conn.save()
    return Response(P.connector(conn))


@api_view(["POST"])
def connector_discover(request, connector_id):
    """The agent logs in and suggests which screens hold employees, salary and leave. A person confirms."""
    from ..ingestion.browser_agent import AgentError, BrowserAgent

    conn = get_object_or_404(Connector, pk=connector_id)
    if not is_in_group(request.user, "maker"):
        raise RuleViolation("Only a maker can connect an HR tool.", code="forbidden")
    messages = []
    agent = BrowserAgent(conn, lambda msg, level="info", **kw: messages.append({"level": level, "message": msg}))
    try:
        found = agent.discover(conn.company, force=bool(request.data.get("force")))
    except AgentError as exc:
        raise RuleViolation(str(exc)) from exc
    recipe = {"login_url": conn.recipe.get("login_url", ""), "datasets": {}}
    for key, pick in found["datasets"].items():
        previous = (conn.recipe.get("datasets") or {}).get(key) or {}
        recipe["datasets"][key] = {"url": pick["url"] if pick else "", "reason": pick["reason"] if pick else "no matching link",
                                   "follow_row_links": (pick or {}).get("follow_row_links", previous.get("follow_row_links", False))}
    conn.recipe = recipe
    conn.recipe_ai = found["interaction"]
    if found["profile"]:
        conn.profile = found["profile"].system
    conn.recipe_confirmed_at = None
    conn.recipe_confirmed_by = None
    conn.save()
    return Response({"connector": P.connector(conn), "links": found["links"], "log": messages, "ai": P.ai(found["interaction"])})


@api_view(["POST"])
def connector_confirm_recipe(request, connector_id):
    conn = get_object_or_404(Connector, pk=connector_id)
    if not is_in_group(request.user, "maker"):
        raise RuleViolation("Only a maker can confirm screens.", code="forbidden")
    datasets = request.data.get("datasets") or {}
    clean = {}
    for key in ("employees", "compensation", "leave"):
        spec = datasets.get(key) or {}
        if spec.get("url"):
            clean[key] = {"url": spec["url"], "reason": spec.get("reason", ""), "follow_row_links": bool(spec.get("follow_row_links"))}
    if not clean.get("employees"):
        raise RuleViolation("At least the employees screen is needed.")
    from ..ingestion.browser_agent import DATASETS, off_site

    login_url = request.data.get("login_url", conn.recipe.get("login_url", ""))
    wrong = [f"{label} screen {spec['url']} is on {other}" for key, label in DATASETS.items()
             if (spec := clean.get(key)) and (other := off_site(spec["url"], conn.base_url))]
    if login_url and (other := off_site(login_url, conn.base_url)):
        wrong.append(f"login page {login_url} is on {other}")
    if wrong:
        raise RuleViolation(f"Every screen must be on {conn.name} ({conn.base_url}): " + "; ".join(wrong)
                            + ". Use a path such as /employees, or add the other HR tool as its own connection.", code="off_site")
    conn.recipe = {"login_url": login_url, "datasets": clean}
    conn.recipe_confirmed_by = request.user
    conn.recipe_confirmed_at = timezone.now()
    conn.save()
    if conn.recipe_ai and conn.recipe_ai.status == "pending_review":
        conn.recipe_ai.status = "accepted"
        conn.recipe_ai.reviewed_by = request.user
        conn.recipe_ai.reviewed_at = timezone.now()
        conn.recipe_ai.save()
    return Response(P.connector(conn))


@api_view(["POST"])
def connector_read(request, connector_id):
    conn = get_object_or_404(Connector, pk=connector_id)
    job = ingestion_jobs.start_browser_job(conn, request.user)
    return Response(P.job(job), status=202)


@api_view(["POST"])
def upload(request, company_id):
    """One workbook with everything (field "file"), or a file per kind of data ("employees", "compensation", "leave")."""
    company = get_object_or_404(Company, pk=company_id)
    if not is_in_group(request.user, "maker"):
        raise RuleViolation("Only a maker can upload HR data.", code="forbidden")
    job = ingestion_jobs.run_spreadsheet_job(company, request.user, _uploaded_files(request))
    return Response(P.job(job, detail=True), status=201)


@api_view(["POST"])
def job_add_files(request, job_id):
    job = get_object_or_404(IngestionJob, pk=job_id)
    ingestion_jobs.add_spreadsheet_files(job, request.user, _uploaded_files(request))
    job.refresh_from_db()
    return Response(P.job(job, detail=True))


def _uploaded_files(request) -> list[tuple[str, bytes, str]]:
    # Form field -> kind of data in the file ("" = anything, e.g. one workbook with every sheet).
    fields = {"file": "", **{k: k for k in ingestion_jobs.UPLOAD_DATASETS if k}}
    unknown = set(request.FILES) - set(fields)
    if unknown:
        raise RuleViolation(f"Unknown upload field(s): {', '.join(sorted(unknown))}")
    files = []
    for field, dataset in fields.items():
        for f in request.FILES.getlist(field):
            if f.size > 10 * 1024 * 1024:
                raise RuleViolation(f"{f.name} is larger than 10 MB.")
            files.append((f.name, f.read(), dataset))
    if not files:
        raise RuleViolation("Choose a file to upload.")
    return files


@api_view(["GET"])
def jobs(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    return Response([P.job(ingestion_jobs.mark_if_interrupted(j)) for j in company.ingestion_jobs.order_by("-id")[:30]])


@api_view(["GET"])
def job_detail(request, job_id):
    job = ingestion_jobs.mark_if_interrupted(get_object_or_404(IngestionJob, pk=job_id))
    return Response(P.job(job, detail=True))


@api_view(["POST"])
def job_suggest(request, job_id):
    job = get_object_or_404(IngestionJob, pk=job_id)
    ingestion_jobs.suggest(job, request.user, force=bool(request.data.get("force")))
    job.refresh_from_db()
    return Response(P.job(job, detail=True))


@api_view(["POST"])
def table_confirm(request, table_id):
    table = get_object_or_404(RawTable, pk=table_id)
    ingestion_jobs.confirm(table, request.user, request.data.get("mapping") or {}, request.data.get("value_maps"),
                           request.data.get("options"))
    table.job.refresh_from_db()
    return Response(P.job(table.job, detail=True))


@api_view(["POST"])
def table_value_maps(request, table_id):
    """Preview which leave types/statuses a mapping would treat as unpaid/counted, before confirming."""
    table = get_object_or_404(RawTable, pk=table_id)
    return Response(default_value_maps(table.rows, request.data.get("mapping") or {}))


@api_view(["POST"])
def job_build_snapshot(request, job_id):
    job = get_object_or_404(IngestionJob, pk=job_id)
    if not is_in_group(request.user, "maker"):
        raise RuleViolation("Only a maker can do this.", code="forbidden")
    snap = snapshot_service.create_from_job(job, request.user)
    return Response(P.snapshot(snap), status=201)


@api_view(["GET"])
def mapping_catalogue(request):
    return Response({"targets": catalogue(), "groups": GROUP_LABELS})


# =========================================================================== snapshots


@api_view(["GET"])
def snapshots(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    return Response([P.snapshot_summary(s) for s in company.snapshots.order_by("-id")[:30]])


@api_view(["GET"])
def snapshot_detail(request, snapshot_id):
    return Response(P.snapshot(get_object_or_404(DataSnapshot, pk=snapshot_id)))


@api_view(["POST"])
def snapshot_approve(request, snapshot_id):
    snap = snapshot_service.approve(get_object_or_404(DataSnapshot, pk=snapshot_id), request.user, request.data.get("note", ""))
    return Response(P.snapshot(snap))


@api_view(["POST"])
def snapshot_reject(request, snapshot_id):
    snap = snapshot_service.reject(get_object_or_404(DataSnapshot, pk=snapshot_id), request.user, request.data.get("note", ""))
    return Response(P.snapshot(snap))


# =========================================================================== runs


@api_view(["GET", "POST"])
def runs(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    if request.method == "POST":
        snapshot = None
        if request.data.get("snapshot_id"):
            snapshot = get_object_or_404(DataSnapshot, pk=request.data["snapshot_id"])
        knowledge = request.data.get("knowledge_date")
        run = run_service.open_month(company, request.data.get("period", ""), request.user, snapshot=snapshot,
                                     knowledge_date=date.fromisoformat(knowledge) if knowledge else None,
                                     note=request.data.get("note", ""))
        dispatch.start(run)
        return Response(P.run_detail(run, request.user), status=201)
    qs = PayrollRun.objects.filter(company=company).order_by("-period", "-id")
    return Response([P.run_summary(r) for r in qs])


@api_view(["GET"])
def run_detail(request, run_id):
    run = get_object_or_404(PayrollRun.objects.select_related("company", "snapshot"), pk=run_id)
    return Response(P.run_detail(run, request.user, with_workflow=request.query_params.get("workflow") != "0"))


@api_view(["POST"])
def run_action(request, run_id, action):
    run = get_object_or_404(PayrollRun, pk=run_id)
    note = request.data.get("note", "") or ""
    if action == "review-data":
        run_service.review_data(run.pk, request.user)
        outcome = {"mode": "direct", "queued": False}
    elif action == "use-latest-data":
        latest = DataSnapshot.objects.filter(company=run.company, status="approved").order_by("-reviewed_at", "-id").first()
        if not latest:
            raise RuleViolation("No reviewed HR data.")
        run_service.use_snapshot(run.pk, latest, request.user)
        outcome = {"mode": "direct", "queued": False}
    elif action in ("calculate", "submit", "approve", "reject", "cancel"):
        if run.run_type == PayrollRun.TYPE_DRAFT:
            if action == "calculate":
                run_service.calculate(run.pk, request.user)
            elif action == "cancel":
                run_service.cancel(run.pk, request.user)
            else:
                raise RuleViolation("A draft re-run is a what-if; it cannot be submitted or approved.")
            outcome = {"mode": "direct", "queued": False}
        else:
            outcome = dispatch.send(run, action, request.user, note)
    else:
        raise Http404
    run.refresh_from_db()
    return Response({"outcome": outcome, "run": P.run_detail(run, request.user, with_workflow=False)})


@api_view(["POST"])
def run_verify(request, run_id):
    run = get_object_or_404(PayrollRun, pk=run_id)
    return Response(run_service.verify_repeatable(run))


@api_view(["POST"])
def run_draft(request, run_id):
    base = get_object_or_404(PayrollRun, pk=run_id)
    if not base.result:
        raise RuleViolation("Calculate the month first.")
    draft = run_service.create_draft_rerun(base, request.user, request.data.get("overrides") or {}, request.data.get("note", ""))
    return Response(P.run_detail(draft, request.user, with_workflow=False), status=201)


@api_view(["GET"])
def run_compare(request, run_id, other_id):
    a = get_object_or_404(PayrollRun, pk=other_id)
    b = get_object_or_404(PayrollRun, pk=run_id)
    if not a.result or not b.result:
        raise RuleViolation("Both runs must be calculated.")
    return Response(compare_results(a.result, b.result, P.run_label(a), P.run_label(b)))


@api_view(["POST"])
def run_explain(request, run_id):
    """AI writes a plain-words summary of the deterministic comparison. It changes nothing."""
    run = get_object_or_404(PayrollRun, pk=run_id)
    base = P.comparison_base(run)
    if not base or not run.result:
        raise RuleViolation("Nothing to compare with yet.")
    diff = compare_results(base.result, run.result, P.run_label(base), P.run_label(run))
    interaction = ai_tasks.summarize_changes(company=run.company, diff=diff, before_label=P.run_label(base), after_label=P.run_label(run),
                                             force=bool(request.data.get("force")))
    return Response(P.ai(interaction))


@api_view(["GET"])
def run_file(request, run_id, name):
    run = get_object_or_404(PayrollRun, pk=run_id)
    if name not in (run.files or {}).get("files", {}) and name != "manifest.json":
        raise Http404
    path = Path(settings.MEDIA_ROOT) / run.files["folder"] / name
    if not path.exists():
        raise Http404
    return FileResponse(open(path, "rb"), as_attachment=True, filename=f"{run.company.code}-{run.period}-{name}")


@api_view(["GET"])
def agent_media(request, path):
    """Screenshots taken by the browser agent (signed-in users only)."""
    root = (Path(settings.MEDIA_ROOT) / "agent").resolve()
    target = (Path(settings.MEDIA_ROOT) / path).resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise Http404
    return FileResponse(open(target, "rb"), content_type="image/png")


@api_view(["GET"])
def run_register_preview(request, run_id):
    run = get_object_or_404(PayrollRun, pk=run_id)
    if not run.result:
        raise Http404
    files = exports.build_files(run.result, run.input)
    name = request.query_params.get("file", "register.csv")
    if name not in files:
        raise Http404
    label = "DRAFT-" if run.status != PayrollRun.FINALIZED else ""
    resp = HttpResponse(files[name], content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{label}{run.company.code}-{run.period}-run{run.pk}-{name}"'
    return resp


# =========================================================================== instructions


@api_view(["POST"])
def run_instruction(request, run_id):
    run = get_object_or_404(PayrollRun, pk=run_id)
    change = change_service.propose(run, request.data.get("text", ""), request.user, force=bool(request.data.get("force")))
    return Response(P.change(change), status=201)


@api_view(["POST"])
def change_decide(request, change_id, decision):
    change = get_object_or_404(ChangeRequest, pk=change_id)
    if decision == "confirm":
        change = change_service.confirm(change, request.user)
    elif decision == "discard":
        change = change_service.reject(change, request.user)
    else:
        raise Http404
    return Response(P.change(change))


# =========================================================================== rules & settings


@api_view(["GET", "POST"])
def policies(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    if request.method == "POST":
        if not (is_in_group(request.user, "maker") or is_in_group(request.user, "checker")):
            raise RuleViolation("You cannot change rules.", code="forbidden")
        d = request.data
        try:
            start = date.fromisoformat(d.get("effective_from", ""))
        except ValueError as exc:
            raise RuleViolation("Give a start date like 2025-08-01.") from exc
        payload = d.get("payload")
        if isinstance(payload, str):
            import json
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise RuleViolation(f"The rule data is not valid JSON: {exc.msg} (line {exc.lineno}).") from exc
        version, warnings = policy_service.add_version(company=company, key=d.get("key", ""), jurisdiction=d.get("jurisdiction", ""),
                                                       effective_from=start, payload=payload, title=d.get("title", ""),
                                                       note=d.get("note", ""), user=request.user)
        return Response({"version": P.policy(version, set()), "warnings": warnings}, status=201)
    used = policy_service.used_version_ids(company)
    qs = PolicyVersion.objects.filter(Q(company__isnull=True) | Q(company=company)).order_by("key", "jurisdiction", "effective_from", "id")
    groups: dict[tuple, dict] = {}
    for v in qs:
        k = (v.scope, v.key, v.jurisdiction)
        g = groups.setdefault(k, {
            "scope": v.scope, "key": v.key, "jurisdiction": v.jurisdiction,
            "title": engine_policies.STATUTORY_KEYS.get(v.key) or engine_policies.COMPANY_KEYS.get(v.key, v.key),
            "versions": [],
        })
        g["versions"].append(P.policy(v, used))
    for key in engine_policies.COMPANY_KEYS:
        if (PolicyVersion.SCOPE_COMPANY, key, "") not in groups:
            groups[(PolicyVersion.SCOPE_COMPANY, key, "")] = {
                "scope": "company", "key": key, "jurisdiction": "", "title": engine_policies.COMPANY_KEYS[key], "versions": [],
                "default": engine_policies.COMPANY_DEFAULTS[key]}
    today = Period.of(timezone.localdate())
    for g in groups.values():
        current = None
        for v in g["versions"]:
            if v["effective_from"] <= today.start.isoformat():
                current = v["id"]
        g["current_version_id"] = current
    return Response({
        "groups": sorted(groups.values(), key=lambda g: (g["scope"] != "statutory", g["key"], g["jurisdiction"])),
        "templates": {k: engine_policies.COMPANY_DEFAULTS.get(k) for k in engine_policies.COMPANY_KEYS},
        "rule_as_of": "Rules and settings are picked as of the 1st day of each pay month.",
    })


@api_view(["DELETE"])
def policy_delete(request, version_id):
    version = get_object_or_404(PolicyVersion, pk=version_id)
    if not (is_in_group(request.user, "maker") or is_in_group(request.user, "checker")):
        raise RuleViolation("You cannot change rules.", code="forbidden")
    policy_service.delete_version(version)
    return Response(status=204)


# =========================================================================== AI log & tools


@api_view(["GET"])
def ai_log(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    qs = AIInteraction.objects.filter(Q(company=company) | Q(company__isnull=True)).order_by("-created_at")[:200]
    return Response({"describe": ai_client.describe(), "items": [P.ai(i, full=True) for i in qs]})


@api_view(["POST"])
def wage_code_simulator(request, company_id):
    """Stretch goal: Labour Codes 50% wage rule. Every number comes from the engine."""
    from payroll_engine.money import d, to_str

    company = get_object_or_404(Company, pk=company_id)
    data = request.data
    gross = d(data.get("monthly_gross", "60000"))
    basic_share = d(data.get("basic_share", "0.40"))
    hra_share = d(data.get("hra_share", "0.20"))
    basic = (gross * basic_share).quantize(d(1))
    hra = (gross * hra_share).quantize(d(1))
    special = gross - basic - hra
    versions = policy_service.versions_for(company)
    rule = next((v for v in sorted(versions, key=lambda v: v["effective_from"]) if v["key"] == "wage_definition"), None)
    after = Period.parse(data.get("after", "")) if data.get("after") else (
        Period.parse(rule["effective_from"][:7]).next() if rule else Period(2025, 12))
    before = after.prev()

    def run(components, period, pf_base):
        emp = {"code": "SIM", "name": data.get("name", "Nisha Verma"), "work_state": company.state, "gender": "F",
               "compensation": [{"effective_from": "2000-01-01", "components": {k: to_str(v) for k, v in components.items()}}],
               "unpaid_days": "0", "one_time": [], "ytd": {"months": []}, "arrears_basis": []}
        vs = list(versions) + [{"id": f"what-if:pf_policy:{pf_base}", "key": "pf_policy", "scope": "override", "jurisdiction": "",
                                "effective_from": period.start.isoformat(), "title": "PF base (simulation)",
                                "payload": engine_policies.COMPANY_DEFAULTS["pf_policy"] | {"wage_base": pf_base}}]
        r = calculate_employee(emp, period, vs, {"state": company.state})
        a = r["amounts"]
        pf_line = next(x for x in r["lines"] if x["code"] == "pf_employee")
        return {"pf_wage": a["pf_wage"], "pf_employee": a["pf_employee"], "pf_employer": a["pf_employer"], "net_pay": a["net_pay"],
                "employer_cost": a["employer_cost"], "pf_reason": pf_line["reason"], "pf_formula": pf_line["formula"]}

    current = {"basic": basic, "hra": hra, "special_allowance": special}
    half = (gross / 2).quantize(d(1))
    restructured = {"basic": half, "hra": hra, "special_allowance": gross - half - hra}
    options = [
        {"title": "Keep the pay structure (rule adds the excess back to PF wages)", "components": current, "pf_base": "full"},
        {"title": "Raise basic to 50% of pay, take it from special allowance", "components": restructured, "pf_base": "full"},
        {"title": "Keep the structure, work PF on the first ₹15,000 only", "components": current, "pf_base": "capped"},
    ]
    out = []
    for o in options:
        out.append({"title": o["title"], "components": {k: to_str(v) for k, v in o["components"].items()}, "pf_base": o["pf_base"],
                    "before": run(o["components"], before, o["pf_base"]), "after": run(o["components"], after, o["pf_base"])})
    return Response({"employee": data.get("name", "Nisha Verma"), "monthly_gross": to_str(gross), "before": str(before),
                     "after": str(after), "before_label": before.label, "after_label": after.label,
                     "rule": rule and {"id": rule["id"], "effective_from": rule["effective_from"], "title": rule["title"]},
                     "options": out})

