
"""Only module that touches SQLite; single writer thread.

All queries are parameterised and foreign keys are enforced. Access is serialised
through one lock, so there is only ever one writer. Chat messages are saved by a
background writer thread, so delivering a message never waits for the disk
(SAD: forward first, then persist).
"""

import os
import queue
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")

log = logging.getLogger(__name__)


class DuplicateError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Repository:
    def __init__(self, db_path):
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

        self._db = sqlite3.connect(
            db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()

        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.executescript(
                SCHEMA.read_text(encoding="utf-8")
            )

    def close(self):
        """Finish any queued writes, then close the database."""
        self._writes.put(None)
        self._writer.join(10)
        with self._lock:
            self._db.close()

    def _write_loop(self):
        while True:
            job = self._writes.get()
            try:
                if job is None:
                    return
                sql, params = job
                with self._lock:
                    self._db.execute(sql, params)
            except sqlite3.Error:
                # A failed write must not stop the writer or the server (CHAT-NF-006).
                log.exception("could not save to the database")
            finally:
                self._writes.task_done()

    def flush(self):
        """Wait until every queued write has been saved."""
        self._writes.join()

    def create_user(self, username, password_hash, salt, role="USER"):
        try:
            with self._lock:
                cur = self._db.execute(
                    """
                    INSERT INTO User
                    (Username, PasswordHash, Salt, Role, CreatedAt)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (username, password_hash, salt, role, now()),
                )
        except sqlite3.IntegrityError as e:
            raise DuplicateError(username) from e

        return cur.lastrowid

    def get_user(self, username):
        with self._lock:
            row = self._db.execute(
                """
                SELECT UserID, Username, PasswordHash, Salt, Role, Status
                FROM User
                WHERE Username = ?
                """,
                (username,),
            ).fetchone()

        return dict(row) if row else None

    # ---------------------------------------------------------
    # SESSION MANAGEMENT
    # ---------------------------------------------------------

    def create_session(self, user_id, token_hash):
        """Create a session and store only the token hash."""
        timestamp = now()

        with self._lock:
            cur = self._db.execute(
                """
                INSERT INTO Session
                (UserID, TokenHash, ConnectedAt, LastActivity)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, token_hash, timestamp, timestamp),
            )

        return cur.lastrowid

    def end_session(self, session_id):
        """Mark a session as disconnected."""
        with self._lock:
            self._db.execute(
                """
                UPDATE Session
                SET DisconnectedAt = ?
                WHERE SessionID = ?
                  AND DisconnectedAt IS NULL
                """,
                (now(), session_id),
            )

    def refresh_session_activity(self, session_id, idle_timeout_seconds):
        """
        Update LastActivity only if the session is still active
        and has not exceeded its idle timeout.

        Returns True if the session is valid; otherwise False.
        """
        current_time = datetime.now(timezone.utc)

        cutoff = (
            current_time - timedelta(seconds=idle_timeout_seconds)
        ).isoformat(timespec="milliseconds")

        timestamp = current_time.isoformat(timespec="milliseconds")

        with self._lock:
            cur = self._db.execute(
                """
                UPDATE Session
                SET LastActivity = ?
                WHERE SessionID = ?
                  AND DisconnectedAt IS NULL
                  AND LastActivity >= ?
                """,
                (timestamp, session_id, cutoff),
            )

        return cur.rowcount == 1

    def expire_idle_sessions(self, idle_timeout_seconds):
        """
        Mark all sessions that have exceeded the configured idle timeout
        as disconnected.

        Returns the number of sessions expired.
        """
        current_time = datetime.now(timezone.utc)

        cutoff = (
            current_time - timedelta(seconds=idle_timeout_seconds)
        ).isoformat(timespec="milliseconds")

        timestamp = current_time.isoformat(timespec="milliseconds")

        with self._lock:
            cur = self._db.execute(
                """
                UPDATE Session
                SET DisconnectedAt = ?
                WHERE DisconnectedAt IS NULL
                  AND LastActivity < ?
                """,
                (timestamp, cutoff),
            )

        return cur.rowcount

    def expire_idle_sessions_with_ids(self, idle_timeout_seconds):
        """
        Expire inactive sessions and return their session IDs.

        The selection and update are performed under the same lock
        to prevent concurrent activity refreshes from racing with expiry.
        """
        current_time = datetime.now(timezone.utc)

        cutoff = (
            current_time - timedelta(seconds=idle_timeout_seconds)
        ).isoformat(timespec="milliseconds")

        timestamp = current_time.isoformat(timespec="milliseconds")

        with self._lock:
            rows = self._db.execute(
                """
                SELECT SessionID
                FROM Session
                WHERE DisconnectedAt IS NULL
                  AND LastActivity < ?
                """,
                (cutoff,),
            ).fetchall()

            session_ids = [row["SessionID"] for row in rows]

            if session_ids:
                self._db.execute(
                    """
                    UPDATE Session
                    SET DisconnectedAt = ?
                    WHERE DisconnectedAt IS NULL
                      AND LastActivity < ?
                    """,
                    (timestamp, cutoff),
                )

        return session_ids
