"""
gui.py — Tkinter desktop GUI.

Must run in the main thread (Tkinter requirement).
Polls the shared EncoderState every 500 ms via after().
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
    "skipped":  "#6A1E55",
    "pending":  "#333333",
}

POLL_MS = 500


class App(tk.Tk):
    def __init__(self, queue: "QueueManager", encoder: "EncoderWorker") -> None:
        super().__init__()
        self._queue = queue
        self._encoder = encoder

        self.title("FFQueue — NVENC Batch Encoder")
        self.geometry("860x580")
        self.minsize(720, 480)
        self.configure(bg="#F5F5F5")

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

        # --- Toolbar ---
        toolbar = ttk.Frame(self, padding=(8, 6))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Select Files",         command=self._select_files).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Start Encoding",       command=self._start).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Stop After Current",   command=self._stop).pack(side="left", padx=3)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(toolbar, text="Clear Finished",       command=self._clear).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Remove Selected",      command=self._remove_selected).pack(side="left", padx=3)

        # --- Status panel ---
        sf = ttk.LabelFrame(self, text="Status", padding=(10, 6))
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
        ttk.Label(sf, textvariable=self._file_var, wraplength=680, justify="left").grid(row=row, column=1, sticky="w")

        row += 1
        ttk.Label(sf, text="Progress:").grid(row=row, column=0, sticky="w", padx=(0, 8))
        prog_frame = ttk.Frame(sf)
        prog_frame.grid(row=row, column=1, sticky="ew", pady=2)
        self._progressbar = ttk.Progressbar(prog_frame, length=460, maximum=100, mode="determinate")
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

        # --- Queue list ---
        qf = ttk.LabelFrame(self, text="Queue", padding=(8, 4))
        qf.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        cols = ("file", "status")
        self._tree = ttk.Treeview(qf, columns=cols, show="headings", selectmode="extended")
        self._tree.heading("file",   text="File",   anchor="w")
        self._tree.heading("status", text="Status", anchor="center")
        self._tree.column("file",   stretch=True,  anchor="w",      minwidth=200)
        self._tree.column("status", width=110,     anchor="center", stretch=False)

        for tag, color in _TAG_COLORS.items():
            self._tree.tag_configure(tag, foreground=color)

        vsb = ttk.Scrollbar(qf, orient="vertical",   command=self._tree.yview)
        hsb = ttk.Scrollbar(qf, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        qf.rowconfigure(0, weight=1)
        qf.columnconfigure(0, weight=1)

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

    def _remove_selected(self) -> None:
        selected = self._tree.selection()
        if not selected:
            return
        for iid in selected:
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
        self.after(POLL_MS, self._poll)

    def _refresh_queue(self) -> None:
        items = self._queue.get_all()
        seen_ids: set[str] = set()

        for item in items:
            item_id = item["id"]
            fname   = Path(item["file_path"]).name
            status  = item["status"]
            tag     = status if status in _TAG_COLORS else "pending"
            seen_ids.add(item_id)

            if self._tree.exists(item_id):
                self._tree.item(item_id, values=(fname, status), tags=(tag,))
            else:
                self._tree.insert("", "end", iid=item_id, values=(fname, status), tags=(tag,))

        # Remove rows no longer in the queue
        for iid in self._tree.get_children():
            if iid not in seen_ids:
                self._tree.delete(iid)
