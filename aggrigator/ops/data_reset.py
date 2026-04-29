"""Data-reset service — list tables + counts, truncate one table at a time
with CASCADE.

This is a destructive admin tool, so:

- Tables we expose are pulled from ``Base.metadata`` at module load — never
  from user input — to make SQL-injection structurally impossible.
- Every truncate uses ``TRUNCATE TABLE {qualified} RESTART IDENTITY CASCADE``
  so dependent FKs go too and BIGSERIAL counters reset to 1.
- Each truncate writes an ``audit_log`` row before the table goes away
  (the row lives in ``audit_log`` itself; if the operator nukes the audit
  table, that's an audit-log truncation event in its own right and the
  earlier rows stay until cascading kicks in).

Tables are grouped into "domain" (sport/league/event/market/…), "auth"
(user/api_key/client_app/refresh_token), "webhook", and "ops" (audit_log,
cron_run) so the UI can render them in semantically meaningful sections.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aggrigator.models import Base
from aggrigator.security import audit

logger = logging.getLogger(__name__)


# ---- table classification --------------------------------------------------

DOMAIN_TABLES = {
    "core_sport", "core_league", "core_team", "core_event_event",
    "core_bookmaker", "core_bookmaker_selection",
    "core_market", "core_selection", "core_odds_quote",
}
AUTH_TABLES = {
    "auth_user", "auth_api_key", "auth_refresh_token", "client_app",
}
WEBHOOK_TABLES = {"webhook_endpoint", "webhook_delivery"}
OPS_TABLES = {"audit_log", "cron_run"}


def _section_for(table_name: str) -> str:
    if table_name in DOMAIN_TABLES:
        return "Domain"
    if table_name in AUTH_TABLES:
        return "Auth"
    if table_name in WEBHOOK_TABLES:
        return "Webhooks"
    if table_name in OPS_TABLES:
        return "Ops"
    return "Other"


# Tables where truncating would lock the operator out / break the running
# session. Allowed but flagged with a warning in the UI.
DANGEROUS_TABLES = {"auth_user", "client_app"}


# ---- public API ------------------------------------------------------------


@dataclass
class TableInfo:
    name: str                # physical table name
    section: str             # "Domain" / "Auth" / "Webhooks" / "Ops" / "Other"
    row_count: int
    is_dangerous: bool       # truncating may log out / break in-flight ops


def known_tables() -> list[str]:
    """Allowlist of table names we'll let the UI ask to truncate.

    Sourced from SQLAlchemy ``Base.metadata`` — anything not registered as a
    model can't be touched from the UI, even if it physically exists in the
    DB. (alembic_version is excluded explicitly.)
    """
    return sorted(
        t for t in Base.metadata.tables
        if t != "alembic_version"
    )


async def list_table_info(session: AsyncSession) -> list[TableInfo]:
    """Returns every known table + its current row count."""
    out: list[TableInfo] = []
    for name in known_tables():
        # Bind via parameterized identifier? Postgres doesn't allow placeholders
        # for table names. Safe because the value comes from the allowlist
        # above, not from request input.
        result = await session.execute(text(f'SELECT COUNT(*) FROM "{name}"'))
        count = result.scalar_one()
        out.append(TableInfo(
            name=name,
            section=_section_for(name),
            row_count=count,
            is_dangerous=name in DANGEROUS_TABLES,
        ))
    # Order: Domain → Webhooks → Ops → Auth → Other (danger last).
    section_order = {"Domain": 0, "Webhooks": 1, "Ops": 2, "Auth": 3, "Other": 4}
    return sorted(out, key=lambda t: (section_order.get(t.section, 99), t.name))


async def truncate_table(
    session: AsyncSession,
    *,
    table: str,
    actor_user_id: uuid.UUID | None,
    actor_email: str | None,
    ip: str | None,
) -> int:
    """Truncate one table CASCADE. Returns the count of rows removed
    (best-effort — the cascade may delete more in dependent tables, those
    aren't counted here)."""
    if table not in known_tables():
        raise ValueError(f"Unknown / disallowed table: {table!r}")

    # Audit BEFORE the truncate. If the operator is nuking ``audit_log``
    # itself, this row is the last one to disappear — useful evidence that
    # someone did it.
    pre_count_result = await session.execute(
        text(f'SELECT COUNT(*) FROM "{table}"')
    )
    pre_count = pre_count_result.scalar_one()

    await audit.write(
        session,
        event_name="ops.truncate",
        actor_user_id=actor_user_id,
        ip=ip,
        target_type="table",
        target_id=table,
        metadata={
            "actor_email": actor_email,
            "rows_before_truncate": pre_count,
        },
    )

    await session.execute(
        text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
    )
    await session.commit()

    logger.warning(
        "ops.truncate cron=%s actor=%s rows=%d",
        table, actor_email, pre_count,
    )
    return pre_count
