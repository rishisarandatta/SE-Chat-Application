
"""Secure session tokens and idle-session expiry."""

import hashlib
import hmac
import secrets
from typing import NamedTuple


class Session(NamedTuple):
    id: int
    token: str


class SessionService:
    def __init__(self, repo, idle_timeout_seconds=900):
        self.repo = repo
        self.idle_timeout_seconds = idle_timeout_seconds

    def start(self, user_id):
        """Create a new session with a cryptographically secure token."""
        token = secrets.token_urlsafe(32)

        # Store only the SHA-256 hash of the token.
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        session_id = self.repo.create_session(user_id, token_hash)

        return Session(session_id, token)

    def end(self, session):
        """End an active session."""
        if session is not None:
            self.repo.end_session(session.id)

    @staticmethod
    def matches(session, presented_token):
        """Compare session tokens safely."""
        return (
            session is not None
            and isinstance(presented_token, str)
            and hmac.compare_digest(
                session.token.encode(),
                presented_token.encode(),
            )
        )

    def validate(self, session, presented_token):
        """
        Validate the token and refresh activity.

        An expired or disconnected session is rejected.
        """
        if not self.matches(session, presented_token):
            return False

        return self.repo.refresh_session_activity(
            session.id,
            self.idle_timeout_seconds,
        )

    def expire_idle_sessions(self):
        """Expire sessions that have exceeded the idle timeout."""
        return self.repo.expire_idle_sessions(
            self.idle_timeout_seconds
        )
