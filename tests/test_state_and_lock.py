from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agent_relay.config import Limits
from agent_relay.state import AlreadyRunning, State, Task, acquire_lock, lock_holder


def test_state_round_trip_and_pruning(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = State.load(path, Limits())
    now = time.time()
    state.processed = {"<new>": "2999-01-01T00:00:00", "<old>": "2000-01-01T00:00:00"}
    state.queue = [Task("<q>", "a@x", "claude: t", "claude", "t", "prompt")]
    state.replies = [now, now - 2 * 86400]
    state.recent = {"fresh": now, "stale": now - 7200}
    state.save()

    loaded = State.load(path, Limits())
    assert loaded.processed == {"<new>": "2999-01-01T00:00:00"}
    assert loaded.queue == state.queue
    assert loaded.replies == [now]
    assert loaded.recent == {"fresh": now}


def test_dry_run_state_writes_nothing(tmp_path: Path) -> None:
    State.load(tmp_path / "state.json", Limits(), dry_run=True).save()
    assert not (tmp_path / "state.json").exists()


def test_second_instance_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "listener.lock"
    with acquire_lock(path):
        assert lock_holder(path) is not None
        with pytest.raises(AlreadyRunning):
            acquire_lock(path)
    assert lock_holder(path) is None


def test_lock_is_released_when_the_holder_is_killed(tmp_path: Path) -> None:
    path = tmp_path / "listener.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
            import time
            from pathlib import Path
            from agent_relay.state import acquire_lock
            lock = acquire_lock(Path({str(path)!r}))
            print("locked", flush=True)
            time.sleep(60)
        """),
        ],
        stdout=subprocess.PIPE,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == b"locked"
    assert lock_holder(path) == str(holder.pid)
    holder.kill()
    holder.wait()
    with acquire_lock(path):
        pass
