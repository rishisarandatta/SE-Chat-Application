"""Accept loop, per-client reader/writer threads, connection cap, cleanup on disconnect."""

import itertools
import logging
import queue
import socket
import ssl
import threading

from server.protocol import messages
from server.protocol.framing import (
    ConnectionClosed,
    FrameError,
    FrameTooLarge,
    discard,
    encode,
    read_frame,
)

log = logging.getLogger(__name__)

UNAUTHENTICATED = "UNAUTHENTICATED"
AUTHENTICATED = "AUTHENTICATED"
CLOSING = "CLOSING"

OUTBOUND_QUEUE_SIZE = 1000
DISCARD_LIMIT = 1 << 20
ACCEPT_POLL_SECONDS = 0.5


class ClientConnection:
    _ids = itertools.count(1)

    def __init__(self, sock, addr):
        self.id = next(self._ids)
        self.sock = sock
        self.addr = addr
        self.state = UNAUTHENTICATED
        self.user = None
        self.session = None
        self.outbound = queue.Queue(maxsize=OUTBOUND_QUEUE_SIZE)
        self._closed = threading.Event()

    def __repr__(self):
        return f"<conn {self.id} {self.addr[0]}:{self.addr[1]} {self.state}>"

    @property
    def authenticated(self):
        return self.state == AUTHENTICATED

    def set_authenticated(self):
        self.state = AUTHENTICATED

    @property
    def closed(self):
        return self._closed.is_set()

    def send(self, message):
        """Queue a message for the writer thread."""
        if self.closed:
            return False

        try:
            self.outbound.put_nowait(message)
            return True
        except queue.Full:
            log.warning("%r outbound queue full; dropping client", self)
            self.close()
            return False

    def wait_closed(self, timeout):
        return self._closed.wait(timeout)

    def close_after_flush(self):
        """Close after queued messages have been sent."""
        self.state = CLOSING

        try:
            self.outbound.put_nowait(None)
        except queue.Full:
            self.close()

    def close(self):
        if self._closed.is_set():
            return

        self._closed.set()
        self.state = CLOSING

        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

        try:
            self.sock.close()
        except OSError:
            pass


class ConnectionManager:
    def __init__(self, config, on_message, on_disconnect):
        self.host = config["host"]
        self.port = config["port"]
        self.tls_cert_path = config["tls_cert_path"]
        self.tls_key_path = config["tls_key_path"]
        self.max_connections = config["max_connections"]
        self.max_frame_bytes = config["max_frame_bytes"]
        self.read_timeout = config["heartbeat_seconds"] * 3

        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._connections = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._listener = None
        self._tls_context = None
        self.address = None
        self.ready = threading.Event()

    def connection_count(self):
        with self._lock:
            return len(self._connections)

    def close_expired_sessions(self, expired_session_ids):
        """Close connections belonging to expired sessions."""
        expired_session_ids = set(expired_session_ids)

        if not expired_session_ids:
            return

        with self._lock:
            connections = [
                conn
                for conn in self._connections.values()
                if conn.session is not None
                and conn.session.id in expired_session_ids
            ]

        for conn in connections:
            conn.close()

    def serve_forever(self):
        """Start a TLS listener and accept client connections."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(
            certfile=self.tls_cert_path,
            keyfile=self.tls_key_path,
        )
        self._tls_context = context

        self._listener = socket.create_server(
            (self.host, self.port),
            backlog=128,
        )
        self._listener.settimeout(ACCEPT_POLL_SECONDS)
        self.address = self._listener.getsockname()[:2]

        log.info("TLS listener ready on %s:%s", *self.address)
        self.ready.set()

        try:
            while not self._stopping.is_set():
                try:
                    sock, addr = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stopping.is_set():
                        break
                    raise

                self._accept(sock, addr)
        finally:
            self._listener.close()

    def stop(self):
        self._stopping.set()

        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass

        with self._lock:
            connections = list(self._connections.values())

        for conn in connections:
            conn.close()

    def _accept(self, sock, addr):
        """Enforce the connection cap and establish TLS for clients."""
        sock.settimeout(self.read_timeout)

        # Check capacity before TLS negotiation.
        with self._lock:
            busy = len(self._connections) >= self.max_connections

        if busy:
            log.warning(
                "connection cap %d reached; rejecting %s:%s",
                self.max_connections,
                *addr[:2],
            )
            try:
                tls_sock = self._tls_context.wrap_socket(
                    sock,
                    server_side=True,
                    do_handshake_on_connect=True,
                )
                tls_sock.sendall(
                    encode(
                        messages.error(
                            None,
                            messages.SERVER_BUSY,
                            "server is full, try again later",
                        )
                    )
                )
            except (OSError, ssl.SSLError):
                pass
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
            return

        try:
            tls_sock = self._tls_context.wrap_socket(
                sock,
                server_side=True,
                do_handshake_on_connect=False,
            )
        except (OSError, ssl.SSLError):
            sock.close()
            log.warning("TLS setup failed for %s:%s", *addr[:2])
            return

        with self._lock:
            # Recheck capacity to handle simultaneous connection attempts.
            if len(self._connections) >= self.max_connections:
                busy = True
                conn = None
            else:
                busy = False
                conn = ClientConnection(tls_sock, addr)
                self._connections[conn.id] = conn

        if busy:
            try:
                tls_sock.do_handshake()
                tls_sock.sendall(
                    encode(
                        messages.error(
                            None,
                            messages.SERVER_BUSY,
                            "server is full, try again later",
                        )
                    )
                )
            except (OSError, ssl.SSLError):
                pass
            finally:
                tls_sock.close()
            return

        log.info("%r connected", conn)

        threading.Thread(
            target=self._reader,
            args=(conn,),
            name=f"reader-{conn.id}",
            daemon=True,
        ).start()

        threading.Thread(
            target=self._writer,
            args=(conn,),
            name=f"writer-{conn.id}",
            daemon=True,
        ).start()

    def _reader(self, conn):
        reason = "closed"

        try:
            conn.sock.do_handshake()

            while not conn.closed:
                try:
                    message = read_frame(
                        conn.sock,
                        self.max_frame_bytes,
                    )
                except FrameTooLarge as exc:
                    log.warning(
                        "%r sent an oversized frame (%d bytes)",
                        conn,
                        exc.length,
                    )
                    conn.send(
                        messages.error(
                            None,
                            messages.INVALID_MESSAGE,
                            str(exc),
                        )
                    )

                    if exc.length > DISCARD_LIMIT:
                        reason = f"oversized frame of {exc.length} bytes"
                        conn.close_after_flush()
                        conn.wait_closed(2)
                        return

                    discard(conn.sock, exc.length)
                    continue

                except FrameError as exc:
                    log.warning(
                        "%r sent a malformed frame: %s",
                        conn,
                        exc,
                    )
                    conn.send(
                        messages.error(
                            None,
                            messages.INVALID_MESSAGE,
                            str(exc),
                        )
                    )
                    continue

                try:
                    self._on_message(conn, message)
                except Exception:
                    log.exception(
                        "error handling message from %r",
                        conn,
                    )

        except ssl.SSLError:
            reason = "TLS handshake or connection failed"
        except (
            ConnectionClosed,
            ConnectionResetError,
            ConnectionAbortedError,
        ):
            reason = "client disconnected"
        except socket.timeout:
            reason = "timed out"
        except OSError as exc:
            reason = (
                "closed"
                if conn.closed
                else f"socket error: {exc}"
            )
        finally:
            self._cleanup(conn, reason)

    def _writer(self, conn):
        while not conn.closed:
            try:
                message = conn.outbound.get(
                    timeout=ACCEPT_POLL_SECONDS
                )
            except queue.Empty:
                continue

            if message is None:
                break

            try:
                conn.sock.sendall(encode(message))
            except (TypeError, ValueError):
                log.exception(
                    "could not encode message for %r",
                    conn,
                )
            except OSError:
                break

        conn.close()

    def _cleanup(self, conn, reason):
        """Remove the connection and notify the dispatcher."""
        conn.close()

        with self._lock:
            removed = self._connections.pop(conn.id, None) is not None

        if removed:
            log.info("%r disconnected (%s)", conn, reason)

            try:
                self._on_disconnect(conn)
            except Exception:
                log.exception("error cleaning up %r", conn)