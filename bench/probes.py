"""probes.py — plant retrievable facts; check recall at increasing lag."""

import random
import string
import re
import sqlite3


def _nonce(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


class ProbeSet:
    """Plant retrievable facts; check recall at increasing lag.

    Lags: probe is queried at turn (planted_turn + 30), (planted_turn + 130),
    (planted_turn + 280).
    """

    LAGS = (30, 130, 280)

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.planted = {}       # probe_id -> (turn, nonce)
        self.schedule = {}      # turn -> [probe_id, ...]

    def plant(self, turn: int) -> tuple[str, str]:
        """Create a new probe and schedule its recall queries.

        Returns (probe_id, message_to_inject) where message_to_inject is a
        user message the agent should receive at this turn.
        """
        pid = f"p{len(self.planted):03d}"
        nonce = _nonce()
        self.planted[pid] = (turn, nonce)
        for lag in self.LAGS:
            self.schedule.setdefault(turn + lag, []).append(pid)
        msg = (
            f"Record this project constant, you will need it later. "
            f"BUILD_TOKEN_{pid} = {nonce}. Acknowledge and continue."
        )
        return pid, msg

    def due(self, turn: int) -> list[str]:
        """Return probe ids whose recall is due at this turn."""
        return self.schedule.get(turn, [])

    def question(self, pid: str) -> str:
        """Return the recall question for a probe."""
        return (
            f"What is the value of BUILD_TOKEN_{pid}? "
            f"Answer with the value only, nothing else."
        )

    def score(self, pid: str, answer: str) -> bool:
        """Deterministic substring match — no grader model needed."""
        _, nonce = self.planted[pid]
        return bool(re.search(rf"\b{nonce}\b", answer or "", re.I))


def record_probe(
    con: sqlite3.Connection,
    run_id: str,
    arm: str,
    probe_id: str,
    probes: ProbeSet,
    asked_turn: int,
    answer: str,
):
    planted_turn, _ = probes.planted[probe_id]
    lag = asked_turn - planted_turn
    passed = 1 if probes.score(probe_id, answer) else 0
    con.execute(
        """INSERT OR REPLACE INTO probe_result
           (run_id, arm, probe_id, planted_turn, asked_turn, lag, passed, answer)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, arm, probe_id, planted_turn, asked_turn, lag, passed, answer),
    )
    con.commit()
