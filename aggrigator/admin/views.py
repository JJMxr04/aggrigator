"""SQLAdmin model views — one per table, read-mostly except for moderation
edits. Mounted at /admin in main.py."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from markupsafe import Markup
from sqladmin import Admin, BaseView, ModelView, action, expose
from sqladmin.filters import (
    AllUniqueStringValuesFilter,
    BooleanFilter,
    ForeignKeyFilter,
    OperationColumnFilter,
    StaticValuesFilter,
)
from sqladmin.flash import Flash
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from wtforms import StringField
from wtforms.widgets import TextInput

_TEMPLATES_DIR = str(Path(__file__).parent / "templates")


class _RelativeRedirectAdmin(Admin):
    """Admin subclass that returns path-only post-save redirect URLs.

    Behind Cloudflare Tunnel the origin app runs over plain HTTP, so
    ``request.url_for(...)`` can emit a redirect Location that the
    browser sees as a different origin from the document — which trips
    ``Content-Security-Policy: form-action 'self'`` enforced on the
    redirect target. Returning a path drops scheme+host entirely so the
    browser resolves the redirect against the request origin, keeping
    the whole submit→302→GET chain inside ``'self'``."""

    def get_save_redirect_url(
        self, request: Request, form: dict, obj: Any, model_view: ModelView,
    ) -> str:
        url = super().get_save_redirect_url(
            request=request, form=form, obj=obj, model_view=model_view,
        )
        return url.path if hasattr(url, "path") else str(url)

from aggrigator.admin.navigation import NavigableModelView
from aggrigator.admin.summaries import EventsByLeagueSummary, TeamsByLeagueSummary
from aggrigator.config import get_settings
from aggrigator.db import async_session_factory, engine
from aggrigator.schemas.team import team_logo_url
from aggrigator.webhooks.enqueue import force_enqueue_for_event
from aggrigator.webhooks.notify import notify_webhook_worker


# ---- click-to-reveal helpers (mirrors MDProject's UserAdmin) ---------------
#
# SQLAdmin escapes column output unless the formatter returns Markup. We
# wrap each sensitive field in a <details><summary> so the masked value
# is the default and the operator has to click Show to expose it. The
# "copy" button writes the full value to clipboard so the operator can
# paste it elsewhere without re-revealing it on every refresh.

_COPY_JS = (
    "navigator.clipboard.writeText(this.previousElementSibling.textContent);"
    "this.textContent='copied';"
    "setTimeout(()=>this.textContent='copy',1200);"
)


def _reveal(masked: str, full: str) -> Markup:
    if not full:
        return Markup('—')
    return Markup(
        '<details style="display:inline-block">'
        f'<summary style="cursor:pointer;list-style:none">'
        f'<code>{masked}</code> <span style="opacity:.6">▸</span></summary>'
        '<div style="margin-top:4px">'
        f'<code>{full}</code> '
        f'<button type="button" style="font-size:11px;padding:1px 6px" '
        f'onclick="{_COPY_JS}">copy</button>'
        '</div></details>'
    )


def _mask_tail(value: str, keep: int = 4) -> str:
    if not value:
        return ''
    if len(value) <= keep:
        return '•' * len(value)
    return '•' * (len(value) - keep) + value[-keep:]


def _mask_email(value: str) -> str:
    if not value or '@' not in value:
        return _mask_tail(value or '')
    local, _, domain = value.partition('@')
    head = local[0] if local else ''
    return f'{head}•••@{domain}'


def _mask_uuid(value) -> str:
    if not value:
        return ''
    s = str(value)
    return f'…{s[-12:]}'
from aggrigator.models import (
    AuditLog,
    Bookmaker,
    BookmakerSelection,
    CronRun,
    Event,
    League,
    Market,
    OddsQuote,
    RefreshToken,
    Selection,
    Sport,
    Team,
    TenantApiKey,
    TenantUser,
    User,
    WebhookDelivery,
)
from aggrigator.models.auth import UserRole
from aggrigator.models.cron_run import CronRunStatus
from aggrigator.models.selection import (
    SelectionType,
    SettlementSource,
    SettlementStatus,
)
from aggrigator.models.tenant import TenantStatus, TenantTier


def _enum_choices(enum_cls) -> list[tuple[str, str]]:
    """Build SQLAdmin StaticValuesFilter ``(value, label)`` pairs from a
    StrEnum. Value is what's stored in the column; label is the member value."""
    return [(member.value, member.value) for member in enum_cls]


# ---- view classes ----------------------------------------------------------


class UserView(NavigableModelView, ModelView, model=User):
    column_list = ["id", "email", "role", "is_active", "created"]
    column_searchable_list = ["email"]
    column_sortable_list = ["created", "email", "role"]
    column_default_sort = [("created", True)]
    column_filters = [
        StaticValuesFilter(
            User.role,
            values=[(UserRole.ADMIN, UserRole.ADMIN), (UserRole.USER, UserRole.USER)],
        ),
        BooleanFilter(User.is_active),
    ]
    form_excluded_columns = ["password_hash"]
    icon = "fa-solid fa-user"


class TenantUserView(ModelView, model=TenantUser):
    # The MDProject-mirrored user. Tier/status are written by signed
    # /v1/internal/* calls from MDProject — editing here would desync
    # the two sides, so this view is read-only.
    name = "Tenant user"
    name_plural = "Tenant users (MDProject)"
    icon = "fa-solid fa-users"
    column_list = [
        "email", "external_user_id", "tier", "status",
        "features", "revoked_at", "created",
    ]
    column_searchable_list = ["email", "external_user_id"]
    column_sortable_list = ["created", "email", "tier", "status"]
    column_default_sort = [("created", True)]
    column_filters = [
        StaticValuesFilter(
            TenantUser.tier,
            values=[(TenantTier.FREE, TenantTier.FREE), (TenantTier.PRO, TenantTier.PRO)],
        ),
        StaticValuesFilter(
            TenantUser.status,
            values=[
                (TenantStatus.ACTIVE, TenantStatus.ACTIVE),
                (TenantStatus.TRIALING, TenantStatus.TRIALING),
                (TenantStatus.PAST_DUE, TenantStatus.PAST_DUE),
                (TenantStatus.UNPAID, TenantStatus.UNPAID),
                (TenantStatus.CANCELED, TenantStatus.CANCELED),
                (TenantStatus.INCOMPLETE, TenantStatus.INCOMPLETE),
                (TenantStatus.INCOMPLETE_EXPIRED, TenantStatus.INCOMPLETE_EXPIRED),
            ],
        ),
    ]
    column_formatters = {
        "email": lambda m, a: _reveal(_mask_email(m.email), m.email),
        "external_user_id": lambda m, a: _reveal(
            _mask_uuid(m.external_user_id), str(m.external_user_id)
        ),
    }
    # Apply on detail page too — same masking, same reveal.
    column_formatters_detail = column_formatters
    can_create = False
    can_edit = False
    can_delete = False


class TenantApiKeyView(ModelView, model=TenantApiKey):
    name = "Tenant API key"
    name_plural = "Tenant API keys"
    icon = "fa-solid fa-key"
    column_list = [
        "tenant_user_id", "prefix", "last_four",
        "revoked_at", "last_used_at", "created",
    ]
    column_searchable_list = ["prefix", "tenant_user_id"]
    column_sortable_list = ["created", "last_used_at", "revoked_at"]
    column_default_sort = [("created", True)]
    column_filters = [OperationColumnFilter(TenantApiKey.last_used_at)]
    column_formatters = {
        "tenant_user_id": lambda m, a: _reveal(
            _mask_uuid(m.tenant_user_id), str(m.tenant_user_id)
        ),
        # The prefix is the public lookup half of the key, but anyone
        # holding the prefix + the unhashed tail can authenticate, so we
        # mask it like the full key.
        "prefix": lambda m, a: _reveal(_mask_tail(m.prefix), m.prefix),
        # 4 chars is too short for a useful mask; hide entirely behind
        # the toggle (no preview).
        "last_four": lambda m, a: _reveal('••••', m.last_four),
    }
    column_formatters_detail = column_formatters
    can_create = False
    can_edit = False
    can_delete = False


class RefreshTokenView(ModelView, model=RefreshToken):
    column_list = ["id", "user_id", "expires_at", "revoked_at", "created_at"]
    column_searchable_list = ["user_id"]
    column_sortable_list = ["expires_at", "created_at", "revoked_at"]
    column_default_sort = [("created_at", True)]
    column_filters = [
        OperationColumnFilter(RefreshToken.expires_at),
        OperationColumnFilter(RefreshToken.revoked_at),
    ]
    can_create = False
    can_edit = False


class WebhookDeliveryView(ModelView, model=WebhookDelivery):
    column_list = [
        "id", "event_id", "event_name",
        "attempts", "last_status", "delivered_at", "next_retry_at", "created_at",
    ]
    column_searchable_list = ["event_id", "event_name"]
    column_sortable_list = ["created_at", "attempts", "last_status", "delivered_at"]
    column_default_sort = [("created_at", True)]
    column_filters = [
        AllUniqueStringValuesFilter(WebhookDelivery.last_status),
        OperationColumnFilter(WebhookDelivery.attempts),
        OperationColumnFilter(WebhookDelivery.created_at),
    ]
    can_create = False
    can_edit = False


class AuditLogView(ModelView, model=AuditLog):
    column_list = [
        "id", "event_name", "actor_user_id", "target_type", "target_id",
        "ip", "created_at",
    ]
    column_searchable_list = ["event_name", "target_id", "actor_user_id"]
    column_sortable_list = ["created_at"]
    column_default_sort = [("created_at", True)]
    column_filters = [
        AllUniqueStringValuesFilter(AuditLog.event_name),
        AllUniqueStringValuesFilter(AuditLog.target_type),
        OperationColumnFilter(AuditLog.created_at),
    ]
    can_create = False
    can_edit = False
    can_delete = False  # audit log is append-only


class SportView(NavigableModelView, ModelView, model=Sport):
    column_list = [
        "id", "name", "active",
        "odds_api_io_key", "thesportsdb_id",
    ]
    column_searchable_list = ["name", "id"]
    column_sortable_list = ["name", "active", "id"]
    column_default_sort = [("name", False)]
    column_filters = [BooleanFilter(Sport.active)]
    # Provider keys are owned by the registry loader — edit the JSON
    # under aggrigator/data/sports/ instead, then re-run load_registry.
    form_excluded_columns = ["odds_api_io_key", "thesportsdb_id"]


class LeagueView(NavigableModelView, ModelView, model=League):
    column_list = [
        "id", "sport_id", "name", "active",
        "odds_api_io_key", "thesportsdb_id",
        "can_pull_historical_scores", "last_refreshed_at",
    ]
    column_searchable_list = ["name", "id"]
    column_sortable_list = [
        "id", "sport_id", "active", "can_pull_historical_scores",
        "last_refreshed_at",
    ]
    column_default_sort = [("sport_id", False), ("name", False)]
    column_filters = [
        ForeignKeyFilter(League.sport_id, Sport.name, foreign_model=Sport),
        BooleanFilter(League.active),
        BooleanFilter(League.can_pull_historical_scores),
    ]
    # Registry-owned + derived columns — read-only via the admin form.
    # can_pull_historical_scores is recomputed by load_registry, never
    # set manually. The two *_key columns come from the on-disk JSON.
    form_excluded_columns = [
        "odds_api_io_key", "thesportsdb_id", "can_pull_historical_scores",
    ]


def _team_logo_thumb(model, _attr) -> Markup:
    """Render a 24px logo thumbnail pointing at the keyless logo endpoint.

    ``public_base_url`` empty -> a relative ``/v1/teams/{id}/logo`` URL,
    which still resolves because the admin is same-origin with the
    aggregator. ``team_logo_url`` is the same builder the /v1/events
    serializer uses, so the admin preview matches the API payload."""
    src = team_logo_url(model.id, public_base=get_settings().public_base_url)
    return Markup('<img src="{}" height="24" alt="{} logo">').format(src, model.id)


_SIX_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class ColorPickerWidget(TextInput):
    """Render a String color column as a native color swatch wired to a
    text input. The text input stays the real form field, so empty values
    and 8-digit alpha hex (which ``<input type=color>`` can't represent)
    round-trip untouched; the swatch is a convenience that writes 6-digit
    hex into the text box on pick and mirrors the text value back when it
    parses as ``#RRGGBB``. Inline handlers are safe here — the admin CSP
    intentionally leaves ``script-src`` unset for SQLAdmin (see main.py)."""

    def __call__(self, field, **kwargs):
        value = field.data or ""
        # Native pickers reject anything but #RRGGBB; fall back to black so
        # the swatch still renders for empty/alpha values without clobbering
        # the text field that actually gets submitted.
        swatch = value if _SIX_HEX.match(value) else "#000000"
        swatch_id = f"{field.id}__swatch"
        kwargs.setdefault(
            "oninput",
            f"document.getElementById('{swatch_id}').value="
            r"/^#[0-9a-fA-F]{6}$/.test(this.value)?this.value:'#000000';",
        )
        text_html = super().__call__(field, **kwargs)
        picker_html = Markup(
            '<input type="color" id="{}" value="{}" '
            'style="vertical-align:middle;margin-right:.4rem;width:2.4rem;'
            'height:2rem;padding:0;border:1px solid #ccc;border-radius:4px" '
            "oninput=\"document.getElementById('{}').value=this.value\">"
        ).format(swatch_id, swatch, field.id)
        return Markup(
            '<span style="display:inline-flex;align-items:center">{}{}</span>'
        ).format(picker_html, text_html)


class ColorField(StringField):
    widget = ColorPickerWidget()


class TeamView(NavigableModelView, ModelView, model=Team):
    column_list = [
        "logo",
        "league_id", "canonical_name",
        "odds_api_io_key", "thesportsdb_team_id",
        "match_confirmed", "match_source",
        "team_id",
    ]
    column_formatters = {
        "logo": _team_logo_thumb,
    }
    # On the detail page the synthetic "logo" column isn't in the model's
    # default details list, so render the thumbnail over the real (dormant)
    # ``logo_url`` column instead — the formatter ignores the column value
    # and builds the <img> from ``model.id``.
    column_formatters_detail = {
        "logo_url": _team_logo_thumb,
    }
    column_searchable_list = [
        "canonical_name", "name_long", "team_id",
        "odds_api_io_key", "thesportsdb_team_id",
    ]
    column_sortable_list = [
        "league_id", "canonical_name", "match_confirmed", "match_source",
    ]
    # Float unconfirmed rows to the top so the operator's review queue is
    # the default view.
    column_default_sort = [("match_confirmed", False), ("league_id", False)]
    column_filters = [
        ForeignKeyFilter(Team.league_id, League.name, foreign_model=League),
        BooleanFilter(Team.match_confirmed),
        AllUniqueStringValuesFilter(Team.match_source),
    ]
    # canonical_name and the two provider keys come from the JSON
    # registry — editing them here would silently drift from disk. Leave
    # match_confirmed editable so the operator can toggle it from the
    # detail page; load_registry preserves the existing flag on re-load
    # via the (league_id, canonical_name) match.
    form_excluded_columns = [
        "canonical_name", "odds_api_io_key", "thesportsdb_team_id",
        "match_source", "id", "public_id", "team_id",
        # Dormant/computed — the logo URL is synthesized at serialization
        # time from the keyless endpoint, never stored, so an editable text
        # field here would be misleading. The detail view shows it as a
        # thumbnail instead (column_formatters_detail above).
        "logo_url",
    ]
    # Render the hex color columns as a swatch picker + text input rather
    # than a bare text box. ColorField keeps the text as the submitted
    # value so empty and 8-digit alpha hex still round-trip.
    form_overrides = {
        "primary_color": ColorField,
        "secondary_color": ColorField,
        "primary_contrast": ColorField,
        "secondary_contrast": ColorField,
    }


def _provider_badge(_model, attr) -> Markup:
    """Color-coded provider chip so historical vs live is unmistakable
    in any list/detail view."""
    value = getattr(_model, attr, None) or ""
    if value == "odds_api_io":
        bg, fg, label = "#dceaff", "#0a64ff", "LIVE (odds-api.io)"
    elif value == "thesportsdb":
        bg, fg, label = "#fef0c7", "#92400e", "HIST (TheSportsDB)"
    else:
        bg, fg, label = "#eee", "#555", value or "—"
    return Markup(
        f'<span style="background:{bg};color:{fg};padding:2px 8px;'
        f'border-radius:4px;font-size:.78rem;font-weight:600;'
        f'font-family:ui-monospace,monospace">{label}</span>'
    )


# TheSportsDB event ids are prefixed ``tsdb:`` (e.g.
# ``tsdb:2069556``); odds-api.io ids are unprefixed (typically numeric
# like ``71173724``). Sniff the provider off the prefix without
# crossing the ``Market.event`` relationship — that relationship is
# configured ``lazy='raise'`` and we don't want to widen the lazy-load
# contract for an admin badge.


def _market_event_provider_badge(model, _attr) -> Markup:
    """Render a provider badge for a Market by inferring its parent
    Event's provider from the event_id prefix."""
    ev_id = getattr(model, "event_id", "") or ""
    inferred = "thesportsdb" if ev_id.startswith("tsdb:") else "odds_api_io"
    # Fake the model shape ``_provider_badge`` expects.
    fake = type("_Stub", (), {"provider": inferred})()
    return _provider_badge(fake, "provider")


class EventView(NavigableModelView, ModelView, model=Event):
    column_list = [
        "provider", "id", "linked_event_id", "league_id",
        "status_type", "start_time",
        "home_team_id", "away_team_id", "home_score", "away_score",
    ]
    column_searchable_list = ["id", "provider", "linked_event_id"]
    column_sortable_list = ["provider", "start_time", "status_type", "league_id"]
    # Group by provider then sort by start_time so live + historical rows
    # for the same date sit visually adjacent.
    column_default_sort = [("provider", False), ("start_time", True)]
    column_filters = [
        ForeignKeyFilter(Event.league_id, League.name, foreign_model=League),
        AllUniqueStringValuesFilter(Event.status_type),
        StaticValuesFilter(
            Event.provider,
            values=[("odds_api_io", "odds-api.io"), ("thesportsdb", "TheSportsDB")],
        ),
        OperationColumnFilter(Event.start_time),
    ]
    column_formatters = {
        "provider": _provider_badge,
    }
    column_formatters_detail = column_formatters
    # Provider + linked_event_id are set at ingest-time; not operator-editable.
    form_excluded_columns = ["provider", "linked_event_id"]

    @action(
        name="re-enqueue-webhook",
        label="Re-enqueue webhook",
        confirmation_message=(
            "Force a fresh webhook delivery for the selected event(s)? "
            "MDProject will re-apply the current event/market/selection "
            "state — useful when the original delivery never arrived."
        ),
    )
    async def re_enqueue_webhook(self, request: Request) -> Response:
        """Force-enqueue a webhook for each selected event, then push the
        delivery worker. Mirrors the orchestrator's enqueue path with a
        unique idempotency-key suffix so MDProject doesn't 409-dedup it."""
        pks_param = request.query_params.get("pks", "") or ""
        event_ids = [p for p in pks_param.split(",") if p]

        enqueued = 0
        skipped = 0
        errors: list[str] = []

        if event_ids:
            async with async_session_factory() as session:
                for event_id in event_ids:
                    event = await session.get(Event, event_id)
                    if event is None:
                        errors.append(f"{event_id}: not found")
                        continue
                    try:
                        delivery = await force_enqueue_for_event(session, event)
                    except Exception as exc:  # noqa: BLE001 — surface to operator
                        await session.rollback()
                        errors.append(f"{event_id}: {type(exc).__name__}: {exc}")
                        continue
                    if delivery is None:
                        skipped += 1
                    else:
                        enqueued += 1
                await session.commit()

            if enqueued:
                # Push the worker so the row drains immediately rather than
                # waiting for the next ingest-driven notify.
                await notify_webhook_worker()

        # ---- flash a summary so the operator sees the outcome ----------
        if enqueued:
            Flash.success(
                request,
                f"Re-enqueued {enqueued} webhook deliver{'y' if enqueued == 1 else 'ies'}.",
            )
        if skipped:
            Flash.warning(
                request,
                f"Skipped {skipped} event(s) — either AGG_MDPROJECT_URL is "
                f"unset, or the event isn't in a terminal state "
                f"(finished/postponed/canceled). Check worker logs for the "
                f"per-event reason.",
            )
        for err in errors:
            Flash.error(request, err)
        if not enqueued and not skipped and not errors:
            Flash.info(request, "No events selected.")

        # Bounce back to wherever the operator clicked from (list or detail).
        referer = request.headers.get("referer") or str(
            request.url_for("admin:list", identity=self.identity)
        )
        return RedirectResponse(referer, status_code=302)


class BookmakerView(NavigableModelView, ModelView, model=Bookmaker):
    column_list = ["id", "name", "active"]
    column_searchable_list = ["name"]
    column_sortable_list = ["name", "active"]
    column_default_sort = [("name", False)]
    column_filters = [BooleanFilter(Bookmaker.active)]


class BookmakerSelectionView(ModelView, model=BookmakerSelection):
    column_list = [
        "id", "selection_id", "bookmaker_id", "decimal_odds", "available",
    ]
    column_searchable_list = ["selection_id", "bookmaker_id"]
    column_sortable_list = ["decimal_odds", "available"]
    column_default_sort = [("selection_id", False)]
    column_filters = [
        ForeignKeyFilter(BookmakerSelection.bookmaker_id, Bookmaker.name, foreign_model=Bookmaker),
        BooleanFilter(BookmakerSelection.available),
    ]
    can_create = False
    can_edit = False


class MarketView(ModelView, model=Market):
    # Synthetic ``event_provider`` column resolves through the
    # ``Market.event`` relationship — there is no provider column on
    # core_market itself; provenance lives on the parent Event.
    column_list = [
        "event_provider", "event_id", "type", "scope", "line", "side",
        "category", "is_live", "suspended", "last_updated", "id",
    ]
    column_labels = {"event_provider": "Provider"}
    column_searchable_list = ["id", "event_id", "type"]
    column_formatters = {
        "event_provider": _market_event_provider_badge,
    }
    column_formatters_detail = column_formatters
    column_sortable_list = ["type", "scope", "is_live", "suspended", "last_updated"]
    column_default_sort = [("event_id", False), ("type", False)]
    column_filters = [
        AllUniqueStringValuesFilter(Market.type),
        AllUniqueStringValuesFilter(Market.scope),
        AllUniqueStringValuesFilter(Market.side),
        AllUniqueStringValuesFilter(Market.category),
        BooleanFilter(Market.is_live),
        BooleanFilter(Market.suspended),
    ]


class SelectionView(NavigableModelView, ModelView, model=Selection):
    column_list = [
        "id", "market_id", "type", "decimal_odds",
        "settlement_status", "settlement_source", "settled_at",
    ]
    column_searchable_list = ["id", "market_id"]
    column_sortable_list = ["settlement_status", "settled_at", "decimal_odds", "type"]
    column_default_sort = [("market_id", False)]
    column_filters = [
        StaticValuesFilter(Selection.settlement_status, values=_enum_choices(SettlementStatus)),
        StaticValuesFilter(Selection.settlement_source, values=_enum_choices(SettlementSource)),
        StaticValuesFilter(Selection.type, values=_enum_choices(SelectionType)),
    ]


class OddsQuoteView(ModelView, model=OddsQuote):
    column_list = ["id", "selection_id", "decimal_odds", "captured_at"]
    column_searchable_list = ["selection_id"]
    column_sortable_list = ["captured_at", "decimal_odds"]
    column_default_sort = [("selection_id", False), ("captured_at", True)]
    column_filters = [OperationColumnFilter(OddsQuote.captured_at)]
    can_create = False
    can_edit = False
    can_delete = False


class CronRunView(ModelView, model=CronRun):
    """Browse the run history that the ops console writes."""
    column_list = [
        "id", "cron_name", "trigger_source", "started_at", "finished_at",
        "status", "items_processed",
    ]
    column_searchable_list = ["cron_name", "job_id"]
    column_sortable_list = ["started_at", "cron_name", "status"]
    column_default_sort = [("started_at", True)]
    column_filters = [
        AllUniqueStringValuesFilter(CronRun.cron_name),
        StaticValuesFilter(
            CronRun.status,
            values=[
                (CronRunStatus.RUNNING, "Running"),
                (CronRunStatus.SUCCESS, "Success"),
                (CronRunStatus.FAILED, "Failed"),
                (CronRunStatus.CANCELLED, "Cancelled"),
            ],
        ),
        AllUniqueStringValuesFilter(CronRun.trigger_source),
    ]
    can_create = False
    can_edit = False
    can_delete = False
    name_plural = "Cron runs"
    icon = "fa-solid fa-history"


class CronsConsoleLink(BaseView):
    """Sidebar entry that takes the operator to the HTMX cron-runner page.

    SQLAdmin generates the sidebar URL via ``request.url_for(f"admin:{view.identity}")``
    where ``identity`` defaults to the exposed method's ``__name__``. If we'd
    used a method named ``index`` SQLAdmin would resolve ``admin:index`` —
    which is SQLAdmin's own dashboard route — and the sidebar link would
    silently land on ``/admin/`` instead of our redirect. We pass an explicit
    ``identity="run-crons"`` so the route name is unique.
    """

    name = "Run crons"
    icon = "fa-solid fa-bolt"

    @expose("/run-crons", methods=["GET"], identity="run-crons")
    async def run_crons(self, request):
        return RedirectResponse(url="/ops/crons", status_code=302)


class HistoricalIngestLink(BaseView):
    """Sidebar entry → /ops/historical-ingest (TheSportsDB backfill form).

    Parameterized cron — doesn't fit the no-arg CronSpec list at
    /ops/crons, so it lives on its own page. Admin-only; the route
    enforces the same admin-session check as the cron-runner page.
    """

    name = "Historical ingest"
    icon = "fa-solid fa-clock-rotate-left"

    @expose(
        "/historical-ingest", methods=["GET"], identity="historical-ingest",
    )
    async def historical_ingest(self, request):
        return RedirectResponse(url="/ops/historical-ingest", status_code=302)


class LogoBackfillLink(BaseView):
    """Sidebar entry → /ops/logo-backfill (per-league crest fetch)."""

    name = "Logo backfill"
    icon = "fa-solid fa-image"

    @expose("/logo-backfill", methods=["GET"], identity="logo-backfill")
    async def logo_backfill(self, request):
        return RedirectResponse(url="/ops/logo-backfill", status_code=302)


class DataResetLink(BaseView):
    """Sidebar entry → /ops/data-reset (truncate-with-cascade UI).

    Visible only when ``AGG_TEST_MODE=True`` — production admins shouldn't
    see a button that always 403s. The redirect target itself is also
    test-mode-gated (defense in depth) at ``aggrigator/ops/routes.py``."""

    name = "Data reset"
    icon = "fa-solid fa-trash-can"

    def is_visible(self, request):
        from aggrigator.config import get_settings
        return get_settings().test_mode

    def is_accessible(self, request):
        from aggrigator.config import get_settings
        return get_settings().test_mode

    @expose("/data-reset", methods=["GET"], identity="data-reset")
    async def data_reset(self, request):
        return RedirectResponse(url="/ops/data-reset", status_code=302)


# ---- registration ----------------------------------------------------------


def mount_admin(app, *, base_url: str = "/admin") -> Admin:
    from aggrigator.admin.auth_backend import make_admin_auth

    admin = _RelativeRedirectAdmin(
        app, engine,
        base_url=base_url,
        title="Aggrigator Admin",
        authentication_backend=make_admin_auth(),
        templates_dir=_TEMPLATES_DIR,
    )
    # Top of sidebar: operator action shortcuts.
    admin.add_base_view(CronsConsoleLink)
    admin.add_base_view(HistoricalIngestLink)
    admin.add_base_view(LogoBackfillLink)
    admin.add_base_view(DataResetLink)
    admin.add_base_view(EventsByLeagueSummary)
    admin.add_base_view(TeamsByLeagueSummary)
    for view in [
        TenantUserView, TenantApiKeyView,
        UserView, RefreshTokenView,
        WebhookDeliveryView, AuditLogView,
        CronRunView,
        SportView, LeagueView, TeamView, EventView,
        BookmakerView, BookmakerSelectionView,
        MarketView, SelectionView, OddsQuoteView,
    ]:
        admin.add_view(view)
    return admin
