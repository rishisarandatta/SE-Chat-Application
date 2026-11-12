
"""Only module that touches SQLite; single writer thread.

All queries are parameterised and foreign keys are enforced. Access is serialised
through one lock, so there is only ever one writer. Message writes move to a
dedicated writer thread when history is persisted (#9).
"""

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


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
        with self._lock:
            self._db.close()

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

    # ----- rooms (#8) -----

    def create_room(self, name, created_by):
        """Create a room and make its creator a member, in one transaction."""
        ts = now()
        try:
            with self._lock:
                self._db.execute("BEGIN")
                try:
                    cur = self._db.execute(
                        "INSERT INTO ChatRoom (RoomName, CreatedBy, CreatedAt) VALUES (?, ?, ?)",
                        (name, created_by, ts))
                    self._db.execute(
                        "INSERT INTO RoomMember (RoomID, UserID, JoinedAt) VALUES (?, ?, ?)",
                        (cur.lastrowid, created_by, ts))
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
        except sqlite3.IntegrityError as e:
            raise DuplicateError(name) from e
        return cur.lastrowid

    def get_room(self, name):
        with self._lock:
            row = self._db.execute(
                "SELECT RoomID, RoomName, CreatedBy, Status FROM ChatRoom WHERE RoomName = ?",
                (name,)).fetchone()
        return dict(row) if row else None

    def count_rooms(self):
        """Rooms that still count towards max_rooms (everything not removed)."""
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM ChatRoom WHERE Status != 'REMOVED'").fetchone()[0]

    def list_rooms(self, user_id):
        """Every room that has not been removed, with its member count and whether user_id is a member."""
        with self._lock:
            rows = self._db.execute(
                """SELECT r.RoomName, r.Status,
                          (SELECT COUNT(*) FROM RoomMember m WHERE m.RoomID = r.RoomID) AS Members,
                          EXISTS (SELECT 1 FROM RoomMember m
                                  WHERE m.RoomID = r.RoomID AND m.UserID = ?) AS Joined
                   FROM ChatRoom r WHERE r.Status != 'REMOVED' ORDER BY r.RoomName COLLATE NOCASE""",
                (user_id,)).fetchall()
        return [{"name": r["RoomName"], "status": r["Status"], "members": r["Members"],
                 "joined": bool(r["Joined"])} for r in rows]

    def add_member(self, room_id, user_id):
        """Returns False if the user was already a member."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO RoomMember (RoomID, UserID, JoinedAt) VALUES (?, ?, ?)",
                (room_id, user_id, now()))
        return cur.rowcount == 1

    def remove_member(self, room_id, user_id):
        """Returns False if the user was not a member."""
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM RoomMember WHERE RoomID = ? AND UserID = ?", (room_id, user_id))
        return cur.rowcount == 1

    def is_member(self, room_id, user_id):
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM RoomMember WHERE RoomID = ? AND UserID = ?",
                (room_id, user_id)).fetchone() is not None

    def member_names(self, room_id):
        with self._lock:
            rows = self._db.execute(
                """SELECT u.Username FROM RoomMember m JOIN User u ON u.UserID = m.UserID
                   WHERE m.RoomID = ? ORDER BY u.Username COLLATE NOCASE""",
                (room_id,)).fetchall()
        return [r["Username"] for r in rows]
