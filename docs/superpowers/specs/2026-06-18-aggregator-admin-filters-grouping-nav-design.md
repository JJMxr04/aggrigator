# Aggregator Admin — Filters, Search, Grouping & Form Navigation

**Date:** 2026-06-18
**Status:** Approved design, pending implementation plan
**Scope:** `aggrigator/aggrigator/admin/` (SQLAdmin views + templates)

## Problem

The aggregator admin (SQLAdmin 0.25.1 on FastAPI/SQLAlchemy, mounted at `/admin`)
has thin list-view ergonomics: only 9 of 15 views have search, 8 have sorting,
**none** have filters, there is no grouping/aggregation anywhere, and edit/form
pages have no record-to-record navigation. Operators reviewing teams, events,
markets, cron runs, etc. have to fall back to raw search every time.

This work adds, across **all 15 registered model views**:
1. Native list **filters** + broadened **search** and **sort**.
2. **Grouping** in two forms: default row clustering on every view, plus two
   real aggregate summary pages.
3. **Prev / Next** record navigation on the edit (form) view.

## Constraints & facts (verified against installed code)

- SQLAdmin **0.25.1** ships a real filter API (`column_filters`, `sqladmin/site-packages/sqladmin/filters.py`):
  - `BooleanFilter(column)` — Yes/No/All.
  - `StaticValuesFilter(column, values=[(val,label),...])` — fixed dropdown; use where a `StrEnum` exists.
  - `AllUniqueStringValuesFilter(column)` — distinct-values dropdown via `SELECT DISTINCT`; use for low-cardinality free-text columns (e.g. `Event.status_type`, which is `String(32)`, not an enum).
  - `ForeignKeyFilter(fk, foreign_display_field, foreign_model)` — dropdown of related rows; the key lever for "by league" / "by sport".
  - `OperationColumnFilter(column)` — typed operator filter (ranges/comparisons) for dates and numerics.
- SQLAdmin Jinja env has `enable_async=True` (`templating.py:52`) — templates may `await` coroutines.
- The edit route (`application.py:626`) passes `obj` and `model_view` into the
  template context; `request` is injected by SQLAdmin's context processors.
  This means Prev/Next can be computed via a method on the view, called from the
  template — **no fork of SQLAdmin routes required**.
- All core entity PKs are **single-column strings** (`Event.id`, `Market.id`,
  `Selection.id` are `String`; integer-PK models likewise single-column). No
  composite PKs in the registered set, so navigation has a clean ordering key.
- `edit.html` is already overridden (Cloudflare-Tunnel CSP path-only form action)
  and extends `sqladmin_original/edit.html`, exposing the `edit_form` and
  `submit_buttons_bottom` blocks.
- The `_RelativeRedirectAdmin` subclass and per-view masking/formatters/actions
  (e.g. `EventView.re_enqueue_webhook`, `TeamView` color pickers) must be left
  intact. Read-only flags (`can_create/can_edit/can_delete = False`) stay as-is;
  filters/search work on read-only views too.

## Design

### Component 1 — Per-view filters, search & sort (all 15 views)

Edit each `ModelView` subclass in `aggrigator/admin/views.py` to add a
`column_filters` list and extend `column_searchable_list` /
`column_sortable_list`. Filter-type selection rule:

- Boolean column → `BooleanFilter`.
- Column backed by a `StrEnum` → `StaticValuesFilter` with the enum's members.
- Low-cardinality free-text/string column → `AllUniqueStringValuesFilter`.
- FK column with a human display field → `ForeignKeyFilter`.
- Datetime / numeric column → `OperationColumnFilter`.

Planned per-view filter coverage (exact final lists fixed during implementation;
this is the intent):

| View | New filters | Search additions | Sort additions |
|---|---|---|---|
| UserView | `role` (static), `is_active` (bool) | — | `role` |
| TenantUserView | `tier` (static), `status` (static) | — | (has tier/status) |
| TenantApiKeyView | revoked? (bool over `revoked_at` via Operation/Boolean) | — | — |
| RefreshTokenView | `expires_at`, `revoked_at` (operation) | `user_id` | `expires_at`, `created_at` |
| WebhookDeliveryView | `last_status` (unique), `attempts` (op), `created_at` (op) | — | `created_at`, `attempts`, `last_status` |
| AuditLogView | `event_name` (unique), `target_type` (unique), `created_at` (op) | `actor_user_id` | (has created_at) |
| SportView | `active` (bool) | `name` | `name`, `active` |
| LeagueView | `sport_id` (FK→Sport.name), `active` (bool), `can_pull_historical_scores` (bool) | `name` | `name` |
| TeamView | `league_id` (FK→League.name), `match_confirmed` (bool), `match_source` (unique) | (rich already) | (has them) |
| EventView | `league_id` (FK→League.name), `status_type` (unique), `provider` (static), `start_time` (op) | — | (has them) |
| BookmakerView | `active` (bool) | `name` | `name`, `active` |
| BookmakerSelectionView | `bookmaker_id` (FK→Bookmaker.name), `available` (bool) | `selection_id`, `bookmaker_id` | `decimal_odds`, `available` |
| MarketView | `type` (static), `scope` (unique), `side` (unique), `category` (unique), `is_live` (bool), `suspended` (bool) | — | `type`, `is_live`, `suspended`, `last_updated` |
| SelectionView | `settlement_status` (static enum), `settlement_source` (static enum), `type` (static enum) | — | `settlement_status`, `settled_at`, `decimal_odds` |
| OddsQuoteView | `selection_id` (search), `captured_at` (op) | `selection_id` | `captured_at`, `decimal_odds` |
| CronRunView | `cron_name` (unique), `status` (static), `trigger_source` (static) | — | (has them) |

`ForeignKeyFilter` needs the related model imported into `views.py` (Sport,
League, Bookmaker already are). Enum-backed `StaticValuesFilter`s import the
`StrEnum` classes (e.g. `SelectionType`, `SettlementStatus`, `SettlementSource`
from `models/selection.py`) and build `[(e.value, e.name) for e in Enum]`.

To keep `views.py` readable as filter lists grow, factor small helpers near the
existing mask helpers, e.g. `_enum_choices(EnumCls)` returning the
`(value,label)` tuples. No behavioural change to existing formatters/actions.

### Component 2 — Grouping

**2a. Default clustering (all views).** Ensure every view has a
`column_default_sort` that places related rows adjacently, and that the grouping
key is in `column_sortable_list` so the operator can re-cluster by clicking.
EventView and TeamView already do this; extend the pattern to Market (by
`event_id` then `type`), Selection (by `market_id`), BookmakerSelection (by
`selection_id`), OddsQuote (by `selection_id`, `captured_at` desc), League (by
`sport_id`, `name`). Combined with the Component-1 FK filters, this delivers the
practical "group by league / by parent" experience SQLAdmin lacks natively.

**2b. Two real aggregate summary pages.** Add a new module
`aggrigator/admin/summaries.py` defining `BaseView` subclasses with `@expose`
routes (admin-session gated like the existing ops links), registered via
`admin.add_base_view(...)` under a "Summaries" sidebar group:

- **Events by league** — `GROUP BY league_id, status_type` → table of league
  name, per-status counts, total. Ordered by total desc.
- **Teams by league** — `GROUP BY league_id` → league name, total teams,
  confirmed vs unconfirmed (`match_confirmed`) counts.

Each runs one aggregate query via `async_session_factory()` and renders a
Jinja template (`templates/sqladmin/summaries/<name>.html`, extending SQLAdmin's
base layout so the sidebar/chrome match). League names resolved by joining
`League`. These follow the established `BaseView` + redirect/expose pattern
already used by `CronsConsoleLink` et al., but render their own content rather
than redirecting. Pattern is reusable for more rollups later (explicitly out of
scope to add more than these two now).

### Component 3 — Prev / Next on the edit form

Add a `NavigableModelView` mixin (in `views.py`, or a small
`admin/navigation.py` if it keeps `views.py` tidy) with:

```python
async def neighbor_pks(self, request, obj) -> dict:
    """Return {"prev": pk|None, "next": pk|None} for the current object,
    ordered by this view's navigation key (first column_default_sort entry,
    pk as tie-break)."""
```

Implementation:
- Determine the nav column + direction from `column_default_sort[0]` (fallback:
  the model PK, ascending).
- Two bounded queries, each `LIMIT 1`:
  - **next** = first row whose `(nav_value, pk)` sorts immediately after the
    current row in the view's order.
  - **prev** = first row immediately before, using the reversed comparison.
- Use a lexicographic `(nav_col, pk)` tuple comparison so ties on the nav column
  resolve deterministically by PK. Both queries are index-friendly (`LIMIT 1`),
  so this stays cheap even on large tables.
- Returns `None` at the ends (button rendered disabled).

The relevant `ModelView` subclasses inherit the mixin (apply broadly; harmless on
read-only views — navigation doesn't mutate). The overridden `edit.html` gains a
nav row (new block, above `submit_buttons_bottom`) that awaits
`model_view.neighbor_pks(request, obj)` and renders:

```
‹ Prev    Next ›
```

as links to `admin:edit` for the neighbor PKs (using the same
`model_view._build_url_for('admin:edit', request, neighbor)` path-only pattern
the file already uses for CSP), each disabled when its PK is `None`. No save
occurs — navigation only.

**Ordering scope (v1):** Prev/Next follows each view's *default* sort order, not
the operator's live per-request filter/sort/search state. Carrying live list
state into navigation is a documented future enhancement.

## Out of scope (v1)

- Save-and-Next (save current edit then advance).
- Live-filter/sort-aware Prev/Next (uses default sort only).
- Admin views for the three currently-unregistered models (`Bet`, `TeamLogo`,
  `CronSchedule`).
- Additional aggregate summary pages beyond the two specified.

## Testing

- **Filters:** for a representative view of each filter type, assert the filtered
  query returns the expected subset (BooleanFilter true/false, StaticValuesFilter
  enum value, ForeignKeyFilter by league, OperationColumnFilter date range).
  Integration tests hit the admin list route with the filter query param and
  assert row membership. **Use `AGG_TEST_DATABASE_URL` → `aggrigator_test`
  (5434), never the dev DB — the integration suite truncates its target.**
- **Summaries:** seed a small fixture (2 leagues, mixed event statuses / team
  confirmation), call each summary route, assert the rendered counts match a
  hand-computed expectation.
- **Prev/Next:** unit-test `neighbor_pks` against an ordered fixture — first row
  has `prev=None`, last has `next=None`, middle rows point to the correct
  adjacent PKs under the view's default sort (including a nav-column tie resolved
  by PK). One route-level test asserting the buttons render with correct hrefs
  and disabled state at the ends.
- Smoke-check that existing behaviour is intact: EventView re-enqueue action,
  TeamView color pickers, masked reveals, and the CSP path-only redirect.

## Files touched

- `aggrigator/admin/views.py` — filters/search/sort/default-sort per view; mixin
  wiring (or import from `navigation.py`).
- `aggrigator/admin/navigation.py` *(new, optional)* — `NavigableModelView`.
- `aggrigator/admin/summaries.py` *(new)* — two aggregate `BaseView`s.
- `aggrigator/admin/templates/sqladmin/edit.html` — Prev/Next nav row.
- `aggrigator/admin/templates/sqladmin/summaries/*.html` *(new)* — summary tables.
- `aggrigator/admin/views.py` `mount_admin()` — register summary base views.
- Tests under the aggregator's existing admin/integration test location.
