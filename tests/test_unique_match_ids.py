"""Match and round ids must stay unique across process restarts.

The old scheme derived ids from in-memory counters (``match-{counter}``,
``round-{match_id}-{n}``), so a restart that reset the counters re-issued ids
that already existed in the database. Ids are now random tokens.
"""

import main


def _start(client, auth_headers, user):
    return client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(user)
    )


def _create_ranked_match(client, auth_headers, player_a, player_b) -> str:
    first = _start(client, auth_headers, player_a)
    assert first.status_code == 200
    assert first.json()["status"] == "searching"

    second = _start(client, auth_headers, player_b)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["status"] == "matched"
    return body["match_id"]


def test_two_ranked_matches_get_distinct_ids(client, auth_headers):
    match_a = _create_ranked_match(client, auth_headers, "guest-ids-a1", "guest-ids-a2")
    match_b = _create_ranked_match(client, auth_headers, "guest-ids-b1", "guest-ids-b2")
    assert match_a != match_b


def test_match_id_does_not_collide_after_counter_reset(client, auth_headers):
    """Simulate a restart: resetting the legacy counter must not reuse an id."""
    match_a = _create_ranked_match(client, auth_headers, "guest-rst-a1", "guest-rst-a2")

    # With counter-based ids the next match would also be "match-1".
    main.match_counter = 0

    match_b = _create_ranked_match(client, auth_headers, "guest-rst-b1", "guest-rst-b2")
    assert match_b != match_a
    assert match_a in main.in_memory_matches
    assert match_b in main.in_memory_matches


def test_round_ids_unique_within_match_and_after_memory_loss(
    client, auth_headers, fixed_question, monkeypatch
):
    inserted_round_ids = []

    async def _spy_insert_one(doc, *args, **kwargs):
        inserted_round_ids.append(doc["_id"])
        return None

    monkeypatch.setattr(main.rounds_collection, "insert_one", _spy_insert_one)

    player = "guest-round-p1"
    match_id = _create_ranked_match(client, auth_headers, player, "guest-round-p2")

    first = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )
    assert first.status_code == 200, first.text
    round_1 = first.json()["round_id"]

    # Finish round 1 so the next question request creates a new round.
    main.in_memory_rounds[round_1]["winner_id"] = "tie"

    second = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )
    assert second.status_code == 200, second.text
    round_2 = second.json()["round_id"]
    assert round_2 != round_1

    # Simulate a restart that lost in-memory rounds but kept the match: the
    # old code re-counted rounds from zero and re-issued "round-{match}-1".
    main.in_memory_rounds.clear()

    third = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )
    assert third.status_code == 200, third.text
    round_3 = third.json()["round_id"]
    assert round_3 not in {round_1, round_2}

    # Every DB insert (the spy stands in for the real collection) used a
    # never-before-seen id.
    assert len(inserted_round_ids) == len(set(inserted_round_ids))
    assert {round_1, round_2, round_3} <= set(inserted_round_ids)
