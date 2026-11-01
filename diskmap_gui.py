#!/usr/bin/env python3
"""
Tkinter front end for diskmap.py — pick a folder, set the options, get a report.

Launched by `python diskmap.py` with no path, or `python diskmap.py --gui`.
The scan runs on a worker thread and reports back through a queue, so the
window stays responsive and Cancel actually works.
"""

import os
import queue
import threading
import webbrowser

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from diskmap import DEFAULT_EXCLUDES, Cancelled, human_size, run_report

BG = "#081522"
PANEL = "#0D2338"
INK = "#F2EFE9"
CYAN = "#4FB3D9"
SLATE = "#7C90A3"
AMBER = "#E8A33D"


class DiskmapGUI:
    def __init__(self, root, args=None):
        self.root = root
        self.queue = queue.Queue()
        self.cancel_event = None
        self.worker = None
        self.last_report = None

        root.title("diskmap")
        root.configure(bg=BG)
        root.minsize(640, 560)

        self._init_style()
        self._build_widgets()
        self._apply_args(args)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._drain_queue)

    # ---------------------------------------------------------------- layout

    def _init_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=INK, fieldbackground=PANEL)
        style.configure("TFrame", background=BG)
        style.configure("TLabelframe", background=BG, foreground=CYAN, bordercolor="#2A4A6B")
        style.configure("TLabelframe.Label", background=BG, foreground=CYAN)
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Dim.TLabel", background=BG, foreground=SLATE)
        style.configure("Status.TLabel", background=BG, foreground=AMBER)
        style.configure("TCheckbutton", background=BG, foreground=INK)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TButton", background=PANEL, foreground=INK, bordercolor="#2A4A6B",
                        focuscolor=BG, padding=5)
        style.map("TButton", background=[("active", "#12314F"), ("disabled", BG)],
                  foreground=[("disabled", SLATE)])
        style.configure("TEntry", fieldbackground=PANEL, foreground=INK,
                        insertcolor=INK, bordercolor="#2A4A6B")
        style.configure("TSpinbox", fieldbackground=PANEL, foreground=INK,
                        insertcolor=INK, arrowcolor=CYAN)
        style.configure("Horizontal.TProgressbar", background=CYAN, troughcolor=PANEL,
                        bordercolor="#2A4A6B", lightcolor=CYAN, darkcolor=CYAN)

    def _build_widgets(self):
        pad = dict(padx=10, pady=4)
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True, padx=12, pady=12)
        outer.columnconfigure(1, weight=1)

        title = tk.Label(outer, text="diskmap", bg=BG, fg=INK,
                         font=("Segoe UI", 18, "bold"))
        title.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 2))
        ttk.Label(outer, text="Local disk usage report — nothing leaves this machine.",
                  style="Dim.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 12))

        self.folder_var = tk.StringVar()
        ttk.Label(outer, text="Folder").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(outer, textvariable=self.folder_var).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(outer, text="Browse…", command=self.pick_folder).grid(row=2, column=2, **pad)

        self.output_var = tk.StringVar(value=os.path.abspath("diskmap_report.html"))
        ttk.Label(outer, text="Save report to").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(outer, textvariable=self.output_var).grid(row=3, column=1, sticky="ew", **pad)
        ttk.Button(outer, text="Browse…", command=self.pick_output).grid(row=3, column=2, **pad)

        self.exclude_var = tk.StringVar(value=", ".join(DEFAULT_EXCLUDES))
        ttk.Label(outer, text="Exclude").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(outer, textvariable=self.exclude_var).grid(row=4, column=1, sticky="ew", **pad)
        ttk.Label(outer, text="comma-separated globs", style="Dim.TLabel").grid(row=4, column=2, **pad)

        opts = ttk.Labelframe(outer, text=" Options ")
        opts.grid(row=5, column=0, columnspan=3, sticky="ew", padx=10, pady=(12, 6))
        for col in (1, 3):
            opts.columnconfigure(col, weight=1)

        self.treemap_depth = tk.IntVar(value=4)
        self.tree_depth = tk.IntVar(value=0)
        self.max_children = tk.IntVar(value=400)
        self.top_n = tk.IntVar(value=100)
        self.min_age = tk.IntVar(value=365)

        specs = [
            ("Treemap depth", self.treemap_depth, 1, 20),
            ("Tree depth (0 = all)", self.tree_depth, 0, 40),
            ("Max items per folder", self.max_children, 10, 100000),
            ("Rows in tables", self.top_n, 10, 1000),
            ("Untouched after (days)", self.min_age, 1, 10000),
        ]
        for i, (label, var, lo, hi) in enumerate(specs):
            r, c = divmod(i, 2)
            ttk.Label(opts, text=label).grid(row=r, column=c * 2, sticky="w", padx=8, pady=4)
            ttk.Spinbox(opts, from_=lo, to=hi, textvariable=var, width=10).grid(
                row=r, column=c * 2 + 1, sticky="w", padx=8, pady=4)

        self.open_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Open the report when it is finished",
                        variable=self.open_var).grid(row=3, column=0, columnspan=4,
                                                     sticky="w", padx=8, pady=(8, 6))

        bar = ttk.Frame(outer)
        bar.grid(row=6, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 4))
        self.scan_btn = ttk.Button(bar, text="Scan", command=self.start_scan)
        self.scan_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=self.cancel_scan, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(bar, text="Open report", command=self.open_report, state="disabled")
        self.open_btn.pack(side="left")
        ttk.Button(bar, text="Quit", command=self.on_close).pack(side="right")

        self.progress = ttk.Progressbar(outer, mode="indeterminate", style="Horizontal.TProgressbar")
        self.progress.grid(row=7, column=0, columnspan=3, sticky="ew", padx=10, pady=(6, 2))

        self.status_var = tk.StringVar(value="Pick a folder to get started.")
        ttk.Label(outer, textvariable=self.status_var, style="Status.TLabel").grid(
            row=8, column=0, columnspan=3, sticky="w", padx=10)

        self.log = tk.Text(outer, height=9, bg=PANEL, fg=INK, insertbackground=INK,
                           relief="flat", wrap="none", font=("Consolas", 9),
                           highlightthickness=1, highlightbackground="#2A4A6B")
        self.log.grid(row=9, column=0, columnspan=3, sticky="nsew", padx=10, pady=(8, 0))
        self.log.configure(state="disabled")
        outer.rowconfigure(9, weight=1)

    def _apply_args(self, args):
        if not args:
            return
        if getattr(args, "path", None):
            self.folder_var.set(os.path.abspath(args.path))
        if getattr(args, "output", None):
            self.output_var.set(os.path.abspath(args.output))
        if getattr(args, "exclude", None):
            merged = list(dict.fromkeys(DEFAULT_EXCLUDES + list(args.exclude)))
            self.exclude_var.set(", ".join(merged))
        self.treemap_depth.set(getattr(args, "treemap_depth", 4))
        self.tree_depth.set(getattr(args, "max_tree_depth", None) or 0)
        self.max_children.set(getattr(args, "max_children", 400))
        self.top_n.set(getattr(args, "top", 100))
        self.min_age.set(getattr(args, "min_age_days", 365))
        if getattr(args, "open", False):
            self.open_var.set(True)

    # --------------------------------------------------------------- actions

    def pick_folder(self):
        chosen = filedialog.askdirectory(title="Choose a folder to scan",
                                         initialdir=self.folder_var.get() or os.path.expanduser("~"))
        if chosen:
            self.folder_var.set(os.path.normpath(chosen))
            name = os.path.basename(os.path.normpath(chosen)) or "root"
            current = self.output_var.get()
            if not current or os.path.basename(current) == "diskmap_report.html":
                self.output_var.set(os.path.join(os.path.dirname(current) or os.getcwd(),
                                                 f"diskmap_{name}.html"))

    def pick_output(self):
        chosen = filedialog.asksaveasfilename(
            title="Save the report as", defaultextension=".html",
            initialfile=os.path.basename(self.output_var.get()) or "diskmap_report.html",
            initialdir=os.path.dirname(self.output_var.get()) or os.getcwd(),
            filetypes=[("HTML report", "*.html"), ("All files", "*.*")])
        if chosen:
            self.output_var.set(os.path.normpath(chosen))

    def start_scan(self):
        if self.worker and self.worker.is_alive():
            return
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Pick a folder", "Choose a folder that exists before scanning.")
            return
        output = self.output_var.get().strip()
        if not output:
            messagebox.showerror("Pick an output file", "Choose where to save the report.")
            return

        excludes = [p.strip() for p in self.exclude_var.get().split(",") if p.strip()]
        tree_depth = self.tree_depth.get() or None

        self._clear_log()
        self._set_running(True)
        self.cancel_event = threading.Event()
        self.worker = threading.Thread(
            target=self._work,
            args=(folder, output, excludes, tree_depth, self.treemap_depth.get(),
                  self.max_children.get(), self.top_n.get(), self.min_age.get(),
                  self.cancel_event),
            daemon=True)
        self.worker.start()

    def cancel_scan(self):
        if self.cancel_event:
            self.cancel_event.set()
            self.status_var.set("Cancelling …")

    def open_report(self):
        if self.last_report and os.path.exists(self.last_report):
            webbrowser.open("file:///" + self.last_report.replace(os.sep, "/"))

    def on_close(self):
        if self.cancel_event:
            self.cancel_event.set()
        self.root.destroy()

    # ---------------------------------------------------------------- worker

    def _work(self, folder, output, excludes, tree_depth, treemap_depth,
              max_children, top_n, min_age, cancel_event):
        def progress(files, dirs, path):
            self.queue.put(("progress", (files, dirs, path)))

        try:
            out_path = run_report(
                folder, output, excludes, tree_depth, treemap_depth, max_children,
                top_n, min_age, progress=progress,
                cancel=cancel_event, log=lambda msg: self.queue.put(("log", msg)))
        except Cancelled:
            self.queue.put(("cancelled", None))
        except Exception as exc:  # surfaced in a dialog rather than a dead window
            self.queue.put(("error", f"{type(exc).__name__}: {exc}"))
        else:
            size = os.path.getsize(out_path)
            self.queue.put(("done", (out_path, size)))

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "progress":
                    files, dirs, path = payload
                    self.status_var.set(f"{files:,} files · {dirs:,} folders · {self._short(path)}")
                elif kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    out_path, size = payload
                    self.last_report = out_path
                    self._set_running(False)
                    self.open_btn.configure(state="normal")
                    self.status_var.set(f"Done — report is {human_size(size)}.")
                    self._append_log("Done.")
                    if self.open_var.get():
                        self.open_report()
                elif kind == "cancelled":
                    self._set_running(False)
                    self.status_var.set("Cancelled.")
                    self._append_log("Cancelled before the report was written.")
                elif kind == "error":
                    self._set_running(False)
                    self.status_var.set("Failed.")
                    self._append_log(payload)
                    messagebox.showerror("diskmap failed", payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    # ----------------------------------------------------------------- chrome

    @staticmethod
    def _short(path, limit=68):
        return path if len(path) <= limit else "…" + path[-(limit - 1):]

    def _set_running(self, running):
        self.scan_btn.configure(state="disabled" if running else "normal")
        self.cancel_btn.configure(state="normal" if running else "disabled")
        if running:
            self.open_btn.configure(state="disabled")
            self.progress.start(12)
            self.status_var.set("Scanning …")
        else:
            self.progress.stop()

    def _append_log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


def launch(args=None):
    root = tk.Tk()
    DiskmapGUI(root, args)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch())
