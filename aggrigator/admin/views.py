"""SQLAdmin model views — one per table, read-mostly except for moderation
edits. Mounted at /admin in main.py."""

from __future__ import annotations

from sqladmin import Admin, BaseView, ModelView, expose
from starlette.responses import RedirectResponse

from aggrigator.db import engine
from aggrigator.models import (
    ApiKey,
    AuditLog,
    Bookmaker,
    BookmakerSelection,
    ClientApp,
    CronRun,
    Event,
    League,
    Market,
    OddsQuote,
    RefreshToken,
    Selection,
    Sport,
    Team,
    User,
    WebhookDelivery,
    WebhookEndpoint,
)


# ---- view classes ----------------------------------------------------------


class UserView(ModelView, model=User):
    column_list = ["id", "email", "role", "tier", "is_active", "created"]
    column_searchable_list = ["email"]
    column_sortable_list = ["created", "email"]
    form_excluded_columns = ["password_hash"]
    icon = "fa-solid fa-user"


class ApiKeyView(ModelView, model=ApiKey):
    column_list = [
        "id", "user_id", "name", "prefix", "last_four",
        "last_used_at", "revoked_at", "created_at",
    ]
    column_searchable_list = ["prefix", "name"]
    can_create = False
    can_edit = False  # rotation/revocation goes through the API


class RefreshTokenView(ModelView, model=RefreshToken):
    column_list = ["id", "user_id", "expires_at", "revoked_at", "created_at"]
    can_create = False
    can_edit = False


class ClientAppView(ModelView, model=ClientApp):
    column_list = ["id", "slug", "name", "tier", "trusted", "revoked_at", "created"]
    column_searchable_list = ["slug", "name"]


class WebhookEndpointView(ModelView, model=WebhookEndpoint):
    column_list = ["id", "user_id", "url", "enabled", "scope", "events", "created"]


class WebhookDeliveryView(ModelView, model=WebhookDelivery):
    column_list = [
        "id", "endpoint_id", "event_id", "event_name",
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
    """Sidebar entry → /ops/data-reset (truncate-with-cascade UI)."""

    name = "Data reset"
    icon = "fa-solid fa-trash-can"

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
        UserView, ApiKeyView, RefreshTokenView, ClientAppView,
        WebhookEndpointView, WebhookDeliveryView, AuditLogView,
        CronRunView,
        SportView, LeagueView, TeamView, EventView,
        BookmakerView, BookmakerSelectionView,
        MarketView, SelectionView, OddsQuoteView,
    ]:
        admin.add_view(view)
    return admin
