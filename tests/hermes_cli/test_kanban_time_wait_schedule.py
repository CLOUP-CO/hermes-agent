"""A time-wait block parks in ``scheduled``; it never loops block -> triage -> specify -> respawn."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.prompt_builder import KANBAN_GUIDANCE
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

FUTURE = "SCHEDULED_UNTIL=2999-01-01T00:00:00Z medir 24 h de datos"
PAST = "SCHEDULED_UNTIL=2000-01-01T00:00:00Z medir 24 h de datos"
WAIT = FUTURE


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


def test_time_wait_closes_run_as_scheduled_without_block_hook(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fired: list[str] = []
    monkeypatch.setattr(kb, "_fire_task_hook", lambda name, *a, **k: fired.append(name))
    with kbc.connect_closing() as conn:
        tid = _new_running(conn)
        assert kb.block_task(conn, tid, reason=WAIT)
        outcome = conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()[0]
        assert outcome == "scheduled"
        assert "kanban_task_blocked" not in fired


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("needs_input", "Wait until the user provides the API credentials"),
        ("needs_input", "esperar hasta que una persona apruebe"),
        ("capability", "Falta credencial; esperar hasta que la entreguen"),
        ("needs_input", "SCHEDULED_UNTIL=manana-temprano"),
        ("needs_input", PAST),
        ("needs_input", "SCHEDULED_UNTIL=2999-01-01T00:00:00 sin zona"),
    ],
)
def test_human_wait_or_unreadable_instant_stays_blocked(kanban_home: Path, kind: str, reason: str) -> None:
    with kbc.connect_closing() as conn:
        tid = _new_running(conn)
        assert kb.block_task(conn, tid, reason=reason, kind=kind)
        assert kb.get_task(conn, tid).status == "blocked"


def test_scheduled_until_future_stays_parked_and_not_claimable(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _new_running(conn)
        assert kb.block_task(conn, tid, reason=FUTURE)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "scheduled"
        assert kb.claim_task(conn, tid, claimer="worker") is None


def _soon() -> str:
    when = datetime.fromtimestamp(time.time() + 60, tz=timezone.utc)
    return f"SCHEDULED_UNTIL={when.isoformat()} medir"


def _advance(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    real = time.time
    monkeypatch.setattr(kb.time, "time", lambda: real() + seconds)


def test_scheduled_until_passed_wakes_to_ready(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _new_running(conn)
        assert kb.block_task(conn, tid, reason=_soon())
        assert kb.get_task(conn, tid).status == "scheduled"
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "scheduled"
        _advance(monkeypatch, 120)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "ready"


def test_kanban_schedule_wakes_when_marker_passes_and_never_for_past_marker(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with kbc.connect_closing() as conn:
        woken = _new_running(conn)
        assert kb.schedule_task(conn, woken, reason=_soon())
        past = _new_running(conn)
        assert kb.schedule_task(conn, past, reason=PAST)
        parked = _new_running(conn)
        assert kb.schedule_task(conn, parked, reason="esperando al proveedor")
        _advance(monkeypatch, 120)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, woken).status == "ready"
        assert kb.get_task(conn, past).status == "scheduled"
        assert kb.get_task(conn, parked).status == "scheduled"


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
    assert "SCHEDULED_UNTIL" in KANBAN_GUIDANCE
