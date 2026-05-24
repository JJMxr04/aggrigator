"""Install Procrastinate schema + rename cron_run.arq_job_id → job_id.

Replaces the ARQ + Redis background-worker stack. Procrastinate creates
its own tables (``procrastinate_jobs``, ``procrastinate_events``,
``procrastinate_periodic_defers``) plus a handful of triggers, functions,
and types that implement the LISTEN/NOTIFY-based job dispatch. We read
the SQL from the installed ``procrastinate`` package so the schema
always matches the pinned wheel version.

Also renames ``cron_run.arq_job_id`` → ``cron_run.job_id`` (and the
matching index) — the ARQ-specific naming is no longer accurate now
that the column holds Procrastinate job row IDs.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-23
"""

from __future__ import annotations

from alembic import op
from procrastinate.schema import SchemaManager

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The schema SQL ships inside the procrastinate wheel; this binding
    # ensures whatever procrastinate version is pinned in requirements.txt
    # at deploy time is the version whose schema lands in the DB.
    op.execute(SchemaManager.get_schema())

    op.alter_column("cron_run", "arq_job_id", new_column_name="job_id")
    op.drop_index("ix_cron_run_arq_job_id", table_name="cron_run")
    op.create_index(
        "ix_cron_run_job_id", "cron_run", ["job_id"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_cron_run_job_id", table_name="cron_run")
    op.create_index(
        "ix_cron_run_arq_job_id", "cron_run", ["arq_job_id"], unique=True,
    )
    op.alter_column("cron_run", "job_id", new_column_name="arq_job_id")

    # Procrastinate ships no downgrade SQL — drop every object it created.
    # Listed top-down (tables, then functions, then types) so foreign-key
    # / dependency order doesn't bite.
    op.execute("DROP TABLE IF EXISTS procrastinate_periodic_defers CASCADE")
    op.execute("DROP TABLE IF EXISTS procrastinate_events CASCADE")
    op.execute("DROP TABLE IF EXISTS procrastinate_jobs CASCADE")
    op.execute("DROP FUNCTION IF EXISTS procrastinate_defer_job CASCADE")
    op.execute("DROP FUNCTION IF EXISTS procrastinate_defer_periodic_job CASCADE")
    op.execute("DROP FUNCTION IF EXISTS procrastinate_fetch_job CASCADE")
    op.execute("DROP FUNCTION IF EXISTS procrastinate_finish_job CASCADE")
    op.execute("DROP FUNCTION IF EXISTS procrastinate_cancel_job CASCADE")
    op.execute("DROP FUNCTION IF EXISTS procrastinate_retry_job CASCADE")
    op.execute("DROP FUNCTION IF EXISTS procrastinate_notify_queue CASCADE")
    op.execute("DROP FUNCTION IF EXISTS procrastinate_trigger_status_events_procedure_insert CASCADE")
    op.execute("DROP FUNCTION IF EXISTS procrastinate_trigger_status_events_procedure_update CASCADE")
    op.execute("DROP FUNCTION IF EXISTS procrastinate_trigger_scheduled_events_procedure CASCADE")
    op.execute("DROP TYPE IF EXISTS procrastinate_job_status CASCADE")
    op.execute("DROP TYPE IF EXISTS procrastinate_job_event_type CASCADE")
