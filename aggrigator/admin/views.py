"""SQLAdmin model views — one per table, read-mostly except for moderation
edits. Mounted at /admin in main.py."""

from __future__ import annotations

from markupsafe import Markup
from sqladmin import Admin, BaseView, ModelView, expose
from starlette.responses import RedirectResponse

from aggrigator.db import engine


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


# ---- view classes ----------------------------------------------------------


class UserView(ModelView, model=User):
    column_list = ["id", "email", "role", "is_active", "created"]
    column_searchable_list = ["email"]
    column_sortable_list = ["created", "email"]
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
    can_create = False
    can_edit = False


class WebhookDeliveryView(ModelView, model=WebhookDelivery):
    column_list = [
        "id", "event_id", "event_name",
        "attempts", "last_status", "delivered_at", "next_retry_at", "created_at",
    ]
    column_searchable_list = ["event_id", "event_name"]
    can_create = False
    can_edit = False


class AuditLogView(ModelView, model=AuditLog):
    column_list = [
        "id", "event_name", "actor_user_id", "target_type", "target_id",
        "ip", "created_at",
    ]
    column_searchable_list = ["event_name", "target_id"]
    column_sortable_list = ["created_at"]
    can_create = False
    can_edit = False
    can_delete = False  # audit log is append-only


class SportView(ModelView, model=Sport):
    column_list = ["id", "name", "active"]


class LeagueView(ModelView, model=League):
    column_list = ["id", "sport_id", "name", "active", "last_refreshed_at"]


class TeamView(ModelView, model=Team):
    column_list = ["id", "league_id", "team_id", "name_long"]
    column_searchable_list = ["name_long", "team_id"]


class EventView(ModelView, model=Event):
    column_list = [
        "id", "league_id", "status_type", "start_time",
        "home_team_id", "away_team_id", "home_score", "away_score",
    ]
    column_searchable_list = ["id"]
    column_sortable_list = ["start_time", "status_type"]


class BookmakerView(ModelView, model=Bookmaker):
    column_list = ["id", "name", "active"]


class BookmakerSelectionView(ModelView, model=BookmakerSelection):
    column_list = [
        "id", "selection_id", "bookmaker_id", "decimal_odds", "available",
    ]
    can_create = False
    can_edit = False


class MarketView(ModelView, model=Market):
    column_list = [
        "id", "event_id", "category", "type", "scope", "line", "side",
        "is_live", "suspended", "last_updated",
    ]
    column_searchable_list = ["id", "event_id", "type"]


class SelectionView(ModelView, model=Selection):
    column_list = [
        "id", "market_id", "type", "decimal_odds",
        "settlement_status", "settlement_source", "settled_at",
    ]
    column_searchable_list = ["id", "market_id"]


class OddsQuoteView(ModelView, model=OddsQuote):
    column_list = ["id", "selection_id", "decimal_odds", "captured_at"]
    can_create = False
    can_edit = False
    can_delete = False


class CronRunView(ModelView, model=CronRun):
    """Browse the run history that the ops console writes."""
    column_list = [
        "id", "cron_name", "trigger_source", "started_at", "finished_at",
        "status", "items_processed",
    ]
    column_searchable_list = ["cron_name", "arq_job_id"]
    column_sortable_list = ["started_at", "cron_name", "status"]
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

    admin = Admin(
        app, engine,
        base_url=base_url,
        title="Aggrigator Admin",
        authentication_backend=make_admin_auth(),
    )
    # Top of sidebar: operator action shortcuts.
    admin.add_base_view(CronsConsoleLink)
    admin.add_base_view(DataResetLink)
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
