from pathlib import Path

import pytest

from exleasingcar_archive.collector import build_argument_parser
from exleasingcar_archive.process_lock import (
    CollectorAlreadyRunning,
    CollectorProcessLock,
)


def test_process_lock_is_exclusive_and_released(tmp_path: Path):
    lock_path = tmp_path / "nested" / "collector.lock"

    with CollectorProcessLock(lock_path):
        assert lock_path.is_file()
        with pytest.raises(CollectorAlreadyRunning, match="already holds the lock"):
            with CollectorProcessLock(lock_path):
                pytest.fail("a second collector must not acquire the lock")

    # The OS lock is released but the lock file is deliberately retained.
    assert lock_path.is_file()
    with CollectorProcessLock(lock_path):
        pass


def test_process_lock_rejects_double_acquire_on_same_instance(tmp_path: Path):
    lock = CollectorProcessLock(tmp_path / "collector.lock")

    with lock:
        with pytest.raises(RuntimeError, match="already acquired"):
            lock.acquire()


def test_collector_cli_accepts_an_explicit_lock_file(tmp_path: Path):
    lock_path = tmp_path / "runtime" / "collector.lock"

    args = build_argument_parser().parse_args(["--lock-file", str(lock_path)])

    assert args.lock_file == lock_path
