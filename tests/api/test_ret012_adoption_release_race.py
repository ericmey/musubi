# PROVENANCE: written by Aoi 2026-09-20 against musubi #732 head ba5a6ad9
# (worktree .claude/worktrees/aoi-732-race-repro, detached - Shiori's branch untouched).
# Red-proofed BOTH directions:
#   ba5a6ad9 as-is        -> subject RED (response version 9, committed 2), control GREEN
#   with 'return stored'  -> both GREEN; tests/api/test_idem007+008 still 33 passed
# Target path in the repo: tests/api/test_ret012_adoption_release_race.py

"""RET-012: the adopted-token release must answer from the saga's own committed
snapshot, never from a re-read a concurrent writer can win.

``_release_adopted_done_token`` deletes the exact ``done:*`` token and then
re-reads the row.  Between those two calls the lease is free.  Another writer
can take it, land an ordinary metadata PATCH, and clear its own token -- and
both of the function's guards still pass, because the token it looked for is
gone and no *other* token remains.  The re-read is then the concurrent writer's
state, it becomes the retraction's response, and a receipt replay answers with
a version the retraction never committed.

The crashed-committer state is produced the way production produces it: the
payload commit mints the ``done:*`` token, and the release ``delete_payload``
immediately after it is made to fail.  Nothing is hand-stamped.

Every cell carries a control.  A race cell that stops interleaving looks
exactly like a race cell that passes, so the subject also asserts that the
intruder write REALLY LANDED -- without that, this file could go quiet and
still read green.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient, models

from musubi.planes.episodic import EpisodicPlane
from musubi.store.mutation_lease import is_expired_done_token

from tests.api.test_idem007_retraction_saga import _body, _headers, _layout, _seed


def _anchor_row(qdrant: QdrantClient, object_id: str) -> dict[str, Any]:
    rows = _layout(qdrant, object_id)
    assert rows, f"no episodic row for {object_id}"
    return rows[0]


def _crash_committed_retraction(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    content: str,
    key: str,
) -> tuple[str, int, dict[str, Any]]:
    """Commit a retraction and crash in its token release, leaving the row in
    the exact state a crashed committer leaves: payload committed, ``done:*``
    token still held.

    ``content`` must differ per cell -- the episodic plane dedups on content,
    so two cells seeding the same prose share one row and the second fails with
    a baffling version conflict instead of testing anything (Shiori, 2026-09-20).
    """
    memory = _seed(episodic, content=content)
    real_delete = qdrant.delete_payload
    crashed: list[bool] = []

    def crash_the_release(*args: object, **kwargs: object) -> object:
        if not crashed:
            crashed.append(True)
            raise OSError("injected crash in committed-token release")
        return real_delete(*args, **kwargs)

    monkeypatch.setattr(qdrant, "delete_payload", crash_the_release)
    crash = client.post(
        f"/v1/episodic/{memory.object_id}/retract",
        headers=_headers(valid_token, key=key),
        json=_body(expected_version=memory.version),
    )
    assert crash.status_code >= 500, f"expected a crash, got {crash.status_code}: {crash.text}"
    assert crashed, "the release delete_payload was never reached; setup proves nothing"
    monkeypatch.setattr(qdrant, "delete_payload", real_delete)

    row = _anchor_row(qdrant, memory.object_id)
    payload = row["payload"]
    assert payload["state"] == "archived"
    assert payload["importance"] == 1
    assert "retraction_evidence" in payload
    held = payload.get("update_lease_token")
    assert isinstance(held, str) and held.startswith("done:"), (
        f"crash did not leave a committed token: {held!r}"
    )

    # Age the held token so recovery may adopt it. Same token identity, older
    # issue time -- the crash is simply long enough ago to be unattributable to
    # a live writer.
    aged = f"done:1:{held.split(':')[2]}"
    assert is_expired_done_token(aged), "aged token is not adoptable; setup proves nothing"
    qdrant.set_payload(
        collection_name="musubi_episodic",
        payload={"update_lease_token": aged},
        points=[row["id"]],
        wait=True,
    )
    return memory.object_id, int(payload["version"]), {"seed_version": memory.version}


def test_adopted_release_answers_with_saga_version_not_concurrent_writers(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "race-adopt"
    object_id, committed_version, meta = _crash_committed_retraction(
        client,
        valid_token,
        episodic,
        qdrant,
        monkeypatch,
        content="The east span is out tonight. This is false.",
        key=key,
    )
    intruder_version = committed_version + 7
    landed: list[int] = []
    real_delete = qdrant.delete_payload

    def delete_then_intrude(*args: object, **kwargs: object) -> object:
        outcome = real_delete(*args, **kwargs)
        if not landed:
            row = _anchor_row(qdrant, object_id)
            qdrant.set_payload(
                collection_name="musubi_episodic",
                payload={"version": intruder_version},
                points=[row["id"]],
                wait=True,
            )
            landed.append(intruder_version)
        return outcome

    monkeypatch.setattr(qdrant, "delete_payload", delete_then_intrude)
    replay = client.post(
        f"/v1/episodic/{object_id}/retract",
        headers=_headers(valid_token, key=key),
        json=_body(expected_version=meta["seed_version"]),
    )
    assert replay.status_code == 200, replay.text

    # The plant must have landed, or this cell measures nothing.
    assert landed == [intruder_version], "intruder PATCH never ran; cell is INERT"
    assert int(_anchor_row(qdrant, object_id)["payload"]["version"]) == intruder_version

    assert int(replay.json()["version"]) == committed_version, (
        "the adopted-token release answered with the concurrent writer's version "
        f"({replay.json()['version']}) instead of the retraction's committed version "
        f"({committed_version}); receipt replay is no longer byte-identical"
    )


def test_adopted_release_control_no_intruder_answers_with_committed_version(
    client: TestClient,
    valid_token: str,
    episodic: EpisodicPlane,
    qdrant: QdrantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the same adoption path with nothing racing it. Proves the
    scenario really reaches ``_release_adopted_done_token`` and answers 200, so
    a red in the cell above is the race and not the setup."""
    key = "control-adopt"
    object_id, committed_version, meta = _crash_committed_retraction(
        client,
        valid_token,
        episodic,
        qdrant,
        monkeypatch,
        content="The west span is out tonight. This is false.",
        key=key,
    )
    replay = client.post(
        f"/v1/episodic/{object_id}/retract",
        headers=_headers(valid_token, key=key),
        json=_body(expected_version=meta["seed_version"]),
    )
    assert replay.status_code == 200, replay.text
    assert int(replay.json()["version"]) == committed_version
    assert _anchor_row(qdrant, object_id)["payload"].get("update_lease_token") is None
