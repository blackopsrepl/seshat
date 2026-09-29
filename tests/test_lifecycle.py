from __future__ import annotations

import signal
import unittest
from unittest.mock import patch

from seshat import lifecycle


class ScriptedProcess:
    def __init__(self, *, returncode: int | None = None, exits: dict[int, int] | None = None) -> None:
        self.pid = 424242
        self.returncode = returncode
        self.exits = exits or {}
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.returncode

    def receive(self, sig: int) -> None:
        self.signals.append(sig)
        if sig in self.exits:
            self.returncode = self.exits[sig]


class TerminationTests(unittest.TestCase):
    def terminate(self, process: ScriptedProcess) -> lifecycle.TerminationOutcome:
        now = 99.0

        def monotonic() -> float:
            nonlocal now
            now += 1.0
            return now

        def killpg(pid: int, sig: int) -> None:
            self.assertEqual(pid, process.pid)
            process.receive(sig)

        with patch.object(lifecycle.os, "killpg", side_effect=killpg):
            return lifecycle.terminate_process_group(
                process,
                timeouts=((signal.SIGINT, 0.0), (signal.SIGTERM, 0.0), (signal.SIGKILL, 0.0)),
                monotonic=monotonic,
                sleep=lambda _seconds: None,
            )

    def test_already_exited_is_natural(self) -> None:
        process = ScriptedProcess(returncode=0)
        outcome = self.terminate(process)
        self.assertEqual(outcome.stage, "natural")
        self.assertEqual(outcome.returncode, 0)
        self.assertEqual(process.signals, [])

    def test_sigint_exit_is_reported_exactly(self) -> None:
        process = ScriptedProcess(exits={signal.SIGINT: 0})
        outcome = self.terminate(process)
        self.assertEqual(outcome.stage, "sigint")
        self.assertEqual(outcome.returncode, 0)
        self.assertEqual(process.signals, [signal.SIGINT])

    def test_sigterm_zero_exit_is_still_a_graceful_recorder_exit(self) -> None:
        process = ScriptedProcess(exits={signal.SIGTERM: 0})
        outcome = self.terminate(process)
        self.assertEqual(outcome.stage, "sigterm")
        self.assertEqual(outcome.returncode, 0)
        self.assertEqual(process.signals, [signal.SIGINT, signal.SIGTERM])
        self.assertTrue(outcome.graceful)

    def test_sigkill_is_an_abnormal_outcome(self) -> None:
        process = ScriptedProcess(exits={signal.SIGKILL: -signal.SIGKILL})
        outcome = self.terminate(process)
        self.assertEqual(outcome.stage, "sigkill")
        self.assertEqual(outcome.returncode, -signal.SIGKILL)
        self.assertEqual(process.signals, [signal.SIGINT, signal.SIGTERM, signal.SIGKILL])
        self.assertFalse(outcome.graceful)

    def test_nonzero_sigint_exit_is_preserved(self) -> None:
        process = ScriptedProcess(exits={signal.SIGINT: 7})
        outcome = self.terminate(process)
        self.assertEqual(outcome.stage, "sigint")
        self.assertEqual(outcome.returncode, 7)
        self.assertFalse(outcome.graceful)


if __name__ == "__main__":
    unittest.main()
