"""upsert_team_from_spec enqueues a logo fetch for newly-created teams."""

from __future__ import annotations

from unittest import mock

import pytest

from aggrigator.ingest import upserts
from aggrigator.ingest.normalize import TeamSpec


@pytest.mark.asyncio
async def test_new_team_enqueues_logo_fetch():
    spec = TeamSpec(
        team_id="38", league_id="usa-nba", name_long="Lakers",
        name_medium="Lakers", name_short="LAL",
    )

    session = mock.AsyncMock()
    session.add = mock.Mock()  # add() is sync; keep it off the AsyncMock
    # session.get calls in order:
    #   1. session.get(League, "usa-nba") -> league with sport_id
    #   2. session.get(Team, "usa-nba:38") -> None (brand-new team)
    session.get = mock.AsyncMock(side_effect=[mock.Mock(sport_id="basketball"), None])
    # canonical-name fallback scalar -> None (no existing canonical match)
    session.scalar = mock.AsyncMock(return_value=None)

    with mock.patch.object(upserts, "enqueue_logo_fetch", new_callable=mock.AsyncMock) as enq:
        await upserts.upsert_team_from_spec(session, spec)

    enq.assert_awaited_once_with("usa-nba:38")


@pytest.mark.asyncio
async def test_existing_team_does_not_enqueue():
    spec = TeamSpec(
        team_id="38", league_id="usa-nba", name_long="Lakers",
        name_medium="Lakers", name_short="LAL",
    )
    existing = mock.Mock(odds_api_io_key="38")
    session = mock.AsyncMock()
    session.add = mock.Mock()  # add() is sync; keep it off the AsyncMock
    # session.get calls in order:
    #   1. session.get(League, "usa-nba") -> league with sport_id
    #   2. session.get(Team, "usa-nba:38") -> existing row (update path)
    session.get = mock.AsyncMock(side_effect=[mock.Mock(sport_id="basketball"), existing])

    with mock.patch.object(upserts, "enqueue_logo_fetch", new_callable=mock.AsyncMock) as enq:
        await upserts.upsert_team_from_spec(session, spec)

    enq.assert_not_called()


@pytest.mark.asyncio
async def test_existing_team_enqueues_when_key_first_set():
    # A roster-filler row already exists but has never carried a provider
    # key. The live feed surfaces its odds-api.io id for the first time —
    # that NULL -> real transition must enqueue a logo fetch, keyed on the
    # row's stable PK (the filler PK, not synth_pk).
    spec = TeamSpec(
        team_id="38", league_id="usa-nba", name_long="Lakers",
        name_medium="Lakers", name_short="LAL",
    )
    existing = mock.Mock(id="usa-nba:_canon:lakers", odds_api_io_key=None)
    session = mock.AsyncMock()
    session.add = mock.Mock()
    session.get = mock.AsyncMock(
        side_effect=[mock.Mock(sport_id="basketball"), existing]
    )
    with mock.patch.object(
        upserts, "enqueue_logo_fetch", new_callable=mock.AsyncMock
    ) as enq:
        await upserts.upsert_team_from_spec(session, spec)
    enq.assert_awaited_once_with("usa-nba:_canon:lakers")


@pytest.mark.asyncio
async def test_existing_team_with_empty_string_key_enqueues():
    spec = TeamSpec(
        team_id="38", league_id="usa-nba", name_long="Lakers",
        name_medium="Lakers", name_short="LAL",
    )
    existing = mock.Mock(id="usa-nba:38", odds_api_io_key="")
    session = mock.AsyncMock()
    session.add = mock.Mock()
    session.get = mock.AsyncMock(
        side_effect=[mock.Mock(sport_id="basketball"), existing]
    )
    with mock.patch.object(
        upserts, "enqueue_logo_fetch", new_callable=mock.AsyncMock
    ) as enq:
        await upserts.upsert_team_from_spec(session, spec)
    enq.assert_awaited_once_with("usa-nba:38")
