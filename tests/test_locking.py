import json
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from thread_to_skill import locking
from thread_to_skill.cli import main
from thread_to_skill.config import Config

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SLACK = FIXTURES / "slack-export"

DEAD_PID = 999999999


def iso(seconds_ago: int = 0) -> str:
    when = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class LockingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / ".thread2skill"
        self.lock = self.home / ".lock"

    def tearDown(self):
        self.temp.cleanup()

    def hold(self, pid: int, seconds_ago: int = 0, host: str | None = None) -> dict:
        """Write a lock file as if another process had taken it."""
        record = {"pid": pid, "hostname": host or socket.gethostname(), "started_at": iso(seconds_ago)}
        self.home.mkdir(parents=True, exist_ok=True)
        self.lock.write_text(json.dumps(record), encoding="utf-8")
        return record

    def current(self) -> dict:
        return json.loads(self.lock.read_text(encoding="utf-8"))

    def test_second_acquire_conflicts_and_names_the_holder(self):
        held = locking.acquire(self.lock, wait=0)
        with self.assertRaises(locking.LockConflict) as caught:
            locking.acquire(self.lock, wait=0)
        message = str(caught.exception)
        self.assertIn(str(held["pid"]), message)
        self.assertIn(held["hostname"], message)
        self.assertEqual(self.current()["pid"], held["pid"])

    def test_dead_pid_on_the_same_host_is_reclaimed(self):
        if locking.pid_is_alive(DEAD_PID):
            self.skipTest(f"pid {DEAD_PID} is live on this machine")
        self.hold(DEAD_PID)
        taken = locking.acquire(self.lock, wait=0)
        self.assertEqual(taken["pid"], os.getpid())
        self.assertEqual(self.current()["pid"], os.getpid())

    def test_lock_older_than_the_threshold_is_reclaimed(self):
        self.hold(os.getpid(), seconds_ago=int(locking.STALE_AFTER_SECONDS) + 60)
        taken = locking.acquire(self.lock, wait=0)
        self.assertEqual(taken["pid"], os.getpid())
        self.assertFalse(locking.is_stale(self.current(), self.lock))

    def test_another_host_is_left_alone_while_it_is_young(self):
        self.hold(os.getpid(), host="other-host")
        self.assertFalse(locking.is_stale(self.current(), self.lock))
        with self.assertRaises(locking.LockConflict):
            locking.acquire(self.lock, wait=0)

    def test_wait_zero_fails_without_blocking(self):
        self.hold(os.getpid())
        started = time.monotonic()
        with self.assertRaises(locking.LockConflict) as caught:
            locking.acquire(self.lock, wait=0)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("without waiting", str(caught.exception))
        self.assertTrue(self.lock.exists())

    def test_wait_succeeds_once_the_holder_releases(self):
        held = locking.acquire(self.lock, wait=0)

        def release_later():
            time.sleep(0.4)
            locking.release(self.lock, held)

        threading.Thread(target=release_later).start()
        taken = locking.acquire(self.lock, wait=5)
        self.assertEqual(taken["pid"], os.getpid())
        self.assertEqual(self.current()["pid"], os.getpid())

    def test_release_removes_the_lock_file(self):
        held = locking.acquire(self.lock, wait=0)
        self.assertTrue(self.lock.exists())
        self.assertTrue(locking.release(self.lock, held))
        self.assertFalse(self.lock.exists())

    def test_release_never_deletes_another_processs_lock(self):
        held = locking.acquire(self.lock, wait=0)
        other = self.hold(DEAD_PID, seconds_ago=1)
        self.assertFalse(locking.release(self.lock, held))
        self.assertEqual(self.current()["pid"], other["pid"])

    def test_release_after_an_error_inside_the_context_manager(self):
        with self.assertRaises(ValueError):
            with locking.workspace_lock(self.lock, wait=0):
                raise ValueError("distilling failed")
        self.assertFalse(self.lock.exists())

    def test_acquire_creates_the_workspace_when_it_is_missing(self):
        self.assertFalse(self.home.exists())
        held = locking.acquire(self.lock, wait=0)
        self.assertTrue(self.home.is_dir())
        self.assertEqual(self.current()["pid"], held["pid"])

    def test_a_half_written_lock_is_reported_and_left_alone(self):
        self.home.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("{not json", encoding="utf-8")
        with self.assertRaises(locking.LockConflict) as caught:
            locking.acquire(self.lock, wait=0)
        self.assertIn("could not be read", str(caught.exception))
        self.assertEqual(self.lock.read_text(encoding="utf-8"), "{not json")

    def test_config_lock_path_is_the_workspace_lock_file(self):
        config = Config.create(self.home)
        self.assertEqual(config.lock_path.name, ".lock")
        self.assertEqual(config.lock_path.parent, config.home)
        self.assertEqual(config.lock_path, config.home / ".lock")

    def test_pid_probe_reports_live_and_dead_pids_without_signalling(self):
        # On Windows os.kill(pid, 0) sends CTRL_C_EVENT, so a naive probe
        # interrupts this process instead of testing it.
        self.assertTrue(locking.pid_is_alive(os.getpid()))
        self.assertFalse(locking.pid_is_alive(DEAD_PID))
        self.assertFalse(locking.pid_is_alive(0))
        self.assertFalse(locking.pid_is_alive(-1))
        self.assertFalse(locking.pid_is_alive("not-a-pid"))
        self.assertFalse(locking.pid_is_alive(None))

    def run_cli(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--home", str(self.home), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_read_only_command_runs_while_the_lock_is_held(self):
        self.assertEqual(self.run_cli("init", "--provider", "offline")[0], 0)
        held = locking.acquire(self.lock, wait=0)
        for args in (
            ("list", "--json"),
            ("search", "anything", "--json"),
            ("suggest", "anything", "--format", "json"),
            ("stats", "--json"),
        ):
            code, output, _ = self.run_cli("--wait", "0", *args)
            self.assertEqual(code, 0)
            json.loads(output)
        # `show` fails on the unknown skill, not on the lock.
        code, output, _ = self.run_cli("--wait", "0", "show", "anything")
        self.assertEqual(code, 1)
        self.assertIn("Unknown skill", output)
        self.assertEqual(self.current()["pid"], held["pid"])

    def test_write_command_reports_the_conflict_and_exits_non_zero(self):
        self.assertEqual(self.run_cli("init", "--provider", "offline")[0], 0)
        held = locking.acquire(self.lock, wait=0)
        code, output, _ = self.run_cli("--wait", "0", "ingest", str(SLACK))
        self.assertEqual(code, 1)
        self.assertIn(str(held["pid"]), output)
        self.assertIn(held["hostname"], output)
        self.assertEqual(self.current()["pid"], held["pid"])

    def test_write_command_releases_the_lock_when_it_fails(self):
        self.assertEqual(self.run_cli("init", "--provider", "offline")[0], 0)
        code, _, _ = self.run_cli("--wait", "0", "delete", "no-such-skill", "--yes")
        self.assertEqual(code, 1)
        self.assertFalse(self.lock.exists())


if __name__ == "__main__":
    unittest.main()
