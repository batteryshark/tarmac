"""The poll loop: one thread, a wake event, per-source locks, notifications.

Every enabled source polls on the tick (default 300s). A refresh request
adds its source to the pending set and sets the wake event; the caller can
wait on a per-request completion event. Per-source non-blocking locks make a
concurrent scheduled poll and a refresh share one adapter run — the Claude
PTY and the Codex app server are never double-spawned.

Notifications fire on TRANSITIONS only: a source whose ``needs_login`` flips
0→1 gets one macOS notification, and 1→0 gets an all-clear. One notification
per actual expiry, not per poll.
"""
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from tarmac import providers, store

POLL_INTERVAL_S = 300.0
PRUNE_EVERY_S = 86400.0


def notify(title: str, text: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{text}" with title "{title}"'],
            capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass  # a missed notification never costs the reading


class Poller:
    def __init__(self, db_path=None, *, interval: float = POLL_INTERVAL_S,
                 notifier=notify):
        self.db_path = db_path
        self.interval = interval
        self.notifier = notifier
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._done: dict[str, list[threading.Event]] = defaultdict(list)
        self._last_prune = 0.0
        self.last_reader_at = 0.0
        self._thread: threading.Thread | None = None

    # --- refresh queue ------------------------------------------------------

    def request_refresh(self, source_id: str) -> threading.Event:
        """Queue a poll of one source; returns an event set when it lands."""
        done = threading.Event()
        with self._pending_lock:
            self._pending.add(source_id)
            self._done[source_id].append(done)
        self.wake.set()
        return done

    def _take_pending(self) -> set[str]:
        with self._pending_lock:
            taken, self._pending = self._pending, set()
        return taken

    def _complete(self, source_id: str) -> None:
        with self._pending_lock:
            waiters = self._done.pop(source_id, [])
        for event in waiters:
            event.set()

    # --- demand --------------------------------------------------------------

    def note_reader(self) -> None:
        """An API read or mutation happened. Demand sources may poll while a
        reader was seen within the last interval."""
        self.last_reader_at = time.monotonic()

    def is_demand(self, source) -> bool:
        """``config.demand`` sources cost something on every poll (the Meta
        probe is a generation call), so they only run for a consumer."""
        try:
            return bool(providers.source_config(source).get("demand"))
        except ValueError:
            return False

    # --- polling ------------------------------------------------------------

    def poll_one(self, source) -> dict | None:
        """Poll one source under its lock; skip (None) if already polling.
        Any failure — a vanished source, a bad config, a full disk — lands
        here as None. The loop outlives every poll."""
        lock = self._locks[source["source_id"]]
        if not lock.acquire(blocking=False):
            return None
        try:
            reading = providers.poll_source(source, store.home())
            con = store.connect(self.db_path)
            try:
                previous = store.latest_reading(con, source["source_id"])
                store.record_reading(con, reading)
            finally:
                con.close()
            self._transition(source, previous, reading)
            return reading
        except Exception:  # noqa: BLE001 — one bad poll never kills the loop
            return None
        finally:
            lock.release()
            self._complete(source["source_id"])

    def _transition(self, source, previous, reading: dict) -> None:
        before = bool(previous["needs_login"]) if previous is not None else False
        after = bool(reading.get("needs_login"))
        if after and not before:
            self.notifier(f"tarmac: {source['name']}",
                          reading.get("reason") or "login required")
        elif before and not after:
            self.notifier(f"tarmac: {source['name']}", "login restored")

    def tick(self, source_ids: set[str] | None = None) -> int:
        """Poll the named sources, or every enabled one. Returns the count."""
        con = store.connect(self.db_path)
        try:
            rows = [row for row in store.all_sources(con)
                    if row["enabled"] and not row["archived"]]
        finally:
            con.close()
        if time.monotonic() - self.last_reader_at > self.interval:
            idle = {row["source_id"] for row in rows if self.is_demand(row)}
            rows = [row for row in rows if row["source_id"] not in idle]
        if source_ids is not None:
            rows = [row for row in rows if row["source_id"] in source_ids]
            # A refresh of a disabled source completes immediately: nothing
            # polls, nobody hangs.
            for missing in source_ids - {row["source_id"] for row in rows}:
                self._complete(missing)
        if not rows:
            return 0
        with ThreadPoolExecutor(max_workers=min(4, len(rows))) as pool:
            list(pool.map(self.poll_one, rows))
        return len(rows)

    # --- loop ---------------------------------------------------------------

    def run(self) -> None:
        last_full = 0.0
        while not self.stop_event.is_set():
            try:
                pending = self._take_pending()
                if pending:
                    self.tick(pending)
                if time.monotonic() - last_full >= self.interval:
                    self.tick()
                    last_full = time.monotonic()
                if time.monotonic() - self._last_prune >= PRUNE_EVERY_S:
                    con = store.connect(self.db_path)
                    try:
                        store.prune_raw(con)
                    finally:
                        con.close()
                    self._last_prune = time.monotonic()
            except Exception:  # noqa: BLE001 — the loop outlives every tick
                pass
            self.wake.wait(timeout=min(
                self.interval,
                max(1.0, self.interval - (time.monotonic() - last_full))))
            self.wake.clear()

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="tarmac-poller",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()
