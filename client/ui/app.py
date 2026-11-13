"""Tkinter chat client: login/registration screen, chat screen and connection status bar.

Tkinter must only be touched from the main thread (SAD risk R8). The network handler
and the connect worker put events on a queue, and _poll drains it every 50 ms.
"""
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from client.net.connection import ChatConnection

POLL_MS = 50
MAX_TEXT_CHARS = 2000

STATUS_COLOURS = {"connected": "#1a7f37", "busy": "#9a6700", "offline": "#cf222e"}

FRIENDLY_ERRORS = {
    "AUTH_FAILED": "Login failed. Check your username and password.",
    "DUPLICATE": "That username is already taken.",
    "SESSION_EXPIRED": "Your session has expired. Please log in again.",
    "SERVER_BUSY": "The server is full. Try again in a moment.",
}


def describe_error(message):
    """Turn an ERROR message into text for the user, without exposing server internals."""
    code = message.get("code")
    return FRIENDLY_ERRORS.get(code) or message.get("reason") or f"Request failed ({code})"


def clock(ts):
    return time.strftime("%H:%M", time.localtime(ts))


class ChatApp:
    def __init__(self, root, host="127.0.0.1", port=5050):
        self.root = root
        self.conn = None
        self.events = queue.Queue()
        self.username = None
        self.on_connected = None      # what to send once the connect worker succeeds
        self.pending_login = None     # credentials to log in with after REGISTER succeeds
        self.logging_out = False
        self.last_seq = {}            # sender -> last seq seen, to check ordering (CHAT-F-008)

        root.title("Chat")
        root.minsize(560, 440)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        self.host_var = tk.StringVar(value=host)
        self.port_var = tk.StringVar(value=str(port))
        self.user_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.to_var = tk.StringVar()
        self.text_var = tk.StringVar()
        self.status_var = tk.StringVar()

        self._build_status_bar()
        self._build_login()
        self._build_chat()
        self.show_login()
        self.set_status("Not connected", "offline")
        root.after(POLL_MS, self._poll)

    # ---------- layout ----------

    def _build_status_bar(self):
        bar = ttk.Frame(self.root, padding=(10, 4))
        bar.pack(side="bottom", fill="x")
        ttk.Separator(self.root).pack(side="bottom", fill="x")
        self.status_dot = tk.Label(bar, text="●", font=("TkDefaultFont", 11))
        self.status_dot.pack(side="left")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left", padx=(4, 0))

    def _build_login(self):
        self.login_frame = frame = ttk.Frame(self.root, padding=24)
        ttk.Label(frame, text="Chat", font=("TkDefaultFont", 16, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 16))
        fields = (("Server", self.host_var, {}), ("Port", self.port_var, {}),
                  ("Username", self.user_var, {}), ("Password", self.password_var, {"show": "•"}))
        self.login_entries = []
        for row, (label, var, options) in enumerate(fields, start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 12))
            entry = ttk.Entry(frame, textvariable=var, width=32, **options)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            entry.bind("<Return>", lambda _e: self.login())
            self.login_entries.append(entry)
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(16, 0))
        self.register_button = ttk.Button(buttons, text="Register", command=self.register)
        self.register_button.pack(side="left", padx=(0, 8))
        self.login_button = ttk.Button(buttons, text="Log in", command=self.login)
        self.login_button.pack(side="left")
        self.login_message = ttk.Label(frame, text="", foreground=STATUS_COLOURS["offline"], wraplength=420)
        self.login_message.grid(row=6, column=0, columnspan=2, sticky="w", pady=(12, 0))
        frame.columnconfigure(1, weight=1)

    def _build_chat(self):
        self.chat_frame = frame = ttk.Frame(self.root, padding=10)

        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Label(top, text="To:").pack(side="left")
        self.to_entry = ttk.Entry(top, textvariable=self.to_var, width=24)
        self.to_entry.pack(side="left", padx=(6, 0))
        self.logout_button = ttk.Button(top, text="Log out", command=self.logout)
        self.logout_button.pack(side="right")

        # Packed before the message log so the input row keeps its space when the window is small.
        bottom = ttk.Frame(frame)
        bottom.pack(side="bottom", fill="x")
        self.message_entry = ttk.Entry(bottom, textvariable=self.text_var)
        self.message_entry.pack(side="left", fill="x", expand=True)
        self.message_entry.bind("<Return>", lambda _e: self.send_message())
        self.send_button = ttk.Button(bottom, text="Send", command=self.send_message)
        self.send_button.pack(side="left", padx=(8, 0))

        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True, pady=8)
        self.log = tk.Text(body, wrap="word", state="disabled", height=12, padx=8, pady=6,
                           relief="solid", borderwidth=1, font=("TkDefaultFont", 10))
        scroll = ttk.Scrollbar(body, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("me", justify="right", foreground="#0b5cad", rmargin=4)
        self.log.tag_configure("them", justify="left", foreground="#1f2328", lmargin1=4, lmargin2=4)
        self.log.tag_configure("meta", font=("TkDefaultFont", 8), foreground="#6e7781")
        self.log.tag_configure("system", justify="center", foreground="#6e7781", font=("TkDefaultFont", 9, "italic"))
        self.log.tag_configure("warning", justify="center", foreground="#9a6700", font=("TkDefaultFont", 9, "italic"))


    # ---------- screens and status ----------

    def show_login(self, message="", colour="offline"):
        self.chat_frame.pack_forget()
        self.login_frame.pack(fill="both", expand=True)
        self.login_message.configure(text=message, foreground=STATUS_COLOURS[colour])
        self._set_login_enabled(True)
        self.root.title("Chat")

    def show_chat(self):
        self.login_frame.pack_forget()
        self.chat_frame.pack(fill="both", expand=True)
        self.root.title(f"Chat — {self.username}")
        self.password_var.set("")
        self.last_seq.clear()
        self._clear_log()
        self.add_line(f"Logged in as {self.username}. Enter a username in To: and start chatting.", "system")
        (self.to_entry if not self.to_var.get() else self.message_entry).focus_set()

    def set_status(self, text, state):
        self.status_var.set(text)
        self.status_dot.configure(foreground=STATUS_COLOURS[state])

    def _set_login_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for widget in (self.login_button, self.register_button, *self.login_entries):
            widget.configure(state=state)

    # ---------- chat log ----------

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def add_line(self, text, tag, meta=None):
        self.log.configure(state="normal")
        if meta:
            self.log.insert("end", meta + "\n", (tag, "meta"))
        self.log.insert("end", text + "\n\n" if meta else text + "\n", tag)
        self.log.configure(state="disabled")
        self.log.see("end")

    # ---------- user actions ----------

    def login(self):
        self._start("LOGIN")

    def register(self):
        self._start("REGISTER")

    def _start(self, action):
        username, password = self.user_var.get().strip(), self.password_var.get()
        if not username or not password:
            self.login_message.configure(text="Enter a username and password.")
            return
        self.login_message.configure(text="")
        credentials = {"username": username, "password": password}
        self.pending_login = credentials if action == "REGISTER" else None

        def send():
            self.conn.request(action, credentials)

        if self.conn is not None and self.conn.connected:
            self._set_login_enabled(False)
            send()
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            self.login_message.configure(text="Port must be a number.")
            return
        host = self.host_var.get().strip() or "127.0.0.1"
        self._connect(host, port, send)

    def _connect(self, host, port, then):
        self._set_login_enabled(False)
        self.set_status(f"Connecting to {host}:{port}…", "busy")
        self.events = events = queue.Queue()
        self.conn = conn = ChatConnection(events)
        self.on_connected = then

        def worker():
            try:
                conn.connect(host, port)
                events.put(("connected", f"{host}:{port}"))
            except OSError as e:
                events.put(("connect_failed", f"Could not connect to {host}:{port} ({e.strerror or e})"))

        threading.Thread(target=worker, name="connect", daemon=True).start()

    def send_message(self):
        to, text = self.to_var.get().strip(), self.text_var.get()
        if not text.strip():
            return
        if not to:
            self.add_line("Enter who the message is for in To:.", "warning")
            self.to_entry.focus_set()
            return
        if len(text) > MAX_TEXT_CHARS:
            self.add_line(f"Messages can be at most {MAX_TEXT_CHARS} characters.", "warning")
            return
        try:
            self.conn.request("MSG", {"to": to, "text": text})
        except OSError:
            self.add_line("Not connected: the message was not sent.", "warning")
            return
        self.text_var.set("")
        self.add_line(text, "me", meta=f"You → {to}  {clock(time.time())}")

    def logout(self):
        if self.conn is None or not self.conn.connected:
            self._back_to_login("You have logged out.", "connected")
            return
        self.logging_out = True
        try:
            self.conn.request("LOGOUT")
        except OSError:
            self._back_to_login("You have logged out.", "connected")

    def quit(self):
        if self.conn is not None:
            self.conn.close()
        self.root.destroy()

    def _back_to_login(self, message, colour="offline"):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        self.username = None
        self.logging_out = False
        self.set_status("Not connected", "offline")
        self.show_login(message, colour)

    # ---------- network events (main thread) ----------

    def _poll(self):
        try:
            while True:
                self.handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll)

    def handle_event(self, item):
        kind = item[0]
        if kind == "connected":
            self.set_status(f"Connected to {item[1]}", "busy")
            action, self.on_connected = self.on_connected, None
            if action:
                action()
        elif kind == "connect_failed":
            self.conn = None
            self.set_status("Not connected", "offline")
            self.show_login(item[1])
        elif kind == "disconnected":
            if self.logging_out:
                self._back_to_login("You have logged out.", "connected")
            elif self.username is not None:
                self._back_to_login(f"Disconnected from the server: {item[1]}.")
            else:
                self.conn = None
                self.set_status("Not connected", "offline")
                self._set_login_enabled(True)
        elif kind == "message":
            self._handle_message(item[1], item[2])

    def _handle_message(self, message, request_type):
        mtype = message.get("type")
        if mtype == "ACK":
            self._handle_ack(message, request_type)
        elif mtype == "ERROR":
            self._handle_error(message, request_type)
        elif mtype == "EVENT" and message.get("kind") == "msg":
            self._show_incoming(message["data"])

    def _handle_ack(self, message, request_type):
        status, data = message.get("status"), message.get("data", {})
        if request_type == "REGISTER":
            credentials, self.pending_login = self.pending_login, None
            self.login_message.configure(text="Account created. Logging in…",
                                         foreground=STATUS_COLOURS["connected"])
            if credentials:
                self.conn.request("LOGIN", credentials)
        elif request_type == "LOGIN":
            self.username = data.get("username")
            self.set_status(f"Connected as {self.username}", "connected")
            self.show_chat()
        elif request_type == "LOGOUT":
            self._back_to_login("You have logged out.", "connected")
        elif request_type == "MSG" and status == "NOT_DELIVERED":
            self.add_line(f"{data.get('to')} is offline — your message was not delivered.", "warning")

    def _handle_error(self, message, request_type):
        text = describe_error(message)
        if message.get("code") == "SESSION_EXPIRED" and self.username is not None:
            self._back_to_login(text)
        elif self.username is None:
            self.pending_login = None
            self.show_login(text)
        else:
            self.add_line(text, "warning")

    def _show_incoming(self, data):
        sender, seq = data.get("from"), data.get("seq")
        last = self.last_seq.get(sender)
        if isinstance(seq, int):
            if last is not None and seq <= last:
                self.add_line(f"Message from {sender} arrived out of order.", "warning")
            self.last_seq[sender] = seq
        self.add_line(data.get("text", ""), "them", meta=f"{sender}  {clock(data.get('ts', time.time()))}")
        if not self.to_var.get().strip():
            self.to_var.set(sender)
