"""Domain models — exact parity with MDProject ``core/event/models`` plus
aggregator-only auth/webhook tables (Phase 1+).
"""

from aggrigator.models.audit import AuditLog
from aggrigator.models.auth import (
    ApiKey,
    ClientApp,
    RefreshToken,
    User,
    UserRole,
    UserTier,
)
from aggrigator.models.base import Base, TimestampMixin
from aggrigator.models.bookmaker import Bookmaker, BookmakerSelection
from aggrigator.models.cron_run import CronRun, CronRunSource, CronRunStatus
from aggrigator.models.event import Event
from aggrigator.models.league import League
from aggrigator.models.market import Market, MarketCategory, MarketScope
from aggrigator.models.quote import OddsQuote
from aggrigator.models.selection import (
    Selection,
    SelectionType,
    SettlementSource,
    SettlementStatus,
)
from aggrigator.models.sport import Sport
from aggrigator.models.team import Team
from aggrigator.models.webhook import WebhookDelivery, WebhookEndpoint

__all__ = [
    "ApiKey",
    "AuditLog",
    "Base",
    "Bookmaker",
    "BookmakerSelection",
    "ClientApp",
    "CronRun",
    "CronRunSource",
    "CronRunStatus",
    "Event",
    "League",
    "Market",
    "MarketCategory",
    "MarketScope",
    "OddsQuote",
    "RefreshToken",
    "Selection",
    "SelectionType",
    "SettlementSource",
    "SettlementStatus",
    "Sport",
    "Team",
    "TimestampMixin",
    "User",
    "UserRole",
    "UserTier",
    "WebhookDelivery",
    "WebhookEndpoint",
]
