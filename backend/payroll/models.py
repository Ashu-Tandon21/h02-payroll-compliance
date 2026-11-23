"""Database models.

The calculation itself lives in ``payroll_engine`` and never touches these models. These
models store: what was read from HR tools (snapshots), the dated rules and settings, the
payroll months with their frozen inputs and results, AI answers, and an audit trail.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from payroll_engine.periods import Period


class Company(models.Model):
    code = models.SlugField(unique=True)
    name = models.CharField(max_length=200)
    state = models.CharField(max_length=2, help_text="Two-letter state code of the main office, e.g. KA")
    city = models.CharField(max_length=100, blank=True)
    # Whether HR data of this company may be sent to an AI model. "off": only HR tool profiles, the
    # rule-based matcher and people map the data; nothing leaves the servers.
    AI_ALLOWED, AI_OFF = "allowed", "off"
    ai_mode = models.CharField(max_length=10, default=AI_ALLOWED,
                               choices=[(AI_ALLOWED, "AI may be used"), (AI_OFF, "No AI: profiles, rules and people only")])
    ai_mode_changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    ai_mode_changed_at = models.DateTimeField(null=True, blank=True)
    # Registration numbers used when paying the government (shown with the dues and on the files).
    pan = models.CharField(max_length=10, blank=True)
    tan = models.CharField(max_length=10, blank=True, help_text="Tax deduction account number, needed to deposit TDS")
    pf_establishment_code = models.CharField(max_length=30, blank=True)
    esi_employer_code = models.CharField(max_length=30, blank=True)
    pay_day = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Day of the next month salaries are paid, e.g. 1")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "companies"

    def __str__(self):
        return self.name


# --------------------------------------------------------------------------- rules & settings

class PolicyVersion(models.Model):
    """One version of a government rule (company is null) or a company setting.

    Versions are append-only. To change a rule, add a version with a later start date.
    A version that a paid month has used can never be changed or deleted.
    """

    SCOPE_STATUTORY = "statutory"
    SCOPE_COMPANY = "company"

    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="policy_versions")
    scope = models.CharField(max_length=20, choices=[(SCOPE_STATUTORY, "Government rule"), (SCOPE_COMPANY, "Company setting")])
    key = models.CharField(max_length=50)
    jurisdiction = models.CharField(max_length=10, blank=True, help_text="IN for national rules, IN-KA for Karnataka, blank for company settings")
    effective_from = models.DateField()
    payload = models.JSONField()
    title = models.CharField(max_length=200, blank=True)
    note = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["key", "jurisdiction", "effective_from", "id"]
        indexes = [models.Index(fields=["key", "jurisdiction", "effective_from"])]

    def save(self, *args, **kwargs):
        if self.pk and PolicyVersion.objects.filter(pk=self.pk).exists():
            raise ValueError("Rule and setting versions cannot be edited. Add a new version with a start date instead.")
        super().save(*args, **kwargs)

    def as_engine_dict(self) -> dict:
        return {
            "id": str(self.pk),
            "key": self.key,
            "scope": self.scope,
            "jurisdiction": self.jurisdiction,
            "effective_from": self.effective_from.isoformat(),
            "payload": self.payload,
            "title": self.title,
            "recorded_at": self.created_at.isoformat() if self.created_at else "",
        }

    def __str__(self):
        return f"{self.key} {self.jurisdiction} from {self.effective_from}"


# --------------------------------------------------------------------------- people

class Employee(models.Model):
    """Employee master, refreshed from each approved snapshot. Used to look up names."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="employees")
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=200)
    work_state = models.CharField(max_length=2, blank=True)
    gender = models.CharField(max_length=1, blank=True)
    date_of_joining = models.DateField(null=True, blank=True)
    date_of_exit = models.DateField(null=True, blank=True)
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("company", "code")]
        ordering = ["code"]

    def __str__(self):
        return f"{self.name} ({self.code})"


# --------------------------------------------------------------------------- AI answers

class AIInteraction(models.Model):
    """Every AI answer is stored here before anything uses it, and a person reviews it."""

    PURPOSES = [
        ("field_mapping", "Suggest which HR field means what"),
        ("page_discovery", "Find the HR tool's screens"),
        ("screen_extraction", "Read records from an HR screen"),
        ("instruction", "Turn an instruction into a proposed change"),
        ("change_summary", "Explain what changed since last month"),
    ]
    STATUS = [
        ("pending_review", "Waiting for a person"),
        ("accepted", "Accepted by a person"),
        ("rejected", "Rejected by a person"),
        ("informational", "Shown to a person; affects no number"),
        ("error", "Failed"),
    ]
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="ai_interactions")
    purpose = models.CharField(max_length=30, choices=PURPOSES)
    provider = models.CharField(max_length=20, help_text="anthropic or rule-based")
    model = models.CharField(max_length=60, blank=True)
    input_hash = models.CharField(max_length=64, db_index=True)
    request = models.JSONField()
    response = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="pending_review")
    validation = models.JSONField(default=list, blank=True, help_text="Deterministic checks run on the AI answer")
    error = models.TextField(blank=True)
    latency_ms = models.IntegerField(default=0)
    usage = models.JSONField(default=dict, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reuse_count = models.IntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]


# --------------------------------------------------------------------------- data in

class Connector(models.Model):
    """A source of HR data: an HR tool read through its screens, or spreadsheet uploads."""

    KIND_BROWSER = "browser"
    KIND_SPREADSHEET = "spreadsheet"
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="connectors")
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=20, choices=[(KIND_BROWSER, "HR tool screens"), (KIND_SPREADSHEET, "Spreadsheet upload")])
    system_name = models.CharField(max_length=100, help_text="e.g. Horilla, Demo HRMS, Spreadsheet")
    profile = models.CharField(max_length=50, blank=True, help_text="Known HR tool (integrations/<profile>/profile.json), if any")
    base_url = models.URLField(blank=True)
    username = models.CharField(max_length=200, blank=True)
    password = models.CharField(max_length=200, blank=True, help_text="Write-only through the API")
    # Pages to read, learned once (by the AI or a person) and confirmed by a person:
    # {"login_url": "...", "datasets": {"employees": {"url": "...", "follow_detail_links": false}, ...}}
    recipe = models.JSONField(default=dict, blank=True)
    recipe_confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    recipe_confirmed_at = models.DateTimeField(null=True, blank=True)
    recipe_ai = models.ForeignKey(AIInteraction, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.system_name})"


class IngestionJob(models.Model):
    STATUS = [
        ("queued", "Queued"),
        ("running", "Reading"),
        ("needs_mapping", "Waiting for field mapping"),
        ("ready", "Ready to build a snapshot"),
        ("done", "Snapshot created"),
        ("failed", "Failed"),
    ]
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="ingestion_jobs")
    connector = models.ForeignKey(Connector, null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs")
    kind = models.CharField(max_length=20)
    source_system = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS, default="queued")
    file = models.FileField(upload_to="uploads/", blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    log = models.JSONField(default=list, blank=True)
    stats = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class RawTable(models.Model):
    """One table exactly as read: a spreadsheet sheet or an HR screen (all its pages)."""

    job = models.ForeignKey(IngestionJob, on_delete=models.CASCADE, related_name="tables")
    name = models.CharField(max_length=200)
    dataset_hint = models.CharField(max_length=50, blank=True)
    source_url = models.CharField(max_length=500, blank=True)
    headers = models.JSONField(default=list)
    rows = models.JSONField(default=list)
    fingerprint = models.CharField(max_length=64)
    read_info = models.JSONField(default=dict, blank=True, help_text="pages read, counts shown on screen, method used")
    mapping = models.ForeignKey("FieldMapping", null=True, blank=True, on_delete=models.SET_NULL, related_name="tables")

    class Meta:
        ordering = ["id"]


class FieldMapping(models.Model):
    """Which source column means which payroll field. Confirmed once by a person, then reused."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="field_mappings")
    source_system = models.CharField(max_length=100)
    table_name = models.CharField(max_length=200)
    fingerprint = models.CharField(max_length=64, db_index=True)
    headers = models.JSONField(default=list)
    mapping = models.JSONField(default=dict, help_text="{source column: target field}")
    value_maps = models.JSONField(default=dict, blank=True, help_text="e.g. which leave types are unpaid")
    options = models.JSONField(default=dict, blank=True, help_text="e.g. date order DMY")
    suggested_by = models.ForeignKey(AIInteraction, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-confirmed_at", "-id"]


class DataSnapshot(models.Model):
    """HR data in one standard shape, whatever it came from. Immutable once created."""

    STATUS = [
        ("pending_review", "Waiting for the maker's review"),
        ("approved", "Reviewed by the maker"),
        ("rejected", "Rejected"),
    ]
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="snapshots")
    job = models.ForeignKey(IngestionJob, null=True, blank=True, on_delete=models.SET_NULL, related_name="snapshots")
    source_kind = models.CharField(max_length=20)
    source_system = models.CharField(max_length=100)
    data = models.JSONField()
    content_hash = models.CharField(max_length=64)
    checks = models.JSONField(default=list)
    status = models.CharField(max_length=20, choices=STATUS, default="pending_review")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def has_blocking_issues(self) -> bool:
        return any(c.get("level") == "error" for c in self.checks)


# --------------------------------------------------------------------------- payroll months

class PayrollRun(models.Model):
    """One calculation of one month. A month can have one live regular run and any number of drafts."""

    TYPE_REGULAR = "regular"
    TYPE_DRAFT = "draft"

    DRAFT = "draft"
    CALCULATED = "calculated"
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"
    APPROVED = "approved"
    FINALIZED = "finalized"
    CANCELLED = "cancelled"
    STATUS = [
        (DRAFT, "Being prepared"),
        (CALCULATED, "Calculated"),
        (AWAITING_APPROVAL, "Waiting for approval"),
        (REJECTED, "Sent back"),
        (APPROVED, "Approved"),
        (FINALIZED, "Final"),
        (CANCELLED, "Cancelled"),
    ]
    LIVE_STATUSES = [DRAFT, CALCULATED, AWAITING_APPROVAL, REJECTED, APPROVED, FINALIZED]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="runs")
    period = models.CharField(max_length=7)
    run_type = models.CharField(max_length=10, default=TYPE_REGULAR)
    status = models.CharField(max_length=20, choices=STATUS, default=DRAFT)
    snapshot = models.ForeignKey(DataSnapshot, on_delete=models.PROTECT, related_name="runs")
    knowledge_date = models.DateField(help_text="Salary changes recorded after this date are not used (lets past months be re-created as they were known)")
    base_run = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="draft_reruns")
    policy_overrides = models.JSONField(default=dict, blank=True, help_text="What-if setting values, drafts only")
    day_count = models.PositiveSmallIntegerField(null=True, blank=True,
                                                 help_text="Days one month's salary is divided by for one day's pay, set for this month; empty: the company setting")
    note = models.TextField(blank=True)

    data_reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    data_reviewed_at = models.DateTimeField(null=True, blank=True)

    input = models.JSONField(null=True, blank=True)
    input_hash = models.CharField(max_length=64, blank=True)
    result = models.JSONField(null=True, blank=True)
    result_hash = models.CharField(max_length=64, blank=True)
    calculated_at = models.DateTimeField(null=True, blank=True)
    calculated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    calculation_count = models.IntegerField(default=0)

    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_result_hash = models.CharField(max_length=64, blank=True)
    submitted_result = models.JSONField(null=True, blank=True, help_text="Exactly what the checker is asked to approve")

    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    files = models.JSONField(default=dict, blank=True)

    workflow_backend = models.CharField(max_length=20, blank=True)
    workflow_id = models.CharField(max_length=200, blank=True)
    last_error = models.TextField(blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-period", "-id"]
        constraints = [
            # A month can be final only once.
            models.UniqueConstraint(
                fields=["company", "period"],
                condition=Q(run_type="regular", status="finalized"),
                name="one_final_run_per_month",
            ),
            # Only one regular run of a month is open at a time.
            models.UniqueConstraint(
                fields=["company", "period"],
                condition=Q(run_type="regular") & ~Q(status__in=["cancelled"]),
                name="one_live_regular_run_per_month",
            ),
        ]

    @property
    def period_obj(self) -> Period:
        return Period.parse(self.period)

    @property
    def is_locked(self) -> bool:
        return self.status in (self.AWAITING_APPROVAL, self.APPROVED, self.FINALIZED, self.CANCELLED)

    def __str__(self):
        return f"{self.company.code} {self.period} #{self.pk} ({self.status})"


class ChangeRequest(models.Model):
    """A change typed in plain English. The AI proposes; a person confirms; the engine calculates."""

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="change_requests")
    period = models.CharField(max_length=7)
    run = models.ForeignKey(PayrollRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="change_requests")
    text = models.TextField()
    ai = models.ForeignKey(AIInteraction, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    proposal = models.JSONField(default=dict, help_text="What the AI understood, after deterministic checks")
    status = models.CharField(max_length=20, default=PROPOSED)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]


class RunEvent(models.Model):
    """Append-only audit trail of everything that happened to a month."""

    run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(auto_now_add=True)
    actor = models.CharField(max_length=150)
    action = models.CharField(max_length=50)
    message = models.TextField()
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["at", "id"]


# --------------------------------------------------------------------------- after a month is final

class StatutoryDue(models.Model):
    """Money the company must deposit with the government for a final month: TDS, PF, ESI, professional tax.

    Created when the month is finalised, from its result; the amounts never change afterwards. Only the
    payment (date and reference) is recorded later. Whether it is due, overdue or paid is worked out from
    the dates, never stored."""

    TDS, PF, ESI, PT = "tds", "pf", "esi", "pt"
    KINDS = [(TDS, "TDS (income tax)"), (PF, "Provident Fund"), (ESI, "ESI"), (PT, "Professional tax")]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="dues")
    run = models.ForeignKey(PayrollRun, on_delete=models.PROTECT, related_name="dues")
    period = models.CharField(max_length=7)
    kind = models.CharField(max_length=5, choices=KINDS)
    jurisdiction = models.CharField(max_length=10, blank=True, help_text="State code for professional tax, e.g. KA")
    employee_amount = models.DecimalField(max_digits=14, decimal_places=2)
    employer_amount = models.DecimalField(max_digits=14, decimal_places=2)
    total = models.DecimalField(max_digits=14, decimal_places=2)
    people = models.PositiveIntegerField(default=0)
    due_date = models.DateField()
    paid_on = models.DateField(null=True, blank=True)
    reference = models.CharField(max_length=60, blank=True, help_text="Challan, TRRN or CRN number of the payment")
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    paid_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["due_date", "period", "kind", "jurisdiction"]
        constraints = [models.UniqueConstraint(fields=["run", "kind", "jurisdiction"], name="one_due_per_kind_per_month")]


class OpeningBalance(models.Model):
    """Pay and TDS of an employee earlier in the financial year, before the company used this system.
    TDS for the rest of the year is spread from these, as if those months had been paid here."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="opening_balances")
    fy_start = models.DateField(help_text="1 April of the financial year")
    employee_code = models.CharField(max_length=50)
    gross = models.DecimalField(max_digits=14, decimal_places=2)
    tds = models.DecimalField(max_digits=14, decimal_places=2)
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    entered_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["employee_code"]
        constraints = [models.UniqueConstraint(fields=["company", "fy_start", "employee_code"], name="one_opening_balance_per_person")]


class AbsenceDismissal(models.Model):
    """A day suggested as a possible unpaid absence (no check-in, no approved leave) that a maker said was not one,
    e.g. a forgotten punch or a day at a client's site. Kept so it is not suggested again, with who and why."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="absence_dismissals")
    period = models.CharField(max_length=7)
    employee_code = models.CharField(max_length=50)
    dates = models.JSONField(default=list)
    reason = models.TextField()
    dismissed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["period", "employee_code", "id"]
