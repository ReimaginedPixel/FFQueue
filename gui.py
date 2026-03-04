"""
gui.py — Tkinter desktop GUI.

Must run in the main thread (Tkinter requirement).
Polls the shared EncoderState every 500 ms via after().

Tabs:
  Queue   — pending + encoding items; add / start / stop controls
  History — done + failed items with size stats and final path
"""

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from encoder import EncoderWorker
    from queue_manager import QueueManager

_VIDEO_EXTENSIONS = (
    "*.mkv *.mp4 *.avi *.mov *.ts *.wmv *.flv *.m2ts *.m4v *.webm *.mpg *.mpeg"
)

_STATUS_COLORS: dict[str, str] = {
    "idle":     "#333333",
    "encoding": "#1565C0",
    "stopping": "#E65100",
    "error":    "#B71C1C",
}

_TAG_COLORS: dict[str, str] = {
    "encoding": "#1565C0",
    "done":     "#2E7D32",
    "failed":   "#B71C1C",
    "pending":  "#333333",
}

_HIST_TAG_COLORS: dict[str, str] = {
    "done":    "#2E7D32",
    "failed":  "#B71C1C",
    "skipped": "#6A1E55",
}

POLL_MS = 500


def _mb(b: int | None) -> str:
    if b is None:
        return "—"
    return f"{b / 1_048_576:.1f}"


def _pct(inp: int | None, out: int | None) -> str:
    if not inp or not out:
        return "—"
    return f"{(1 - out / inp) * 100:.1f}%"


class App(tk.Tk):
    def __init__(self, queue: "QueueManager", encoder: "EncoderWorker") -> None:
        super().__init__()
        self._queue = queue
        self._encoder = encoder

        self.title("FFQueue — NVENC Batch Encoder")
        self.geometry("960x640")
        self.minsize(780, 500)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_ui()
        self._poll()

    # ------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TButton", padding=6)
        style.configure("TLabelframe.Label", font=("Segoe UI", 9, "bold"))
        style.configure("TNotebook.Tab", padding=(12, 4))

        # --- Toolbar ---
        toolbar = ttk.Frame(self, padding=(8, 6))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Select Files",       command=self._select_files).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Start Encoding",     command=self._start).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Stop After Current", command=self._stop).pack(side="left", padx=3)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(toolbar, text="Clear Finished",     command=self._clear).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Remove Selected",    command=self._remove_selected).pack(side="left", padx=3)

        # --- Status panel ---
        sf = ttk.LabelFrame(self, text="Now Encoding", padding=(10, 6))
        sf.pack(fill="x", padx=8, pady=(0, 4))
        sf.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(sf, text="State:").grid(row=row, column=0, sticky="w", padx=(0, 8))
        self._status_var = tk.StringVar(value="idle")
        self._status_lbl = ttk.Label(sf, textvariable=self._status_var, font=("Segoe UI", 9, "bold"))
        self._status_lbl.grid(row=row, column=1, sticky="w")

        row += 1
        ttk.Label(sf, text="Phase:").grid(row=row, column=0, sticky="w", padx=(0, 8))
        self._phase_var = tk.StringVar(value="")
        ttk.Label(sf, textvariable=self._phase_var, foreground="#555").grid(row=row, column=1, sticky="w")

        row += 1
        ttk.Label(sf, text="File:").grid(row=row, column=0, sticky="w", padx=(0, 8))
        self._file_var = tk.StringVar(value="—")
        ttk.Label(sf, textvariable=self._file_var, wraplength=750, justify="left").grid(row=row, column=1, sticky="w")

        row += 1
        ttk.Label(sf, text="Progress:").grid(row=row, column=0, sticky="w", padx=(0, 8))
        prog_frame = ttk.Frame(sf)
        prog_frame.grid(row=row, column=1, sticky="ew", pady=2)
        self._progressbar = ttk.Progressbar(prog_frame, length=500, maximum=100, mode="determinate")
        self._progressbar.pack(side="left")
        self._pct_var = tk.StringVar(value="0.0%")
        ttk.Label(prog_frame, textvariable=self._pct_var, width=8).pack(side="left", padx=6)

        row += 1
        ttk.Label(sf, text="ETA:").grid(row=row, column=0, sticky="w", padx=(0, 8))
        self._eta_var = tk.StringVar(value="—")
        ttk.Label(sf, textvariable=self._eta_var).grid(row=row, column=1, sticky="w")

        row += 1
        ttk.Label(sf, text="Pending:").grid(row=row, column=0, sticky="w", padx=(0, 8))
        self._pending_var = tk.StringVar(value="0")
        ttk.Label(sf, textvariable=self._pending_var).grid(row=row, column=1, sticky="w")

        # --- Notebook (Queue / History) ---
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self._build_queue_tab()
        self._build_history_tab()

    def _build_queue_tab(self) -> None:
        frame = ttk.Frame(self._nb, padding=(4, 4))
        self._nb.add(frame, text="  Queue  ")

        cols = ("file", "status")
        self._qtree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        self._qtree.heading("file",   text="File",   anchor="w")
        self._qtree.heading("status", text="Status", anchor="center")
        self._qtree.column("file",   stretch=True, anchor="w",      minwidth=200)
        self._qtree.column("status", width=110,    anchor="center", stretch=False)

        for tag, color in _TAG_COLORS.items():
            self._qtree.tag_configure(tag, foreground=color)

        vsb = ttk.Scrollbar(frame, orient="vertical",   command=self._qtree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self._qtree.xview)
        self._qtree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._qtree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    def _build_history_tab(self) -> None:
        frame = ttk.Frame(self._nb, padding=(4, 4))
        self._nb.add(frame, text="  History  ")

        # Summary bar
        sum_frame = ttk.Frame(frame)
        sum_frame.pack(fill="x", pady=(0, 4))
        self._hist_summary_var = tk.StringVar(value="")
        ttk.Label(sum_frame, textvariable=self._hist_summary_var, foreground="#2E7D32",
                  font=("Segoe UI", 9, "bold")).pack(side="left", padx=4)
        ttk.Button(sum_frame, text="Clear History", command=self._clear_history).pack(side="right", padx=4)

        # History treeview
        cols = ("file", "status", "input_mb", "output_mb", "saved", "encoder", "completed", "location")
        self._htree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")

        self._htree.heading("file",      text="File",       anchor="w")
        self._htree.heading("status",    text="Status",     anchor="center")
        self._htree.heading("input_mb",  text="Input MB",   anchor="e")
        self._htree.heading("output_mb", text="Output MB",  anchor="e")
        self._htree.heading("saved",     text="Saved",      anchor="center")
        self._htree.heading("encoder",   text="Encoder",    anchor="center")
        self._htree.heading("completed", text="Completed",  anchor="center")
        self._htree.heading("location",  text="Final Path", anchor="w")

        self._htree.column("file",      stretch=True,  anchor="w",      minwidth=160, width=200)
        self._htree.column("status",    width=80,      anchor="center", stretch=False)
        self._htree.column("input_mb",  width=80,      anchor="e",      stretch=False)
        self._htree.column("output_mb", width=80,      anchor="e",      stretch=False)
        self._htree.column("saved",     width=70,      anchor="center", stretch=False)
        self._htree.column("encoder",   width=100,     anchor="center", stretch=False)
        self._htree.column("completed", width=140,     anchor="center", stretch=False)
        self._htree.column("location",  stretch=True,  anchor="w",      minwidth=160, width=220)

        for tag, color in _HIST_TAG_COLORS.items():
            self._htree.tag_configure(tag, foreground=color)

        vsb = ttk.Scrollbar(frame, orient="vertical",   command=self._htree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self._htree.xview)
        self._htree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._htree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        hsb_frame = ttk.Frame(frame)
        hsb_frame.pack(fill="x", side="bottom")
        hsb = ttk.Scrollbar(hsb_frame, orient="horizontal", command=self._htree.xview)
        hsb.pack(fill="x")
        self._htree.configure(xscrollcommand=hsb.set)

    # ------------------------------------------------------------------
    # Button handlers

    def _select_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select video files",
            filetypes=[
                ("Video files", _VIDEO_EXTENSIONS),
                ("All files", "*.*"),
            ],
        )
        if paths:
            added = self._queue.add_files(list(paths))
            self._refresh_queue()
            messagebox.showinfo("FFQueue", f"Added {added} file(s) to queue.")

    def _start(self) -> None:
        self._encoder.start()

    def _stop(self) -> None:
        self._encoder.request_stop()

    def _clear(self) -> None:
        self._queue.clear_finished()
        self._refresh_queue()
        self._refresh_history()

    def _clear_history(self) -> None:
        self._queue.clear_finished()
        self._refresh_history()

    def _remove_selected(self) -> None:
        # Remove from queue tab selection
        for iid in self._qtree.selection():
            self._queue.remove_item(iid)
        self._refresh_queue()

    def _on_close(self) -> None:
        if self._encoder.is_alive() and self._encoder.state.status in ("encoding", "stopping"):
            if not messagebox.askyesno(
                "FFQueue",
                "Encoding is in progress.\n\nStop after current file and quit?",
            ):
                return
            self._encoder.request_stop()
        self.destroy()

    # ------------------------------------------------------------------
    # Poll loop

    def _poll(self) -> None:
        snap = self._encoder.state.snapshot()

        status = snap["status"]
        self._status_var.set(status)
        self._status_lbl.configure(foreground=_STATUS_COLORS.get(status, "#333333"))
        self._phase_var.set(snap.get("phase", ""))

        cf = snap["current_file"]
        self._file_var.set(Path(cf).name if cf else "—")

        pct = snap["progress_percent"]
        self._progressbar["value"] = pct
        self._pct_var.set(f"{pct:.1f}%")

        eta = snap["eta_minutes"]
        self._eta_var.set(f"{eta:.1f} min" if eta is not None else "—")
        self._pending_var.set(str(self._queue.get_pending_count()))

        self._refresh_queue()
        self._refresh_history()
        self.after(POLL_MS, self._poll)

    # ------------------------------------------------------------------
    # Queue tab refresh  (pending + encoding only)

    def _refresh_queue(self) -> None:
        items = self._queue.get_all()
        seen: set[str] = set()

        for item in items:
            if item["status"] not in ("pending", "encoding"):
                continue
            iid    = item["id"]
            fname  = Path(item["file_path"]).name
            status = item["status"]
            tag    = status if status in _TAG_COLORS else "pending"
            seen.add(iid)

            if self._qtree.exists(iid):
                self._qtree.item(iid, values=(fname, status), tags=(tag,))
            else:
                self._qtree.insert("", "end", iid=iid, values=(fname, status), tags=(tag,))

        for iid in self._qtree.get_children():
            if iid not in seen:
                self._qtree.delete(iid)

        # Update tab title with pending count
        pending = sum(1 for i in items if i["status"] == "pending")
        encoding = sum(1 for i in items if i["status"] == "encoding")
        label = f"  Queue ({pending} pending"
        if encoding:
            label += ", 1 encoding"
        label += ")  "
        self._nb.tab(0, text=label)

    # ------------------------------------------------------------------
    # History tab refresh  (done + failed)

    def _refresh_history(self) -> None:
        items = self._queue.get_all()
        hist  = [i for i in items if i["status"] in ("done", "failed")]
        seen: set[str] = set()

        total_saved_bytes = 0
        done_count = 0
        fail_count = 0

        for item in hist:
            iid    = item["id"]
            status = item["status"]
            seen.add(iid)

            fname     = Path(item["file_path"]).name
            inp       = item.get("input_size_bytes")
            out       = item.get("output_size_bytes")
            encoder   = item.get("encoder_used") or "—"
            completed = (item.get("completed_at") or "")[:19].replace("T", " ")
            final     = item.get("final_path") or item.get("file_path") or "—"
            # Show only the final path (may be in output_dir or original location)
            final_display = str(final)

            # Strip duplicate tag for skipped-HEVC items stored as done
            if "skipped" in encoder.lower():
                tag = "skipped"
            else:
                tag = status

            values = (
                fname,
                status,
                _mb(inp),
                _mb(out),
                _pct(inp, out),
                encoder,
                completed,
                final_display,
            )

            if self._htree.exists(iid):
                self._htree.item(iid, values=values, tags=(tag,))
            else:
                self._htree.insert("", 0, iid=iid, values=values, tags=(tag,))

            if status == "done" and inp and out:
                total_saved_bytes += max(inp - out, 0)
                done_count += 1
            elif status == "failed":
                fail_count += 1

        for iid in self._htree.get_children():
            if iid not in seen:
                self._htree.delete(iid)

        # Summary line
        saved_gb = total_saved_bytes / 1_073_741_824
        parts = []
        if done_count:
            parts.append(f"{done_count} encoded")
        if fail_count:
            parts.append(f"{fail_count} failed")
        if total_saved_bytes:
            parts.append(f"{saved_gb:.2f} GB reclaimed")
        self._hist_summary_var.set("  " + "  ·  ".join(parts) if parts else "")

        # Update tab title
        total = len(hist)
        self._nb.tab(1, text=f"  History ({total})  " if total else "  History  ")
