import os
import sys
import threading
import winreg
from collections import deque
from datetime import datetime

if sys.platform != "win32":
    print("PC Health Dashboard runs on Windows only.")
    sys.exit(1)

try:
    import psutil
except ImportError:
    import tkinter as tk
    from tkinter import messagebox

    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        "PC Health Dashboard",
        "Install the psutil package: pip install psutil",
    )
    sys.exit(1)

try:
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
except ImportError:
    import tkinter as tk
    from tkinter import messagebox

    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        "PC Health Dashboard",
        "Install matplotlib: pip install matplotlib",
    )
    sys.exit(1)

import tkinter as tk
from tkinter import messagebox
from tkinter import ttk

BG = "#16161e"
CARD = "#1f1f2e"
CARD2 = "#252538"
FG = "#e4e4ef"
MUTED = "#8b8ba3"
ACCENT = "#6c8cff"
ACCENT2 = "#a78bfa"
GRID = "#2e2e42"
BAR_USED = "#6c8cff"


def _format_bytes(n):
    if n < 1024:
        return f"{n} B"
    for u in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024.0:
            return f"{n:.1f} {u}"
    return f"{n:.1f} PB"


def _format_uptime(seconds):
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    m = int((seconds % 3600) // 60)
    if d > 0:
        return f"{d}d {h}h {m}m"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def _read_run_key(root, subkey):
    out = []
    label = "HKEY_CURRENT_USER" if root == winreg.HKEY_CURRENT_USER else "HKEY_LOCAL_MACHINE"
    try:
        k = winreg.OpenKey(root, subkey)
    except OSError:
        return out
    try:
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(k, i)
                if isinstance(value, str) and value.strip():
                    out.append((name, value.strip(), f"{label}\\{subkey}"))
                i += 1
            except OSError:
                break
    finally:
        winreg.CloseKey(k)
    return out


def _collect_startup_items():
    items = []
    run_paths = [
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
    ]
    for p in run_paths:
        items.extend(_read_run_key(winreg.HKEY_CURRENT_USER, p))
    for p in run_paths:
        items.extend(_read_run_key(winreg.HKEY_LOCAL_MACHINE, p))
    seen = {f"{a}|{b.lower()}" for a, b, _ in items}
    startup_dirs = []
    ad = os.environ.get("APPDATA", "")
    pd = os.environ.get("PROGRAMDATA", "")
    if ad:
        startup_dirs.append(
            os.path.join(ad, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        )
    if pd:
        startup_dirs.append(
            os.path.join(pd, "Microsoft", "Windows", "Start Menu", "Programs", "StartUp")
        )
    for folder in startup_dirs:
        if not folder or not os.path.isdir(folder):
            continue
        try:
            for fn in os.listdir(folder):
                fp = os.path.join(folder, fn)
                if os.path.isfile(fp):
                    key = f"{fn}|{fp.lower()}"
                    if key not in seen:
                        seen.add(key)
                        items.append((fn, fp, "Startup folder"))
        except OSError:
            pass
    items.sort(key=lambda x: (x[2], x[0].lower()))
    return items


def _short_name(s, n=28):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


class PcHealthDashboard:
    def __init__(self):
        self._history_cpu = deque(maxlen=72)
        self._history_mem = deque(maxlen=72)
        self._live_after = None
        self._disk_raw = []
        self._proc_raw = []

        self.root = tk.Tk()
        self.root.title("PC Health Dashboard")
        self.root.geometry("1040x760")
        self.root.minsize(900, 620)
        self.root.configure(bg=BG)

        self._style = ttk.Style()
        if sys.platform == "win32":
            self._style.theme_use("clam")
        self._apply_ttk_theme()

        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill=tk.BOTH, expand=True, padx=18, pady=(16, 10))

        head = tk.Frame(outer, bg=BG)
        head.pack(fill=tk.X, pady=(0, 12))
        tk.Label(
            head,
            text="PC Health Dashboard",
            bg=BG,
            fg=FG,
            font=("Segoe UI", 22, "normal"),
        ).pack(side=tk.LEFT)
        self.status_var = tk.StringVar(value="Ready")
        btn_f = tk.Frame(head, bg=BG)
        btn_f.pack(side=tk.RIGHT)
        self._btn_refresh = ttk.Button(btn_f, text="Refresh", style="Accent.TButton", command=self._schedule_refresh)
        self._btn_refresh.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Label(btn_f, textvariable=self.status_var, style="Muted.TLabel").pack(side=tk.RIGHT)

        nb = ttk.Notebook(outer)
        nb.pack(fill=tk.BOTH, expand=True)

        self._overview = tk.Frame(nb, bg=BG)
        self._disk = tk.Frame(nb, bg=BG)
        self._proc = tk.Frame(nb, bg=BG)
        self._startup = tk.Frame(nb, bg=BG)
        nb.add(self._overview, text="  Overview  ")
        nb.add(self._disk, text="  Disk  ")
        nb.add(self._proc, text="  Processes  ")
        nb.add(self._startup, text="  Startup  ")

        self._card_cpu = tk.StringVar(value="—")
        self._card_mem = tk.StringVar(value="—")
        self._card_up = tk.StringVar(value="—")
        self._card_swap = tk.StringVar(value="—")
        self._ov_os = tk.StringVar(value="")

        cards = tk.Frame(self._overview, bg=BG)
        cards.pack(fill=tk.X, pady=(0, 12))
        for i, (title, var) in enumerate(
            [
                ("CPU", self._card_cpu),
                ("Memory", self._card_mem),
                ("Uptime", self._card_up),
                ("Swap", self._card_swap),
            ]
        ):
            c = tk.Frame(cards, bg=CARD, highlightthickness=1, highlightbackground=GRID)
            c.grid(row=0, column=i, padx=(0, 10 if i < 3 else 0), sticky=tk.EW)
            cards.columnconfigure(i, weight=1)
            tk.Label(c, text=title.upper(), bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(anchor=tk.W, padx=14, pady=(12, 2))
            tk.Label(
                c,
                textvariable=var,
                bg=CARD,
                fg=FG,
                font=("Segoe UI", 20, "normal"),
            ).pack(anchor=tk.W, padx=14, pady=(0, 14))

        chart_frame = tk.Frame(self._overview, bg=CARD, highlightthickness=1, highlightbackground=GRID)
        chart_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        tk.Label(
            chart_frame,
            text="Live load (recent samples)",
            bg=CARD,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).pack(anchor=tk.W, padx=14, pady=(12, 4))

        self._fig_live = Figure(figsize=(10, 3.4), dpi=100, facecolor=CARD)
        self._ax_cpu = self._fig_live.add_subplot(211)
        self._ax_mem = self._fig_live.add_subplot(212, sharex=self._ax_cpu)
        self._fig_live.subplots_adjust(left=0.09, right=0.97, top=0.92, bottom=0.12, hspace=0.35)
        self._canvas_live = FigureCanvasTkAgg(self._fig_live, master=chart_frame)
        self._canvas_live.get_tk_widget().configure(bg=CARD, highlightthickness=0)
        self._canvas_live.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 10))

        os_row = tk.Frame(self._overview, bg=BG)
        os_row.pack(fill=tk.X)
        tk.Label(os_row, textvariable=self._ov_os, bg=BG, fg=MUTED, font=("Segoe UI", 10), wraplength=980, justify=tk.LEFT).pack(anchor=tk.W)

        disk_top = tk.Frame(self._disk, bg=BG)
        disk_top.pack(fill=tk.BOTH, expand=True, pady=(4, 8))
        self._fig_disk = Figure(figsize=(10, 3.2), dpi=100, facecolor=CARD)
        self._ax_disk = self._fig_disk.add_subplot(111)
        self._fig_disk.subplots_adjust(left=0.18, right=0.96, top=0.92, bottom=0.15)
        self._canvas_disk = FigureCanvasTkAgg(self._fig_disk, master=disk_top)
        self._canvas_disk.get_tk_widget().configure(bg=CARD, highlightthickness=0)
        self._canvas_disk.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        disk_bot = tk.Frame(self._disk, bg=BG)
        disk_bot.pack(fill=tk.BOTH, expand=True)
        scroll_d = ttk.Scrollbar(disk_bot)
        scroll_d.pack(side=tk.RIGHT, fill=tk.Y)
        self._disk_tree = ttk.Treeview(
            disk_bot,
            columns=("total", "used", "free", "pct"),
            show="headings",
            height=10,
            yscrollcommand=scroll_d.set,
            style="Dark.Treeview",
        )
        self._disk_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_d.config(command=self._disk_tree.yview)
        for c, w, t in [
            ("#0", 100, "Drive"),
            ("total", 100, "Total"),
            ("used", 100, "Used"),
            ("free", 100, "Free"),
            ("pct", 64, "Used %"),
        ]:
            self._disk_tree.heading(c, text=t)
            self._disk_tree.column(c, width=w, anchor=tk.W if c == "#0" else tk.E)

        proc_top = tk.Frame(self._proc, bg=BG)
        proc_top.pack(fill=tk.BOTH, expand=True, pady=(4, 8))
        self._fig_proc = Figure(figsize=(10, 3.6), dpi=100, facecolor=CARD)
        self._ax_proc = self._fig_proc.add_subplot(111)
        self._fig_proc.subplots_adjust(left=0.22, right=0.96, top=0.92, bottom=0.12)
        self._canvas_proc = FigureCanvasTkAgg(self._fig_proc, master=proc_top)
        self._canvas_proc.get_tk_widget().configure(bg=CARD, highlightthickness=0)
        self._canvas_proc.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        proc_bot = tk.Frame(self._proc, bg=BG)
        proc_bot.pack(fill=tk.BOTH, expand=True)
        scroll_p = ttk.Scrollbar(proc_bot)
        scroll_p.pack(side=tk.RIGHT, fill=tk.Y)
        self._proc_tree = ttk.Treeview(
            proc_bot,
            columns=("pid", "cpu", "rss"),
            show="headings",
            height=14,
            yscrollcommand=scroll_p.set,
            style="Dark.Treeview",
        )
        self._proc_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_p.config(command=self._proc_tree.yview)
        for c, w, t in [
            ("#0", 240, "Name"),
            ("pid", 72, "PID"),
            ("cpu", 64, "CPU %"),
            ("rss", 96, "Memory"),
        ]:
            self._proc_tree.heading(c, text=t)
            self._proc_tree.column(c, width=w, anchor=tk.W if c == "#0" else tk.E)

        sh = tk.Frame(self._startup, bg=BG)
        sh.pack(fill=tk.BOTH, expand=True, padx=4, pady=8)
        scroll_s = ttk.Scrollbar(sh)
        scroll_s.pack(side=tk.RIGHT, fill=tk.Y)
        self._start_tree = ttk.Treeview(
            sh,
            columns=("command", "source"),
            show="headings",
            height=24,
            yscrollcommand=scroll_s.set,
            style="Dark.Treeview",
        )
        self._start_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_s.config(command=self._start_tree.yview)
        for c, w, t in [
            ("#0", 200, "Name"),
            ("command", 440, "Command or path"),
            ("source", 220, "Where listed"),
        ]:
            self._start_tree.heading(c, text=t)
            self._start_tree.column(c, width=w, anchor=tk.W)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._schedule_refresh()

    def _apply_ttk_theme(self):
        s = self._style
        s.configure(".", background=BG, foreground=FG)
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        s.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=CARD2, foreground=MUTED, padding=[18, 10], font=("Segoe UI", 10))
        s.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", FG)])
        s.configure(
            "Accent.TButton",
            font=("Segoe UI", 10),
            padding=[18, 9],
            background=ACCENT,
            foreground="#f0f4ff",
            borderwidth=0,
            focuscolor="none",
        )
        s.map("Accent.TButton", background=[("active", "#5878e6"), ("pressed", "#4a6bd4")])
        s.configure("TButton", background=CARD2, foreground=FG, font=("Segoe UI", 10))
        s.map("TButton", background=[("active", CARD)])
        s.configure(
            "Dark.Treeview",
            background=CARD,
            foreground=FG,
            fieldbackground=CARD,
            borderwidth=0,
            font=("Segoe UI", 9),
            rowheight=24,
        )
        s.configure(
            "Treeview.Heading",
            background=CARD2,
            foreground=FG,
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            borderwidth=0,
        )
        s.map("Dark.Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])

    def _on_close(self):
        if self._live_after is not None:
            self.root.after_cancel(self._live_after)
            self._live_after = None
        self.root.destroy()

    def _schedule_refresh(self):
        self.status_var.set("Refreshing…")
        threading.Thread(target=self._gather, daemon=True).start()

    def _gather(self):
        try:
            cpu_pct = psutil.cpu_percent(interval=0.75)
            vm = psutil.virtual_memory()
            sm = psutil.swap_memory()
            boot = psutil.boot_time()
            now = datetime.now().timestamp()
            uptime_s = max(0, now - boot)
            uname = os.environ.get("OS", "Windows")
            rel = sys.getwindowsversion()
            os_line = f"{uname} ({rel.major}.{rel.minor}, build {rel.build})"

            disk_rows = []
            disk_raw = []
            for part in psutil.disk_partitions(all=False):
                try:
                    if "cdrom" in part.opts and part.fstype == "":
                        continue
                    u = psutil.disk_usage(part.mountpoint)
                    pct = (u.used / u.total * 100) if u.total else 0
                    label = part.device.rstrip("\\")
                    disk_raw.append((label, pct, u.used, u.total))
                    disk_rows.append(
                        (
                            part.device,
                            _format_bytes(u.total),
                            _format_bytes(u.used),
                            _format_bytes(u.free),
                            f"{pct:.0f}%",
                        )
                    )
                except (PermissionError, OSError):
                    continue

            proc_rows = []
            for p in psutil.process_iter(["pid", "name", "memory_info"]):
                try:
                    info = p.info
                    rss = info.get("memory_info")
                    rss = rss.rss if rss else 0
                    proc_rows.append((info.get("name") or "", info.get("pid"), rss))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            proc_rows.sort(key=lambda x: x[2], reverse=True)
            top = proc_rows[:120]
            top_chart = proc_rows[:12]
            out_proc = []
            for name, pid, rss in top:
                try:
                    p = psutil.Process(pid)
                    cpu = p.cpu_percent(interval=None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    cpu = 0.0
                out_proc.append((name or "(unknown)", pid, cpu, _format_bytes(rss)))

            startup = _collect_startup_items()
            proc_chart = [(_short_name(n), r) for n, p, r in top_chart]
            payload = {
                "os_line": os_line,
                "uptime": _format_uptime(uptime_s),
                "cpu_pct": cpu_pct,
                "mem_pct": vm.percent,
                "swap_pct": sm.percent,
                "disks": disk_rows,
                "disk_raw": disk_raw,
                "proc_chart": proc_chart,
                "procs": out_proc,
                "startup": startup,
            }

            self.root.after(0, lambda pl=payload: self._apply(pl))
        except Exception as ex:
            err = str(ex)
            self.root.after(0, lambda e=err: self._fail(e))

    def _apply(self, payload):
        self._ov_os.set(payload["os_line"])
        self._card_cpu.set(f"{payload['cpu_pct']:.0f}%")
        self._card_mem.set(f"{payload['mem_pct']:.0f}%")
        self._card_up.set(payload["uptime"])
        self._card_swap.set(f"{payload['swap_pct']:.0f}%")

        self._history_cpu.append(payload["cpu_pct"])
        self._history_mem.append(payload["mem_pct"])
        self._redraw_live()

        self._disk_raw = payload["disk_raw"]
        self._redraw_disk_chart()

        self._proc_raw = payload["proc_chart"]
        self._redraw_proc_chart()

        for x in self._disk_tree.get_children():
            self._disk_tree.delete(x)
        for row in payload["disks"]:
            self._disk_tree.insert("", tk.END, text=row[0], values=row[1:])

        for x in self._proc_tree.get_children():
            self._proc_tree.delete(x)
        for name, pid, cpu, rss in payload["procs"]:
            self._proc_tree.insert(
                "",
                tk.END,
                text=name,
                values=(pid, f"{cpu:.1f}", rss),
            )

        for x in self._start_tree.get_children():
            self._start_tree.delete(x)
        for name, cmd, src in payload["startup"]:
            self._start_tree.insert("", tk.END, text=name, values=(cmd, src))

        self.status_var.set(
            f"Updated {datetime.now().strftime('%H:%M:%S')} · read-only"
        )
        self._ensure_live()

    def _redraw_live(self):
        self._ax_cpu.clear()
        self._ax_mem.clear()
        xs = list(range(len(self._history_cpu)))
        cu = list(self._history_cpu)
        mu = list(self._history_mem)
        self._ax_cpu.set_facecolor(CARD)
        self._ax_mem.set_facecolor(CARD)
        self._ax_cpu.plot(xs, cu, color=ACCENT, linewidth=2, solid_capstyle="round")
        self._ax_cpu.set_ylim(0, 100)
        self._ax_cpu.set_ylabel("CPU %", color=MUTED, fontsize=9)
        self._ax_cpu.tick_params(colors=MUTED, labelsize=8)
        self._ax_cpu.grid(True, color=GRID, linestyle="-", linewidth=0.8, alpha=0.85)
        self._ax_cpu.spines["bottom"].set_color(GRID)
        self._ax_cpu.spines["top"].set_color(GRID)
        self._ax_cpu.spines["left"].set_color(GRID)
        self._ax_cpu.spines["right"].set_color(GRID)
        self._ax_cpu.set_xticklabels([])
        self._ax_mem.plot(xs, mu, color=ACCENT2, linewidth=2, solid_capstyle="round")
        self._ax_mem.set_ylim(0, 100)
        self._ax_mem.set_ylabel("Memory %", color=MUTED, fontsize=9)
        self._ax_mem.set_xlabel("Sample", color=MUTED, fontsize=8)
        self._ax_mem.tick_params(colors=MUTED, labelsize=8)
        self._ax_mem.grid(True, color=GRID, linestyle="-", linewidth=0.8, alpha=0.85)
        for sp in self._ax_mem.spines.values():
            sp.set_color(GRID)
        self._canvas_live.draw_idle()

    def _redraw_disk_chart(self):
        self._ax_disk.clear()
        self._ax_disk.set_facecolor(CARD)
        if not self._disk_raw:
            self._ax_disk.text(0.5, 0.5, "No drives", ha="center", va="center", color=MUTED, fontsize=11)
            self._canvas_disk.draw_idle()
            return
        labels = [x[0] for x in self._disk_raw]
        vals = [x[1] for x in self._disk_raw]
        y = range(len(labels))
        self._ax_disk.barh(list(y), vals, height=0.65, color=BAR_USED, alpha=0.9, zorder=2)
        self._ax_disk.set_yticks(list(y))
        self._ax_disk.set_yticklabels(labels, color=FG, fontsize=9)
        self._ax_disk.set_xlim(0, 100)
        self._ax_disk.set_xlabel("Space used (%)", color=MUTED, fontsize=9)
        self._ax_disk.tick_params(colors=MUTED, labelsize=8)
        self._ax_disk.grid(True, axis="x", color=GRID, linestyle="-", linewidth=0.8, alpha=0.85)
        for sp in self._ax_disk.spines.values():
            sp.set_color(GRID)
        self._ax_disk.invert_yaxis()
        h = max(2.4, 0.38 * len(labels))
        self._fig_disk.set_size_inches(10, min(h, 12))
        self._canvas_disk.draw_idle()

    def _redraw_proc_chart(self):
        self._ax_proc.clear()
        self._ax_proc.set_facecolor(CARD)
        if not self._proc_raw:
            self._ax_proc.text(0.5, 0.5, "No data", ha="center", va="center", color=MUTED, fontsize=11)
            self._canvas_proc.draw_idle()
            return
        names = [x[0] for x in self._proc_raw]
        rss = [x[1] for x in self._proc_raw]
        mb = [r / (1024 * 1024) for r in rss]
        y = range(len(names))
        self._ax_proc.barh(list(y), mb, height=0.65, color=ACCENT2, alpha=0.88, zorder=2)
        self._ax_proc.set_yticks(list(y))
        self._ax_proc.set_yticklabels(names, color=FG, fontsize=8)
        self._ax_proc.set_xlabel("Memory (MB)", color=MUTED, fontsize=9)
        self._ax_proc.tick_params(colors=MUTED, labelsize=8)
        self._ax_proc.grid(True, axis="x", color=GRID, linestyle="-", linewidth=0.8, alpha=0.85)
        for sp in self._ax_proc.spines.values():
            sp.set_color(GRID)
        self._ax_proc.invert_yaxis()
        self._canvas_proc.draw_idle()

    def _ensure_live(self):
        if self._live_after is not None:
            self.root.after_cancel(self._live_after)
        self._live_after = self.root.after(1400, self._live_tick)

    def _live_tick(self):
        def work():
            try:
                c = psutil.cpu_percent(interval=0.35)
                m = psutil.virtual_memory().percent
                self.root.after(0, lambda: self._push_live(c, m))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()
        self._live_after = self.root.after(1400, self._live_tick)

    def _push_live(self, c, m):
        self._history_cpu.append(c)
        self._history_mem.append(m)
        self._card_cpu.set(f"{c:.0f}%")
        self._card_mem.set(f"{m:.0f}%")
        self._redraw_live()

    def _fail(self, msg):
        self.status_var.set("Refresh failed")
        messagebox.showerror("PC Health Dashboard", msg)

    def run(self):
        self.root.mainloop()


def main():
    PcHealthDashboard().run()


if __name__ == "__main__":
    main()
