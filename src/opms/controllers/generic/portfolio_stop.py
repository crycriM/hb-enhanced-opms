"""Cross-process quote stop for the two-account perp basket.

Both Hummingbot instances must use the same local SQLite path and explicit
member list. Missing/stale peers cancel quotes but never cause guessed trades.
"""

import math
import os
import sqlite3
import time

from mm_core.inventory import usdc_flat_targets


class PortfolioStopBook:
    def __init__(self, path: str, members: set[tuple[str, str]], stale_s: float = 20.0):
        if not path or not os.path.isabs(path):
            raise ValueError("portfolio stop database path must be absolute")
        if len(members) < 2 or len({account for account, _ in members}) < 2:
            raise ValueError("portfolio stop requires legs from at least two accounts")
        if not math.isfinite(stale_s) or stale_s <= 0:
            raise ValueError("stale_s must be finite and positive")
        self.path = path
        self.members = members
        self._member_names = {f"{account}:{coin}" for account, coin in members}
        self.stale_s = stale_s
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with sqlite3.connect(path, timeout=5) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS legs (
                member TEXT PRIMARY KEY, ts REAL NOT NULL, position REAL NOT NULL,
                mid REAL NOT NULL, mode TEXT NOT NULL)""")

    def update(
        self, account: str, coin: str, position: float, mid: float, mode: str,
        now: float | None = None,
    ) -> tuple[str, float]:
        """Return (basket mode, this leg's reduce-only target base position)."""
        key = (account, coin)
        if key not in self.members or mode not in {"quote", "stop", "emergency"}:
            raise ValueError("unknown portfolio leg or mode")
        if not math.isfinite(position) or not math.isfinite(mid) or mid <= 0:
            raise ValueError("invalid portfolio position or mid")
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("invalid portfolio timestamp")
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute(
                "INSERT INTO legs VALUES (?, ?, ?, ?, ?) ON CONFLICT(member) DO UPDATE SET "
                "ts=excluded.ts, position=excluded.position, mid=excluded.mid, mode=excluded.mode",
                (f"{account}:{coin}", now, position, mid, mode),
            )
            rows = db.execute("SELECT member, ts, position, mid, mode FROM legs").fetchall()
        fresh = {member: (pos, price, state) for member, ts, pos, price, state in rows
                 if now - self.stale_s <= ts <= now + 1 and
                 member in self._member_names}
        if any(state == "emergency" for _, _, state in fresh.values()):
            return "emergency", 0.0
        if len(fresh) != len(self.members):
            return "hold", position
        if not any(state == "stop" for _, _, state in fresh.values()):
            return "quote", position
        legs = {(acct, symbol): fresh[f"{acct}:{symbol}"][:2]
                for acct, symbol in self.members}
        return "stop", usdc_flat_targets(legs)[key]
