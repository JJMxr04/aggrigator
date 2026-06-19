"""Summary BaseViews are categorised and registered with the expected routes."""
from __future__ import annotations

from starlette.applications import Starlette

from aggrigator.admin import summaries
from aggrigator.admin.views import mount_admin


def test_summary_views_categorised():
    # category is a class attribute, available pre-registration.
    assert summaries.EventsByLeagueSummary.category == "Summaries"
    assert summaries.TeamsByLeagueSummary.category == "Summaries"


def test_summary_views_registered_with_identities():
    app = Starlette()
    mount_admin(app)  # registration sets BaseView.identity from @expose
    assert summaries.EventsByLeagueSummary.identity == "events-by-league"
    assert summaries.TeamsByLeagueSummary.identity == "teams-by-league"
    # Collect all route paths from the mounted app to confirm route presence.
    all_paths = []

    def _collect(routes):
        for r in routes:
            if hasattr(r, "path"):
                all_paths.append(r.path)
            if hasattr(r, "routes"):
                _collect(r.routes)
            if hasattr(r, "app") and hasattr(r.app, "routes"):
                _collect(r.app.routes)

    _collect(app.routes)
    assert any("events-by-league" in p for p in all_paths)
    assert any("teams-by-league" in p for p in all_paths)
