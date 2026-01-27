import json
import time
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import requests
import psutil

CONFIG_PATH = "instances.json"

# -------------------- THEME COLORS --------------------
BG = "#F5F7FA"
BLUE = "#1E88E5"
BLUE_DARK = "#1565C0"
YELLOW = "#FBC02D"
TEXT = "#0D1B2A"


class Instance:
    def __init__(self, cfg):
        self.name = cfg["name"]
        self.cwd = cfg["cwd"]
        self.cmd = cfg["cmd"]
        self.status_url = cfg["status_url"]
        self.logs_url = cfg["logs_url"]
        self.api_key = cfg.get("api_key", "")
        self.proc = None

    def headers(self):
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.is_running():
            return
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=self.cwd,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

    def stop(self):
        if not self.is_running():
            return
        try:
            p = psutil.Process(self.proc.pid)
            for c in p.children(recursive=True):
                c.terminate()
            p.terminate()
        except Exception:
            pass

    def restart(self):
        self.stop()
        time.sleep(0.5)
        self.start()

    def fetch_status(self, timeout=1.5):
        r = requests.get(self.status_url, headers=self.headers(), timeout=timeout)
        r.raise_for_status()
        return r.json()

    def fetch_logs(self, tail=300, timeout=2.5):
        r = requests.get(
            self.logs_url,
            params={"tail": str(tail)},
            headers=self.headers(),
            timeout=timeout,
        )
        r.raise_for_status()
        return r.text


class App(tk.Tk):
    def __init__(self, instances):
        super().__init__()
        self.title("Discord Music Bot Manager")
        self.geometry("1180x640")
        self.configure(bg=BG)

        self.instances = instances
        self._stop_flag = False

        self._setup_style()
        self._build_ui()

        threading.Thread(target=self.poll_loop, daemon=True).start()

    # -------------------- STYLE --------------------
    def _setup_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(
            "Treeview",
            background="white",
            foreground=TEXT,
            rowheight=32,
            fieldbackground="white",
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background=BLUE,
            foreground="white",
            font=("Segoe UI", 10, "bold"),
            padding=8,
        )
        style.map(
            "Treeview",
            background=[("selected", YELLOW)],
            foreground=[("selected", TEXT)],
        )

        style.configure(
            "Accent.TButton",
            background=BLUE,
            foreground="white",
            font=("Segoe UI", 10, "bold"),
            padding=8,
        )
        style.map(
            "Accent.TButton",
            background=[("active", BLUE_DARK)],
        )

        style.configure(
            "Warn.TButton",
            background=YELLOW,
            foreground=TEXT,
            font=("Segoe UI", 10, "bold"),
            padding=8,
        )

    # -------------------- UI --------------------
    def _build_ui(self):
        header = tk.Frame(self, bg=BLUE, height=56)
        header.pack(fill="x")

        tk.Label(
            header,
            text="🎵 Discord Music Bot Manager",
            bg=BLUE,
            fg="white",
            font=("Segoe UI", 16, "bold"),
        ).pack(side="left", padx=16, pady=10)

        toolbar = tk.Frame(self, bg=BG)
        toolbar.pack(fill="x", padx=12, pady=8)

        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            toolbar,
            text="Auto refresh",
            variable=self.auto_var,
        ).pack(side="left")

        ttk.Label(toolbar, text="Refresh (ms):").pack(side="left", padx=(12, 4))
        self.refresh_ms = tk.IntVar(value=1000)
        ttk.Entry(toolbar, width=7, textvariable=self.refresh_ms).pack(side="left")

        self.tree = ttk.Treeview(
            self,
            columns=("state", "uptime", "guild", "voice", "track", "queue"),
            show="headings",
        )

        columns = [
            ("state", 100),
            ("uptime", 90),
            ("guild", 180),
            ("voice", 180),
            ("track", 520),
            ("queue", 80),
        ]

        for c, w in columns:
            self.tree.heading(c, text=c.upper())
            self.tree.column(c, width=w, anchor="w")

        self.tree.pack(fill="both", expand=True, padx=12, pady=6)

        controls = tk.Frame(self, bg=BG)
        controls.pack(fill="x", padx=12, pady=10)

        ttk.Button(controls, text="▶ Start", style="Accent.TButton", command=self.start_sel).pack(side="left", padx=4)
        ttk.Button(controls, text="■ Stop", style="Warn.TButton", command=self.stop_sel).pack(side="left", padx=4)
        ttk.Button(controls, text="↻ Restart", style="Accent.TButton", command=self.restart_sel).pack(side="left", padx=4)
        ttk.Button(controls, text="📄 View Logs", command=self.logs_sel).pack(side="left", padx=4)

        ttk.Button(controls, text="Start All", style="Accent.TButton", command=self.start_all).pack(side="right", padx=4)
        ttk.Button(controls, text="Stop All", style="Warn.TButton", command=self.stop_all).pack(side="right", padx=4)

        for inst in self.instances:
            self.tree.insert("", "end", iid=inst.name, values=("unknown", "", "", "", "", ""))

    # -------------------- ACTIONS --------------------
    def selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select instance", "Please select an instance first.")
            return None
        return next(i for i in self.instances if i.name == sel[0])

    def start_sel(self):
        if (i := self.selected()):
            i.start()

    def stop_sel(self):
        if (i := self.selected()):
            i.stop()

    def restart_sel(self):
        if (i := self.selected()):
            i.restart()

    def start_all(self):
        for i in self.instances:
            i.start()

    def stop_all(self):
        for i in self.instances:
            i.stop()

    def logs_sel(self):
        i = self.selected()
        if not i:
            return
        try:
            text = i.fetch_logs(tail=500)
        except Exception as e:
            text = f"Failed to load logs:\n{e}"

        win = tk.Toplevel(self)
        win.title(f"Logs — {i.name}")
        win.geometry("980x540")

        box = tk.Text(win, wrap="none", bg="#0E1116", fg="#E6EDF3", insertbackground="white")
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        box.configure(state="disabled")

    # -------------------- POLLING --------------------
    def poll_loop(self):
        while not self._stop_flag:
            if not self.auto_var.get():
                time.sleep(0.25)
                continue

            for i in self.instances:
                state = "stopped"
                uptime = guild = voice = track = queue = ""

                if i.is_running():
                    state = "running"

                try:
                    s = i.fetch_status()
                    state = "online"
                    uptime = str(s.get("uptime_sec", ""))

                    v = s.get("voice") or {}
                    guild = (v.get("guild") or "")[:50]
                    voice = (v.get("channel") or "")[:50]

                    cur = s.get("current") or {}
                    title = cur.get("title") or ""
                    rem = cur.get("remaining")

                    if title:
                        title = title[:140] + ("…" if len(title) > 140 else "")
                        track = title
                        if rem is not None:
                            track += f" | rem {rem}s"

                    queue = str(s.get("queue_len", ""))
                except Exception:
                    if i.is_running():
                        state = "starting…"

                self.tree.set(i.name, "state", state)
                self.tree.set(i.name, "uptime", uptime)
                self.tree.set(i.name, "guild", guild)
                self.tree.set(i.name, "voice", voice)
                self.tree.set(i.name, "track", track)
                self.tree.set(i.name, "queue", queue)

            time.sleep(max(250, int(self.refresh_ms.get() or 1000)) / 1000)


def load_instances():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [Instance(x) for x in data["instances"]]


if __name__ == "__main__":
    App(load_instances()).mainloop()