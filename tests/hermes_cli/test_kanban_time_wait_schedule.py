"""A time-wait block parks in ``scheduled``; it never loops block -> triage -> specify -> respawn."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.prompt_builder import KANBAN_GUIDANCE
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

WAIT = "Espera de tiempo: no re-promover antes del 17-nov 01:00 Bogotá. Medir 24 h de datos."


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _run(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _new_running(conn):
    tid = kb.create_task(conn, title="t", assignee="worker")
    _run(conn, tid)
    return tid


def test_time_wait_block_x3_ends_scheduled_never_triage(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _new_running(conn)
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT + 1):
            assert kb.block_task(conn, tid, reason=WAIT, kind="needs_input")
            assert kb.get_task(conn, tid).status == "scheduled"
            assert kb.unblock_task(conn, tid)
            _run(conn, tid)
        assert kb.block_task(conn, tid, reason=WAIT)
        assert kb.get_task(conn, tid).status == "scheduled"
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "block_loop_detected" not in kinds
        assert kinds.count("scheduled") >= 3


def test_scheduled_time_wait_is_not_dispatchable(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _new_running(conn)
        assert kb.block_task(conn, tid, reason=WAIT)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "scheduled"
        assert kb.claim_task(conn, tid, claimer="worker") is None


def test_non_time_reason_still_hits_loop_breaker(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _new_running(conn)
        assert kb.block_task(conn, tid, reason="falta credencial", kind="capability")
        assert kb.unblock_task(conn, tid)
        _run(conn, tid)
        assert kb.block_task(conn, tid, reason="falta credencial", kind="capability")
        assert kb.get_task(conn, tid).status == "triage"


def test_worker_prompt_points_time_waits_to_schedule() -> None:
    assert "kanban_schedule" in KANBAN_GUIDANCE
