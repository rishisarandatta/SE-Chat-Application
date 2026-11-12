
"""Wires the server layers together."""

import threading

from server.persistence.repository import Repository
from server.protocol.dispatcher import Dispatcher
from server.services.auth import AuthService
from server.services.presence import Presence
from server.services.rooms import RoomService
from server.services.router import Router
from server.services.sessions import SessionService
from server.transport.connection_manager import ConnectionManager


class ChatServer:
    def __init__(self, config):
        self.config = config
        self.repo = Repository(config["db_path"])
        self.presence = Presence()
        self.rooms = RoomService(self.repo, self.presence, config["max_rooms"])

        # Session service with configured idle timeout.
        self.sessions = SessionService(
            self.repo,
            config["idle_timeout_seconds"],
        )

        self.dispatcher = Dispatcher(
            config,
            auth=AuthService(self.repo),
            sessions=self.sessions,
            presence=self.presence,
            router=Router(self.presence, self.repo, self.rooms),
            rooms=self.rooms,
        )

        self.connections = ConnectionManager(
            config,
            self.dispatcher.handle,
            self.dispatcher.disconnected,
        )

        self._thread = None

        # Background session-expiry worker.
        self._expiry_stop = threading.Event()
        self._expiry_thread = None

    def serve_forever(self):
        self.connections.serve_forever()

    def _expire_sessions_loop(self):
        """Periodically expire inactive sessions and close their connections."""
        timeout = self.config["idle_timeout_seconds"]
        interval = min(30, max(1, timeout // 4))

        while not self._expiry_stop.wait(interval):
            expired_session_ids = (
                self.sessions.expire_idle_sessions_with_ids()
            )

            self.connections.close_expired_sessions(expired_session_ids)

    def start(self):
        """Start the server and session-expiry worker."""
        self._thread = threading.Thread(
            target=self.serve_forever,
            name="accept",
            daemon=True,
        )
        self._thread.start()

        if not self.connections.ready.wait(5):
            raise RuntimeError("server did not start")

        self._expiry_thread = threading.Thread(
            target=self._expire_sessions_loop,
            name="session-expiry",
            daemon=True,
        )
        self._expiry_thread.start()

        return self.connections.address

    def stop(self):
        """Stop the server and background workers."""
        self._expiry_stop.set()

        if self._expiry_thread is not None:
            self._expiry_thread.join(5)

        self.connections.stop()

        if self._thread is not None:
            self._thread.join(5)

        self.repo.close()
