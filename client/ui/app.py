
"""Modern Tkinter chat client with login, registration and messaging."""

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from client.net.connection import ChatConnection


POLL_MS = 50
MAX_TEXT_CHARS = 2000

STATUS_COLOURS = {
    "connected": "#22C55E",
    "busy": "#F59E0B",
    "offline": "#EF4444",
}

FRIENDLY_ERRORS = {
    "AUTH_FAILED": "Login failed. Check your username and password.",
    "DUPLICATE": "That username is already taken.",
    "SESSION_EXPIRED": "Your session has expired. Please log in again.",
    "SERVER_BUSY": "The server is full. Try again in a moment.",
}


def describe_error(message):
    code = message.get("code")
    return (
        FRIENDLY_ERRORS.get(code)
        or message.get("reason")
        or f"Request failed ({code})"
    )


def clock(ts):
    return time.strftime("%H:%M", time.localtime(ts))


class ChatApp:

    def __init__(self, root, host="127.0.0.1", port=5050):
        self.root = root
        self.conn = None
        self.events = queue.Queue()
        self.username = None
        self.on_connected = None
        self.pending_login = None
        self.logging_out = False
        self.last_seq = {}

        # Theme
        self.bg = "#111827"
        self.panel = "#1F2937"
        self.input_bg = "#374151"
        self.accent = "#6366F1"
        self.text_color = "#F9FAFB"
        self.muted = "#9CA3AF"

        root.title("Connect | Chat Application")
        root.geometry("950x650")
        root.minsize(700, 500)
        root.configure(bg=self.bg)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        self._configure_styles()

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

    # ---------- theme ----------

    def _configure_styles(self):
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(
            "TFrame",
            background=self.bg
        )

        style.configure(
            "TLabel",
            background=self.bg,
            foreground=self.text_color,
            font=("Segoe UI", 10)
        )

        style.configure(
            "TEntry",
            fieldbackground=self.input_bg,
            foreground=self.text_color,
            insertcolor="white",
            padding=9,
            borderwidth=0
        )

        style.configure(
            "TButton",
            background=self.accent,
            foreground="white",
            font=("Segoe UI", 10, "bold"),
            padding=(12, 8),
            borderwidth=0
        )

        style.map(
            "TButton",
            background=[
                ("active", "#4F46E5"),
                ("disabled", "#4B5563")
            ],
            foreground=[
                ("disabled", "#9CA3AF")
            ]
        )

        style.configure(
            "Vertical.TScrollbar",
            background=self.panel,
            troughcolor=self.bg,
            borderwidth=0
        )

    # ---------- status bar ----------

    def _build_status_bar(self):
        bar = tk.Frame(
            self.root,
            bg=self.panel,
            padx=12,
            pady=9
        )
        bar.pack(side="bottom", fill="x")

        self.status_dot = tk.Label(
            bar,
            text="●",
            bg=self.panel,
            fg=STATUS_COLOURS["offline"],
            font=("Segoe UI", 10)
        )
        self.status_dot.pack(side="left")

        tk.Label(
            bar,
            textvariable=self.status_var,
            bg=self.panel,
            fg=self.text_color,
            font=("Segoe UI", 9)
        ).pack(side="left", padx=7)

    # ---------- login screen ----------

    def _build_login(self):
        self.login_frame = frame = tk.Frame(
            self.root,
            bg=self.bg
        )

        card = tk.Frame(
            frame,
            bg=self.panel,
            padx=38,
            pady=30
        )

        card.place(
            relx=0.5,
            rely=0.46,
            anchor="center"
        )

        tk.Label(
            card,
            text="CONNECT",
            bg=self.panel,
            fg=self.accent,
            font=("Segoe UI", 27, "bold")
        ).pack(pady=(0, 5))

        tk.Label(
            card,
            text="Your space to connect and communicate",
            bg=self.panel,
            fg=self.muted,
            font=("Segoe UI", 10)
        ).pack(pady=(0, 25))

        fields = (
            ("Server Address", self.host_var, {}),
            ("Port", self.port_var, {}),
            ("Username", self.user_var, {}),
            ("Password", self.password_var, {"show": "•"}),
        )

        self.login_entries = []

        for label, variable, options in fields:
            tk.Label(
                card,
                text=label,
                bg=self.panel,
                fg=self.text_color,
                font=("Segoe UI", 10, "bold")
            ).pack(anchor="w", pady=(8, 4))

            entry = ttk.Entry(
                card,
                textvariable=variable,
                width=36,
                **options
            )

            entry.pack(fill="x", ipady=3)

            entry.bind(
                "<Return>",
                lambda _event: self.login()
            )

            self.login_entries.append(entry)

        buttons = tk.Frame(
            card,
            bg=self.panel
        )
        buttons.pack(fill="x", pady=(22, 10))

        self.register_button = ttk.Button(
            buttons,
            text="Register",
            command=self.register
        )
        self.register_button.pack(
            side="left",
            expand=True,
            fill="x",
            padx=(0, 6)
        )

        self.login_button = ttk.Button(
            buttons,
            text="Log In",
            command=self.login
        )
        self.login_button.pack(
            side="left",
            expand=True,
            fill="x",
            padx=(6, 0)
        )

        self.login_message = tk.Label(
            card,
            text="",
            bg=self.panel,
            fg="#F87171",
            font=("Segoe UI", 9),
            wraplength=350,
            justify="center"
        )
        self.login_message.pack(pady=(10, 0))

    # ---------- chat screen ----------

    def _build_chat(self):
        self.chat_frame = frame = tk.Frame(
            self.root,
            bg=self.bg,
            padx=18,
            pady=15
        )

        # Header
        header = tk.Frame(
            frame,
            bg=self.panel,
            padx=15,
            pady=13
        )
        header.pack(fill="x")

        tk.Label(
            header,
            text="CONNECT",
            bg=self.panel,
            fg=self.accent,
            font=("Segoe UI", 18, "bold")
        ).pack(side="left")

        self.user_label = tk.Label(
            header,
            text="",
            bg=self.panel,
            fg=self.muted,
            font=("Segoe UI", 10)
        )
        self.user_label.pack(side="left", padx=18)

        self.logout_button = ttk.Button(
            header,
            text="Log Out",
            command=self.logout
        )
        self.logout_button.pack(side="right")

        # Recipient section
        recipient = tk.Frame(
            frame,
            bg=self.bg,
            pady=13
        )
        recipient.pack(fill="x")

        tk.Label(
            recipient,
            text="Send message to:",
            bg=self.bg,
            fg=self.text_color,
            font=("Segoe UI", 10, "bold")
        ).pack(side="left")

        self.to_entry = ttk.Entry(
            recipient,
            textvariable=self.to_var,
            width=30
        )
        self.to_entry.pack(side="left", padx=12)

        # Message input - packed BEFORE chat history
        bottom = tk.Frame(
            frame,
            bg=self.bg,
            pady=8
        )
        bottom.pack(side="bottom", fill="x")

        self.message_entry = ttk.Entry(
            bottom,
            textvariable=self.text_var,
            font=("Segoe UI", 11)
        )

        self.message_entry.pack(
            side="left",
            fill="x",
            expand=True,
            ipady=9
        )

        self.message_entry.bind(
            "<Return>",
            lambda _event: self.send_message()
        )

        self.send_button = ttk.Button(
            bottom,
            text="Send  ➤",
            command=self.send_message
        )

        self.send_button.pack(
            side="right",
            padx=(10, 0)
        )

        # Chat history
        body = tk.Frame(
            frame,
            bg=self.panel,
            padx=10,
            pady=10
        )

        body.pack(
            fill="both",
            expand=True,
            pady=(0, 8)
        )

        self.log = tk.Text(
            body,
            wrap="word",
            state="disabled",
            font=("Segoe UI", 11),
            bg=self.panel,
            fg=self.text_color,
            insertbackground="white",
            selectbackground=self.accent,
            relief="flat",
            padx=12,
            pady=12,
            spacing3=8
        )

        scroll = ttk.Scrollbar(
            body,
            command=self.log.yview,
            style="Vertical.TScrollbar"
        )

        self.log.configure(
            yscrollcommand=scroll.set
        )

        scroll.pack(side="right", fill="y")

        self.log.pack(
            side="left",
            fill="both",
            expand=True
        )

        self.log.tag_configure(
            "me",
            foreground="#A5B4FC",
            justify="right",
            font=("Segoe UI", 11)
        )

        self.log.tag_configure(
            "them",
            foreground="#F9FAFB",
            justify="left",
            font=("Segoe UI", 11)
        )

        self.log.tag_configure(
            "meta",
            foreground="#9CA3AF",
            font=("Segoe UI", 8)
        )

        self.log.tag_configure(
            "system",
            foreground="#818CF8",
            justify="center",
            font=("Segoe UI", 9, "italic")
        )

        self.log.tag_configure(
            "warning",
            foreground="#FBBF24",
            justify="center",
            font=("Segoe UI", 9, "italic")
        )

    # ---------- screens and status ----------

    def show_login(self, message="", colour="offline"):
        self.chat_frame.pack_forget()

        self.login_frame.pack(
            fill="both",
            expand=True
        )

        self.login_message.configure(
            text=message,
            fg=STATUS_COLOURS[colour]
        )

        self._set_login_enabled(True)
        self.root.title("Connect | Chat Application")

    def show_chat(self):
        self.login_frame.pack_forget()

        self.chat_frame.pack(
            fill="both",
            expand=True
        )

        self.root.title(f"Connect | {self.username}")

        self.user_label.configure(
            text=f"●  {self.username}",
            fg=STATUS_COLOURS["connected"]
        )

        self.password_var.set("")
        self.last_seq.clear()
        self._clear_log()

        self.add_line(
            f"Welcome, {self.username}! "
            "Enter a username above to start chatting.",
            "system"
        )

        (
            self.to_entry
            if not self.to_var.get()
            else self.message_entry
        ).focus_set()

    def set_status(self, text, state):
        self.status_var.set(text)

        self.status_dot.configure(
            foreground=STATUS_COLOURS[state]
        )

    def _set_login_enabled(self, enabled):
        state = "normal" if enabled else "disabled"

        for widget in (
            self.login_button,
            self.register_button,
            *self.login_entries
        ):
            widget.configure(state=state)

    # ---------- chat log ----------

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def add_line(self, text, tag, meta=None):
        self.log.configure(state="normal")

        if meta:
            self.log.insert(
                "end",
                meta + "\n",
                (tag, "meta")
            )

        self.log.insert(
            "end",
            text + ("\n\n" if meta else "\n"),
            tag
        )

        self.log.configure(state="disabled")
        self.log.see("end")

    # ---------- user actions ----------

    def login(self):
        self._start("LOGIN")

    def register(self):
        self._start("REGISTER")

    def _start(self, action):
        username = self.user_var.get().strip()
        password = self.password_var.get()

        if not username or not password:
            self.login_message.configure(
                text="Enter a username and password."
            )
            return

        self.login_message.configure(text="")

        credentials = {
            "username": username,
            "password": password
        }

        self.pending_login = (
            credentials if action == "REGISTER" else None
        )

        def send():
            self.conn.request(action, credentials)

        if self.conn is not None and self.conn.connected:
            self._set_login_enabled(False)
            send()
            return

        try:
            port = int(self.port_var.get())
        except ValueError:
            self.login_message.configure(
                text="Port must be a number."
            )
            return

        host = self.host_var.get().strip() or "127.0.0.1"

        self._connect(host, port, send)

    def _connect(self, host, port, then):
        self._set_login_enabled(False)

        self.set_status(
            f"Connecting to {host}:{port}...",
            "busy"
        )

        self.events = events = queue.Queue()
        self.conn = conn = ChatConnection(events)
        self.on_connected = then

        def worker():
            try:
                conn.connect(host, port)

                events.put(
                    ("connected", f"{host}:{port}")
                )

            except OSError as error:
                events.put(
                    (
                        "connect_failed",
                        f"Could not connect to {host}:{port} "
                        f"({error.strerror or error})"
                    )
                )

        threading.Thread(
            target=worker,
            name="connect",
            daemon=True
        ).start()

    def send_message(self):
        to = self.to_var.get().strip()
        text = self.text_var.get()

        if not text.strip():
            return

        if not to:
            self.add_line(
                "Enter who the message is for in the recipient field.",
                "warning"
            )
            self.to_entry.focus_set()
            return

        if len(text) > MAX_TEXT_CHARS:
            self.add_line(
                f"Messages can be at most {MAX_TEXT_CHARS} characters.",
                "warning"
            )
            return

        try:
            if self.conn is None or not self.conn.connected:
                raise OSError("Not connected")

            self.conn.request(
                "MSG",
                {
                    "to": to,
                    "text": text
                }
            )

        except (OSError, AttributeError):
            self.add_line(
                "Not connected: the message was not sent.",
                "warning"
            )
            return

        self.text_var.set("")

        self.add_line(
            text,
            "me",
            meta=f"You → {to}  {clock(time.time())}"
        )

    def logout(self):
        if self.conn is None or not self.conn.connected:
            self._back_to_login(
                "You have logged out.",
                "connected"
            )
            return

        self.logging_out = True

        try:
            self.conn.request("LOGOUT")

        except OSError:
            self._back_to_login(
                "You have logged out.",
                "connected"
            )

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

        self.set_status(
            "Not connected",
            "offline"
        )

        self.show_login(message, colour)

    # ---------- network events ----------

    def _poll(self):
        try:
            while True:
                self.handle_event(
                    self.events.get_nowait()
                )

        except queue.Empty:
            pass

        self.root.after(POLL_MS, self._poll)

    def handle_event(self, item):
        kind = item[0]

        if kind == "connected":
            self.set_status(
                f"Connected to {item[1]}",
                "busy"
            )

            action, self.on_connected = (
                self.on_connected,
                None
            )

            if action:
                action()

        elif kind == "connect_failed":
            self.conn = None
            self.set_status("Not connected", "offline")
            self.show_login(item[1])

        elif kind == "disconnected":
            if self.logging_out:
                self._back_to_login(
                    "You have logged out.",
                    "connected"
                )

            elif self.username is not None:
                self._back_to_login(
                    f"Disconnected from the server: {item[1]}."
                )

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

        elif (
            mtype == "EVENT"
            and message.get("kind") == "msg"
        ):
            self._show_incoming(message["data"])

    def _handle_ack(self, message, request_type):
        status = message.get("status")
        data = message.get("data", {})

        if request_type == "REGISTER":
            credentials, self.pending_login = (
                self.pending_login,
                None
            )

            self.login_message.configure(
                text="Account created. Logging in...",
                fg=STATUS_COLOURS["connected"]
            )

            if credentials:
                self.conn.request("LOGIN", credentials)

        elif request_type == "LOGIN":
            self.username = data.get("username")

            self.set_status(
                f"Connected as {self.username}",
                "connected"
            )

            self.show_chat()

        elif request_type == "LOGOUT":
            self._back_to_login(
                "You have logged out.",
                "connected"
            )

        elif request_type == "MSG" and status == "NOT_DELIVERED":
            self.add_line(
                f"{data.get('to')} is offline — "
                "your message was not delivered.",
                "warning"
            )

    def _handle_error(self, message, request_type):
        error_text = describe_error(message)

        if (
            message.get("code") == "SESSION_EXPIRED"
            and self.username is not None
        ):
            self._back_to_login(error_text)

        elif self.username is None:
            self.pending_login = None
            self.show_login(error_text)

        else:
            self.add_line(error_text, "warning")

    def _show_incoming(self, data):
        sender = data.get("from")
        seq = data.get("seq")

        last = self.last_seq.get(sender)

        if isinstance(seq, int):
            if last is not None and seq <= last:
                self.add_line(
                    f"Message from {sender} arrived out of order.",
                    "warning"
                )

            self.last_seq[sender] = seq

        self.add_line(
            data.get("text", ""),
            "them",
            meta=f"{sender}  {clock(data.get('ts', time.time()))}"
        )

        if not self.to_var.get().strip():
            self.to_var.set(sender)
