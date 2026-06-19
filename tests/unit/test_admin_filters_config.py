"""Filter/search/sort configuration on the registry & entity admin views."""

from __future__ import annotations

from sqladmin.filters import (
    AllUniqueStringValuesFilter,
    BooleanFilter,
    ForeignKeyFilter,
)

from aggrigator.admin import views


def _filter_types(view):
    return {type(f) for f in view.column_filters}


def test_enum_choices_helper():
    from aggrigator.models.selection import SettlementStatus

    choices = views._enum_choices(SettlementStatus)
    assert ("WON", "WON") in choices
    assert ("PENDING", "PENDING") in choices
    assert all(isinstance(v, str) and isinstance(label, str) for v, label in choices)


def test_sport_view_filters_and_search():
    assert BooleanFilter in _filter_types(views.SportView)
    assert "name" in views.SportView.column_searchable_list
    assert "name" in views.SportView.column_sortable_list


def test_league_view_has_sport_fk_filter_and_bools():
    types = _filter_types(views.LeagueView)
    assert ForeignKeyFilter in types
    assert BooleanFilter in types  # active + can_pull_historical_scores
    assert "name" in views.LeagueView.column_searchable_list


def test_team_view_has_league_fk_and_match_confirmed_filter():
    types = _filter_types(views.TeamView)
    assert ForeignKeyFilter in types     # league_id -> League.name
    assert BooleanFilter in types        # match_confirmed
    # Existing rich search must be preserved.
    assert "canonical_name" in views.TeamView.column_searchable_list


def test_event_view_has_league_fk_and_status_filter():
    types = _filter_types(views.EventView)
    assert ForeignKeyFilter in types                 # league_id -> League.name
    assert AllUniqueStringValuesFilter in types       # status_type (free text)
    # Existing provider/start_time clustering sort preserved.
    assert views.EventView.column_default_sort[0][0] == "provider"


def test_market_view_filters():
    from sqladmin.filters import AllUniqueStringValuesFilter, BooleanFilter

    types = _filter_types(views.MarketView)
    assert BooleanFilter in types                  # is_live, suspended
    assert AllUniqueStringValuesFilter in types    # type (composite strings like NBA_POINTS_ML)
    assert "type" in views.MarketView.column_sortable_list


def test_selection_view_enum_filters():
    from sqladmin.filters import StaticValuesFilter

    types = _filter_types(views.SelectionView)
    assert StaticValuesFilter in types
    # settlement_status enum choices wired from the StrEnum.
    param_names = {f.parameter_name for f in views.SelectionView.column_filters}
    assert "settlement_status" in param_names


def test_bookmaker_selection_fk_filter():
    from sqladmin.filters import BooleanFilter, ForeignKeyFilter

    types = _filter_types(views.BookmakerSelectionView)
    assert ForeignKeyFilter in types   # bookmaker_id -> Bookmaker.name
    assert BooleanFilter in types       # available


def test_bookmaker_and_oddsquote_config():
    from sqladmin.filters import BooleanFilter

    assert BooleanFilter in _filter_types(views.BookmakerView)
    assert "name" in views.BookmakerView.column_searchable_list
    assert "selection_id" in views.OddsQuoteView.column_searchable_list
    assert "captured_at" in views.OddsQuoteView.column_sortable_list


def test_user_view_filters():
    from sqladmin.filters import BooleanFilter, StaticValuesFilter

    types = _filter_types(views.UserView)
    assert BooleanFilter in types       # is_active
    assert StaticValuesFilter in types   # role


def test_tenant_user_view_filters_preserve_masking():
    from sqladmin.filters import StaticValuesFilter

    assert StaticValuesFilter in _filter_types(views.TenantUserView)
    # Masking formatter must stay intact.
    assert "email" in views.TenantUserView.column_formatters
    assert views.TenantUserView.can_edit is False


def test_webhook_and_audit_and_cron_filters():
    from sqladmin.filters import (
        AllUniqueStringValuesFilter,
        OperationColumnFilter,
        StaticValuesFilter,
    )

    assert OperationColumnFilter in _filter_types(views.WebhookDeliveryView)
    assert AllUniqueStringValuesFilter in _filter_types(views.AuditLogView)
    assert StaticValuesFilter in _filter_types(views.CronRunView)
    assert "created_at" in views.WebhookDeliveryView.column_sortable_list
