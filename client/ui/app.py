
The chat screen has a sidebar with the rooms and the users who are online, and one
conversation per room or person. Tkinter must only be touched from the main thread
(SAD risk R8): the network handler and the connect worker put events on a queue,
and _poll drains it every 50 ms.
"""
import queue
import threading
import time
import tkinter as tk
from tkinter import simpledialog

import customtkinter as ctk

from client.net.connection import ChatConnection

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

POLL_MS = 50
MAX_TEXT_CHARS = 2000

APP_BG = "#0b141a"
SIDEBAR_BG = "#111b21"
PANEL_BG = "#17232b"
INPUT_BG = "#202c33"
OUTLINE = "#293941"
TEXT = "#e9edef"
MUTED = "#8696a0"
ACCENT = "#00a884"
ACCENT_HOVER = "#008f72"
STATUS_COLOURS = {"connected": "#00a884", "busy": "#fbbf24", "offline": "#fb7185"}

FRIENDLY_ERRORS = {
    "AUTH_FAILED": "Login failed. Check your username and password.",
    "SESSION_EXPIRED": "Your session has expired. Please log in again.",
    "SERVER_BUSY": "The server is full. Try again in a moment.",
}

ROOM, USER = "room", "user"

ADMIN_HELP = ("Admin commands: /admin activate|deactivate|restrict <user>, "
              "/admin open_room|restrict_room|remove_room <room>, /admin rename_room <room> <new name>")


def describe_error(message):
    code = message.get("code")
    return (
        FRIENDLY_ERRORS.get(code)
        or message.get("reason")
        or f"Request failed ({code})"
    )


def clock(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def conversation_title(conversation):
    kind, name = conversation
    return f"# {name}" if kind == ROOM else name


class ChatApp:

    def __init__(self, root, host="127.0.0.1", port=5050):
        self.root = root
        self.conn = None
        self.events = queue.Queue()
        self.username = None
        self.role = None
        self.on_connected = None      # what to send once the connect worker succeeds
        self.pending_login = None     # credentials to log in with after REGISTER succeeds
        self.logging_out = False
        self.last_seq = {}            # (conversation, sender) -> last seq seen (CHAT-F-008)
        self.online = set()
        self.rooms = {}               # room name -> {"joined": bool, "members": int, "status": str}
        self.lines = {}               # conversation -> [(text, tag, meta, key)]; key = (sender, ts)
        self.history_requested = set()
        self.unread = set()
        self.current = None           # (ROOM, name) or (USER, name)
        self._room_names = []         # what each sidebar row shows, in order
        self._user_names = []

        root.title("Relay | Chat")
        root.geometry("1180x760")
        root.minsize(860, 600)
        root.configure(fg_color=APP_BG)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        self._configure_styles()

        self.host_var = tk.StringVar(value=host)
        self.port_var = tk.StringVar(value=str(port))
        self.user_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.text_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.title_var = tk.StringVar()
        self.subtitle_var = tk.StringVar(value="Choose a room or person to start chatting")
        self.search_var = tk.StringVar()

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
        bar = ctk.CTkFrame(self.root, fg_color="transparent", height=38)
        bar.pack(side="bottom", fill="x", padx=18, pady=(0, 8))
        self.status_dot = ctk.CTkLabel(
            bar, text="●", text_color=STATUS_COLOURS["offline"], font=("Segoe UI", 12))
        self.status_dot.pack(side="left")
        ctk.CTkLabel(
            bar, textvariable=self.status_var, text_color=MUTED, font=("Segoe UI", 11)
        ).pack(side="left", padx=(8, 0))

    def _build_login(self):
        self.login_frame = frame = ctk.CTkFrame(self.root, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=38, pady=28)
        frame.grid_columnconfigure((0, 1), weight=1, uniform="login")
        frame.grid_rowconfigure(0, weight=1)

        hero = ctk.CTkFrame(frame, fg_color=SIDEBAR_BG, corner_radius=28)
        hero.grid(row=0, column=0, sticky="nsew", padx=(0, 14), pady=8)
        ctk.CTkLabel(
            hero, text="✦   RELAY", text_color="#aab4ff",
            font=("Segoe UI", 13, "bold")
        ).pack(anchor="w", padx=42, pady=(48, 0))
        ctk.CTkLabel(
            hero, text="Good conversations\nstart here.",
            text_color=TEXT, font=("Segoe UI", 34, "bold"), justify="left"
        ).pack(anchor="w", padx=42, pady=(46, 16))
        ctk.CTkLabel(
            hero, text="A calmer place to catch up with your people.",
            text_color=MUTED, font=("Segoe UI", 14), wraplength=350, justify="left"
        ).pack(anchor="w", padx=42)
        ctk.CTkFrame(hero, height=1, fg_color=OUTLINE).pack(fill="x", padx=42, pady=32)
        for title, detail in (
            ("Private by design", "Encrypted connections keep your chats protected."),
            ("Room for everyone", "Create a room or pick up a direct conversation."),
            ("Always in sync", "See who's online and catch up on saved history."),
        ):
            item = ctk.CTkFrame(hero, fg_color="transparent")
            item.pack(fill="x", padx=42, pady=9)
            ctk.CTkLabel(
                item, text="●", text_color=ACCENT, font=("Segoe UI", 12)
            ).pack(side="left", anchor="n", padx=(0, 12), pady=3)
            copy = ctk.CTkFrame(item, fg_color="transparent")
            copy.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(
                copy, text=title, text_color=TEXT, font=("Segoe UI", 13, "bold"),
                anchor="w"
            ).pack(anchor="w")
            ctk.CTkLabel(
                copy, text=detail, text_color=MUTED, font=("Segoe UI", 11),
                anchor="w", wraplength=310, justify="left"
            ).pack(anchor="w", pady=(3, 0))

        card = ctk.CTkFrame(frame, fg_color=PANEL_BG, corner_radius=28)
        card.grid(row=0, column=1, sticky="nsew", padx=(14, 0), pady=8)
        ctk.CTkLabel(
            card, text="Welcome back", text_color=TEXT, font=("Segoe UI", 27, "bold")
        ).pack(anchor="w", padx=42, pady=(54, 6))
        ctk.CTkLabel(
            card, text="Sign in or create an account to continue.",
            text_color=MUTED, font=("Segoe UI", 12)
        ).pack(anchor="w", padx=42, pady=(0, 30))

        fields = (
            ("Server address", self.host_var, {"placeholder_text": "127.0.0.1"}),
            ("Port", self.port_var, {"placeholder_text": "5050"}),
            ("Username", self.user_var, {"placeholder_text": "Your username"}),
            ("Password", self.password_var, {"placeholder_text": "Your password", "show": "•"}),
        )
        self.login_entries = []
        for label, variable, options in fields:
            ctk.CTkLabel(
                card, text=label, text_color="#cbd5e1", font=("Segoe UI", 11, "bold")
            ).pack(anchor="w", padx=42, pady=(0, 7))
            entry = ctk.CTkEntry(
                card, textvariable=variable, height=44, corner_radius=12,
                border_color=OUTLINE, fg_color=INPUT_BG, text_color=TEXT,
                placeholder_text_color="#66758c", font=("Segoe UI", 12), **options
            )
            entry.pack(fill="x", padx=42, pady=(0, 17))
            entry.bind("<Return>", lambda _e: self.login())
            self.login_entries.append(entry)
        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(fill="x", padx=42, pady=(3, 0))
        self.register_button = ctk.CTkButton(
            buttons, text="Create account", command=self.register, height=44,
            corner_radius=12, fg_color=INPUT_BG, hover_color=OUTLINE,
            border_width=1, border_color=OUTLINE, text_color=TEXT,
            font=("Segoe UI", 12, "bold")
        )
        self.register_button.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.login_button = ctk.CTkButton(
            buttons, text="Sign in", command=self.login, height=44,
            corner_radius=12, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=("Segoe UI", 12, "bold")
        )
        self.login_button.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.login_message = ctk.CTkLabel(
            card, text="", text_color=STATUS_COLOURS["offline"],
            font=("Segoe UI", 11), wraplength=380, justify="left"
        )
        self.login_message.pack(anchor="w", padx=42, pady=(18, 8))

    def _build_chat(self):
        self.chat_frame = frame = ctk.CTkFrame(self.root, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=16, pady=(14, 0))

        sidebar = ctk.CTkFrame(frame, width=330, fg_color=SIDEBAR_BG, corner_radius=18)
        sidebar.pack(side="left", fill="y", padx=(0, 12), pady=(0, 8))
        sidebar.pack_propagate(False)
        brand = ctk.CTkFrame(sidebar, fg_color="transparent", height=62)
        brand.pack(fill="x", padx=18, pady=(10, 4))
        brand.pack_propagate(False)
        ctk.CTkLabel(
            brand, text="Chats", text_color=TEXT, font=("Segoe UI", 22, "bold")
        ).pack(side="left")
        self.add_chat_button = ctk.CTkButton(
            brand, text="+", command=self.ask_direct_message, width=38, height=38,
            corner_radius=19, fg_color=INPUT_BG, hover_color=OUTLINE,
            text_color="#c7d5dc", font=("Segoe UI", 22, "normal")
        )
        self.add_chat_button.pack(side="right")
        self.search_entry = ctk.CTkEntry(
            sidebar, textvariable=self.search_var, height=38, corner_radius=10,
            border_width=0, fg_color=INPUT_BG, text_color=TEXT,
            placeholder_text="⌕   Search chats", placeholder_text_color=MUTED,
            font=("Segoe UI", 11)
        )
        self.search_entry.pack(fill="x", padx=14, pady=(0, 12))
        self.search_var.trace_add("write", lambda *_args: self._refresh_sidebar())
        self.chat_list = ctk.CTkScrollableFrame(
            sidebar, fg_color="transparent", corner_radius=0,
            scrollbar_button_color="#34454e", scrollbar_button_hover_color="#4a606b"
        )
        self.chat_list.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        self.new_group_button = ctk.CTkButton(
            sidebar, text="＋   New group", command=self.ask_new_room, height=40,
            corner_radius=10, fg_color=INPUT_BG, hover_color=OUTLINE,
            text_color="#c7d5dc", font=("Segoe UI", 11, "bold")
        )
        self.new_group_button.pack(side="bottom", fill="x", padx=14, pady=(0, 14))

        main = ctk.CTkFrame(frame, fg_color="transparent")
        main.pack(side="left", fill="both", expand=True, pady=(0, 8))
        top = ctk.CTkFrame(main, fg_color=SIDEBAR_BG, corner_radius=16, height=72)
        top.pack(fill="x", pady=(0, 12))
        top.pack_propagate(False)
        title_box = ctk.CTkFrame(top, fg_color="transparent")
        title_box.pack(side="left", fill="both", expand=True, padx=18, pady=10)
        self.header_avatar = ctk.CTkLabel(
            title_box, text="✦", width=42, height=42, corner_radius=21,
            fg_color=INPUT_BG, text_color="#9aabb3", font=("Segoe UI", 16, "bold")
        )
        self.header_avatar.pack(side="left", padx=(0, 12))
        title_copy = ctk.CTkFrame(title_box, fg_color="transparent")
        title_copy.pack(side="left", fill="y", pady=5)
        self.conversation_heading = ctk.CTkLabel(
            title_copy, textvariable=self.title_var, text_color=TEXT,
            font=("Segoe UI", 14, "bold"), anchor="w"
        )
        self.conversation_heading.pack(anchor="w", pady=(0, 2))
        self.conversation_subtitle = ctk.CTkLabel(
            title_copy, textvariable=self.subtitle_var,
            text_color=MUTED, font=("Segoe UI", 10), anchor="w"
        )
        self.conversation_subtitle.pack(anchor="w")
        self.room_action_button = ctk.CTkButton(
            top, text="Join group", command=self.join_selected_room, width=112,
            height=34, corner_radius=9, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=("Segoe UI", 10, "bold")
        )
        self.logout_button = ctk.CTkButton(
            top, text="Log out", command=self.logout, width=92, height=34,
            corner_radius=9, fg_color=INPUT_BG, hover_color=OUTLINE,
            text_color="#cbd5e1", font=("Segoe UI", 10, "bold")
        )
        self.logout_button.pack(side="right", padx=(8, 14))

        body = ctk.CTkFrame(main, fg_color=PANEL_BG, corner_radius=16)
        body.pack(fill="both", expand=True, pady=(0, 12))
        self.empty_canvas = tk.Canvas(
            body, highlightthickness=0, background="#101d24", borderwidth=0
        )
        self.empty_canvas.pack(fill="both", expand=True)
        self.empty_state = ctk.CTkFrame(
            body, fg_color="#17262d", corner_radius=20, border_width=1,
            border_color="#263a42"
        )
        ctk.CTkLabel(
            self.empty_state, text="✦", width=64, height=64, corner_radius=32,
            fg_color="#123d35", text_color="#6ed6b3", font=("Segoe UI", 28, "bold")
        ).pack(pady=(26, 16))
        ctk.CTkLabel(
            self.empty_state, text="Your chats, all in one place",
            text_color=TEXT, font=("Segoe UI", 20, "bold")
        ).pack(padx=34)
        ctk.CTkLabel(
            self.empty_state, text="Choose a conversation or start something new.",
            text_color=MUTED, font=("Segoe UI", 11)
        ).pack(padx=34, pady=(8, 24))
        self.empty_window = self.empty_canvas.create_window(
            0, 0, window=self.empty_state, anchor="center"
        )
        self.empty_canvas.bind("<Configure>", self._draw_empty_wallpaper)
        self.log = tk.Text(
            body, wrap="word", state="disabled", borderwidth=0, padx=16, pady=14,
            background="#101d24", foreground=TEXT, insertbackground=TEXT,
            selectbackground="#303c70", font=("Segoe UI", 11)
        )
        self.log_scrollbar = ctk.CTkScrollbar(body, command=self.log.yview)
        self.log.configure(yscrollcommand=self.log_scrollbar.set)
        self.log.tag_configure(
            "me", justify="right", foreground="#e1fff4", background="#075e54",
            lmargin1=150, lmargin2=150, rmargin=18, spacing1=5, spacing3=5
        )
        self.log.tag_configure(
            "them", justify="left", foreground="#e9edef", background="#202c33",
            lmargin1=18, lmargin2=18, rmargin=150, spacing1=5, spacing3=5
        )
        self.log.tag_configure("meta", font=("Segoe UI", 9), foreground=MUTED)
        self.log.tag_configure("system", justify="center", foreground="#9aa8bd", font=("Segoe UI", 10, "italic"))
        self.log.tag_configure("warning", justify="center", foreground="#fbbf24", font=("Segoe UI", 10, "italic"))

        bottom = ctk.CTkFrame(main, fg_color=PANEL_BG, corner_radius=16)
        bottom.pack(fill="x")
        self.message_entry = ctk.CTkEntry(
            bottom, textvariable=self.text_var, height=44, corner_radius=12,
            border_width=0, fg_color=INPUT_BG, text_color=TEXT,
            placeholder_text="Write a message…", placeholder_text_color="#718096",
            font=("Segoe UI", 11)
        )
        self.message_entry.pack(side="left", fill="x", expand=True, padx=(12, 8), pady=10)
        self.message_entry.bind("<Return>", lambda _e: self.send_message())
        self.send_button = ctk.CTkButton(
            bottom, text="Send  ➤", command=self.send_message, width=100, height=42,
            corner_radius=11, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=("Segoe UI", 11, "bold")
        )
        self.send_button.pack(side="right", padx=(0, 12), pady=10)

    def _draw_empty_wallpaper(self, event):
        self.empty_canvas.coords(self.empty_window, event.width / 2, event.height / 2)
        self.empty_canvas.delete("wallpaper")
        step = 66
        for row, y in enumerate(range(24, event.height, step)):
            for column, x in enumerate(range(24, event.width, step)):
                offset = 30 if row % 2 else 0
                cx, cy = x + offset, y
                radius = 2 if (row + column) % 3 else 5
                self.empty_canvas.create_oval(
                    cx - radius, cy - radius, cx + radius, cy + radius,
                    outline="#1c3038", fill="#1c3038", tags="wallpaper"
                )
        self.empty_canvas.tag_lower("wallpaper", self.empty_window)

    # ---------- screens and status ----------

    def show_login(self, message="", colour="offline"):
        self.chat_frame.pack_forget()
        self.login_frame.pack(fill="both", expand=True)
        self.login_message.configure(text=message, text_color=STATUS_COLOURS[colour])
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
        self.current = None
        self.title_var.set("")
        self.subtitle_var.set("Choose a room or person to start chatting")
        self._render()
        self.add_line(f"Logged in as {self.username}. Pick a room or a person on the left.", "system")

    def set_status(self, text, state):
        self.status_var.set(text)
        self.status_dot.configure(text_color=STATUS_COLOURS[state])

    def _set_login_enabled(self, enabled):
        state = "normal" if enabled else "disabled"

        for widget in (
            self.login_button,
            self.register_button,
            *self.login_entries
        ):
            widget.configure(state=state)

    # ---------- sidebar ----------

    def _refresh_sidebar(self):
        self._room_names = sorted(self.rooms, key=str.casefold)
        self._user_names = sorted(
            (self.online - {self.username}) |
            {conversation[1] for conversation in self.lines
             if conversation and conversation[0] == USER},
            key=str.casefold
        )
        for child in self.chat_list.winfo_children():
            child.destroy()

        query = self.search_var.get().strip().casefold()
        self.chat_rows = {}
        groups = (
            ("GROUPS", [(ROOM, name) for name in self._room_names]),
            ("PEOPLE", [(USER, name) for name in self._user_names]),
        )
        visible_count = 0
        for heading, conversations in groups:
            matches = [
                conversation for conversation in conversations
                if query in conversation[1].casefold()
            ]
            if not matches:
                continue
            ctk.CTkLabel(
                self.chat_list, text=heading, text_color="#7f929c",
                font=("Segoe UI", 9, "bold"), anchor="w"
            ).pack(fill="x", padx=12, pady=(8, 5))
            for kind, name in matches:
                self._build_chat_row(kind, name)
                visible_count += 1
        if not visible_count:
            message = "No chats match your search." if query else "No conversations yet."
            ctk.CTkLabel(
                self.chat_list, text=message, text_color=MUTED,
                font=("Segoe UI", 11), wraplength=250
            ).pack(fill="x", padx=12, pady=20)

        if self.current and self.current[0] == ROOM and self.current[1] in self.rooms:
            info = self.rooms.get(self.current[1], {})
            joined = info.get("joined", False)
            subtitle = f"{info.get('members', 0)} members"
            if not joined:
                subtitle += " · not joined"
            if info.get("status") == "RESTRICTED":
                subtitle += " · read-only"
            self.subtitle_var.set(subtitle)
            self.room_action_button.configure(
                text="Leave group" if joined else "Join group",
                command=self.leave_selected_room if joined else self.join_selected_room
            )
            if not self.room_action_button.winfo_manager():
                self.room_action_button.pack(side="right", padx=(0, 8))
        elif self.current and self.current[0] == USER:
            self.subtitle_var.set("Online" if self.current[1] in self.online else "Offline")
            self.room_action_button.pack_forget()
        else:
            self.room_action_button.pack_forget()

    def _build_chat_row(self, kind, name):
        conversation = (kind, name)
        selected = conversation == self.current
        background = "#1f343b" if selected else "transparent"
        row = ctk.CTkFrame(
            self.chat_list, height=68, corner_radius=10, fg_color=background
        )
        self.chat_rows[conversation] = row
        row.pack(fill="x", padx=4, pady=1)
        row.pack_propagate(False)
        avatar_color = "#17483f" if kind == ROOM else "#263a42"
        avatar_text = "▦" if kind == ROOM else (name[:1].upper() or "?")
        avatar = ctk.CTkLabel(
            row, text=avatar_text, width=42, height=42, corner_radius=21,
            fg_color=avatar_color, text_color="#a7e3cf" if kind == ROOM else "#c9d6db",
            font=("Segoe UI", 14, "bold")
        )
        avatar.pack(side="left", padx=(10, 11))

        details = ctk.CTkFrame(row, fg_color="transparent")
        details.pack(side="left", fill="both", expand=True, pady=12)
        title = f"# {name}" if kind == ROOM else name
        ctk.CTkLabel(
            details, text=title, text_color=TEXT, font=("Segoe UI", 11, "bold"),
            anchor="w"
        ).pack(fill="x")
        history = self.lines.get(conversation, [])
        if history:
            preview = history[-1][0].replace("\n", " ")
        elif kind == ROOM:
            info = self.rooms[name]
            preview = f"{info['members']} members"
            if info["status"] == "RESTRICTED":
                preview += " · read-only"
        else:
            preview = "Online" if name in self.online else "Start a conversation"
        ctk.CTkLabel(
            details, text=preview[:42], text_color=ACCENT if selected else MUTED,
            font=("Segoe UI", 9), anchor="w"
        ).pack(fill="x", pady=(3, 0))
        if conversation in self.unread:
            ctk.CTkLabel(
                row, text="●", text_color=ACCENT, font=("Segoe UI", 10)
            ).pack(side="right", padx=12)

        def open_row(_event=None):
            self.open_conversation(kind, name)

        for widget in (row, avatar, details, *details.winfo_children()):
            widget.bind("<Button-1>", open_row)

    def ask_new_room(self):
        name = simpledialog.askstring("New room", "Room name:", parent=self.root)
        if name and name.strip():
            self.create_room(name.strip())

    def ask_direct_message(self):
        name = simpledialog.askstring("Message someone", "Username:", parent=self.root)
        if name and name.strip():
            self.open_conversation(USER, name.strip())

    def join_selected_room(self):
        if self.current and self.current[0] == ROOM:
            self.join_room(self.current[1])

    def leave_selected_room(self):
        if self.current and self.current[0] == ROOM:
            self.leave_room(self.current[1])

    # ---------- conversations ----------

    def open_conversation(self, kind, name):
        self.current = (kind, name)
        self.unread.discard(self.current)
        self.title_var.set(conversation_title(self.current))
        self.header_avatar.configure(
            text="▦" if kind == ROOM else (name[:1].upper() or "?"),
            fg_color="#17483f" if kind == ROOM else "#263a42"
        )
        self._render()
        self._refresh_sidebar()
        if kind == ROOM and not self.rooms.get(name, {}).get("joined"):
            self.add_line(f"You are not in #{name}. Click Join to take part.", "system")
        else:
            self._load_history(self.current)
        self.message_entry.focus_set()

    def _load_history(self, conversation):
        """Ask for a conversation's saved messages the first time it is opened (CHAT-F-012)."""
        if conversation in self.history_requested:
            return
        kind, name = conversation
        if self._request("HISTORY", {kind if kind == ROOM else "with": name}):
            self.history_requested.add(conversation)

    def _render(self):
        if self.current is None:
            self.log.pack_forget()
            self.log_scrollbar.pack_forget()
            if not self.empty_canvas.winfo_manager():
                self.empty_canvas.pack(fill="both", expand=True)
            return

        self.empty_canvas.pack_forget()
        if not self.log_scrollbar.winfo_manager():
            self.log_scrollbar.pack(side="right", fill="y", padx=(0, 10), pady=12)
        if not self.log.winfo_manager():
            self.log.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=12)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        for text, tag, meta, _ in self.lines.get(self.current, []):
            self._insert(text, tag, meta)

    def _insert(self, text, tag, meta):
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

    def add_line(self, text, tag, meta=None, conversation=None, key=None):
        """Add a line to a conversation (the current one by default), showing it if it is open."""
        conversation = conversation or self.current
        self.lines.setdefault(conversation, []).append((text, tag, meta, key))
        if conversation == self.current:
            self._insert(text, tag, meta)
        elif conversation is not None and tag in ("them", "warning"):
            self.unread.add(conversation)
            self._refresh_sidebar()

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

    def _request(self, message_type, payload):
        try:
            self.conn.request(message_type, payload)
            return True
        except (OSError, AttributeError):
            self.add_line("Not connected: the request was not sent.", "warning")
            return False

    def create_room(self, name):
        self._request("CREATE_ROOM", {"room": name})

    def join_room(self, name):
        self._request("JOIN", {"room": name})

    def leave_room(self, name):
        self._request("LEAVE", {"room": name})

    def send_message(self):
        text = self.text_var.get()
        if not text.strip():
            return
        if text.startswith("/admin"):
            self._admin_command(text)
            return
        if self.current is None:
            self.add_line("Pick a room or a person on the left first.", "warning")
            return

        if len(text) > MAX_TEXT_CHARS:
            self.add_line(
                f"Messages can be at most {MAX_TEXT_CHARS} characters.",
                "warning"
            )
            return
        kind, name = self.current
        if not self._request("MSG", {kind if kind == ROOM else "to": name, "text": text}):
            return

        self.text_var.set("")
        self.add_line(text, "me", meta=f"You  {clock(time.time())}")

    def _admin_command(self, text):
        """/admin <action> <target> [new name] -> ADMIN request (CHAT-F-015, CHAT-F-016)."""
        parts = text.split(maxsplit=3)
        if len(parts) < 3:
            self.add_line(ADMIN_HELP, "system")
            return
        payload = {"action": parts[1], "target": parts[2]}
        if len(parts) == 4:
            payload["args"] = {"name": parts[3]}
        if self._request("ADMIN", payload):
            self.text_var.set("")

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
        self.role = None
        self.logging_out = False
        self.online.clear()
        self.rooms.clear()
        self.lines.clear()
        self.history_requested.clear()
        self.unread.clear()
        self.last_seq.clear()
        self.current = None
        self.set_status("Not connected", "offline")
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
        elif mtype == "EVENT":
            self._handle_event(message.get("kind"), message.get("data", {}))

    def _handle_ack(self, message, request_type):
        status = message.get("status")
        data = message.get("data", {})

        if request_type == "REGISTER":
            credentials, self.pending_login = self.pending_login, None
            self.login_message.configure(text="Account created. Logging in…",
                                         text_color=STATUS_COLOURS["connected"])
            if credentials:
                self.conn.request("LOGIN", credentials)

        elif request_type == "LOGIN":
            self.username = data.get("username")
            self.online = set(data.get("online", []))
            self.rooms = {r["name"]: {"joined": r["joined"], "members": r["members"], "status": r["status"]}
                          for r in data.get("rooms", [])}
            self.role = data.get("role")
            suffix = " (administrator)" if self.role == "ADMIN" else ""
            self.set_status(f"Connected as {self.username}{suffix}", "connected")
            self.show_chat()
            self._refresh_sidebar()
        elif request_type == "LOGOUT":
            self._back_to_login("You have logged out.", "connected")
        elif request_type in ("CREATE_ROOM", "JOIN"):
            name = data["room"]
            self.rooms[name] = {**self.rooms.get(name, {"status": "OPEN"}),
                                "joined": True, "members": len(data.get("members", []))}
            self.open_conversation(ROOM, name)
            verb = "created" if request_type == "CREATE_ROOM" else "joined"
            self.add_line(f"You {verb} #{name}. Members: {', '.join(data.get('members', []))}", "system")
        elif request_type == "LEAVE":
            name = data["room"]
            info = self.rooms.setdefault(name, {"status": "OPEN", "members": 1})
            info.update(joined=False, members=max(0, info["members"] - 1))
            self.add_line(f"You left #{name}.", "system", conversation=(ROOM, name))
            self._refresh_sidebar()
        elif request_type == "ADMIN":
            self.add_line(f"Done: {data.get('action')} {data.get('target')}", "system")
        elif request_type == "HISTORY":
            self._show_history(data)
        elif request_type == "MSG" and status == "STORED_NOT_DELIVERED":
            target = (ROOM, data["room"]) if "room" in data else (USER, data.get("to"))
            self.add_line(f"{data.get('reason')} — your message was saved and will be in the history.",
                          "warning", conversation=target)

    def _handle_error(self, message, request_type):
        error_text = describe_error(message)

        if (
            message.get("code") == "SESSION_EXPIRED"
            and self.username is not None
        ):
            self._back_to_login(error_text)

        elif self.username is None:
            self.pending_login = None
            if message.get("code") == "DUPLICATE":
                text = "That username is already taken."
            self.show_login(text)
        else:
            self.add_line(error_text, "warning")

    def _handle_event(self, kind, data):
        if kind == "msg":
            self._show_incoming(data)
        elif kind == "presence":
            user = data.get("user")
            if data.get("status") == "online":
                self.online.add(user)
            else:
                self.online.discard(user)
            if (USER, user) in self.lines:
                self.add_line(f"{user} is now {data.get('status')}.", "system", conversation=(USER, user))
            self._refresh_sidebar()
        elif kind == "room":
            self._room_changed(data)
        elif kind == "account":
            status = data.get("status", "").lower()
            self._back_to_login(f"Your account was {status} by an administrator.")
        elif kind in ("join", "leave"):
            name, user = data.get("room"), data.get("user")
            info = self.rooms.setdefault(name, {"joined": True, "members": 0, "status": "OPEN"})
            info["members"] = max(0, info["members"] + (1 if kind == "join" else -1))
            verb = "joined" if kind == "join" else "left"
            self.add_line(f"{user} {verb} the room.", "system", conversation=(ROOM, name))
            self._refresh_sidebar()

    def _room_changed(self, data):
        action, name = data.get("action"), data.get("room")
        if action == "created":
            self.rooms.setdefault(name, {"joined": False, "members": 1, "status": "OPEN"})
        elif action in ("restricted", "opened") and name in self.rooms:
            self.rooms[name]["status"] = "RESTRICTED" if action == "restricted" else "OPEN"
            note = "is now read-only" if action == "restricted" else "is open again"
            self.add_line(f"#{name} {note}.", "system", conversation=(ROOM, name))
        elif action == "removed":
            self.rooms.pop(name, None)
            self.add_line(f"#{name} was removed by an administrator.", "warning", conversation=(ROOM, name))
        elif action == "renamed" and name in self.rooms:
            new = data.get("name")
            self.rooms[new] = self.rooms.pop(name)
            old_key, new_key = (ROOM, name), (ROOM, new)
            if old_key in self.lines:
                self.lines[new_key] = self.lines.pop(old_key)
            self.history_requested = {new_key if c == old_key else c for c in self.history_requested}
            if self.current == old_key:
                self.current = new_key
                self.title_var.set(conversation_title(new_key))
                self.subtitle_var.set("Room conversation" if new_key[0] == ROOM else "Direct message")
            self.add_line(f"#{name} was renamed to #{new}.", "system", conversation=new_key)
        self._refresh_sidebar()

    def _show_history(self, data):
        conversation = (ROOM, data["room"]) if "room" in data else (USER, data["with"])
        saved = []
        for m in data.get("messages", []):
            mine = m["from"] == self.username
            meta = f"{'You' if mine else m['from']}  {clock(m['ts'])}"
            saved.append((m["text"], "me" if mine else "them", meta, (m["from"], m["ts"])))
        if not saved:
            return
        keys = {line[3] for line in saved}
        newer = [line for line in self.lines.get(conversation, []) if line[3] is None or line[3] not in keys]
        self.lines[conversation] = saved + newer
        if conversation == self.current:
            self._render()

    def _show_incoming(self, data):
        sender, seq = data.get("from"), data.get("seq")
        conversation = (ROOM, data["room"]) if "room" in data else (USER, sender)
        key = (conversation, sender)
        last = self.last_seq.get(key)
        if isinstance(seq, int):
            if last is not None and seq <= last:
                self.add_line(f"Message from {sender} arrived out of order.", "warning", conversation=conversation)
            self.last_seq[key] = seq
        if self.current is None:
            self.open_conversation(*conversation)
        self.add_line(data.get("text", ""), "them", meta=f"{sender}  {clock(data.get('ts', time.time()))}",
                      conversation=conversation, key=(sender, data.get("ts")))
