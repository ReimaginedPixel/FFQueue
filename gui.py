"""
gui.py — Tkinter desktop GUI.

Must run in the main thread (Tkinter requirement).
Polls the shared EncoderState every 500 ms via after().

Tabs:
  Queue     — pending + encoding items; add / start / stop controls
  Scheduled — completed/failed items; shows original path; lets user
              open the folder or delete the original manually
"""

import os
import subprocess
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

_QUEUE_TAG_COLORS: dict[str, str] = {
    "encoding": "#1565C0",
    "pending":  "#333333",
}

_SCHED_TAG_COLORS: dict[str, str] = {
    "done":    "#2E7D32",
    "failed":  "#B71C1C",
    "skipped": "#795548",
}

POLL_MS = 500


def _mb(b: int | None) -> str:
    return f"{b / 1_048_576:.1f}" if b else "—"


def _saved(inp: int | None, out: int | None) -> str:
    if not inp or not out:
        return "—"
    return f"{(1 - out / inp) * 100:.1f}%"


def _open_folder(path: str) -> None:
    """Open Windows Explorer at the folder containing path."""
    folder = str(Path(path).parent)
    subprocess.Popen(["explorer", folder])


class App(tk.Tk):
    def __init__(self, queue: "QueueManager", encoder: "EncoderWorker") -> None:
        super().__init__()
        self._queue   = queue
        self._encoder = encoder

        self.title("FFQueue — NVENC Batch Encoder")
        self.geometry("980x660")
        self.minsize(800, 520)

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
        style.configure("Danger.TButton", foreground="#B71C1C", padding=6)

        # --- Toolbar ---
        toolbar = ttk.Frame(self, padding=(8, 6))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Select Files",       command=self._select_files).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Start Encoding",     command=self._start).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Stop After Current", command=self._stop).pack(side="left", padx=3)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(toolbar, text="Remove Selected",    command=self._remove_selected).pack(side="left", padx=3)

        # --- Status panel ---
        sf = ttk.LabelFrame(self, text="Now Encoding", padding=(10, 6))
        sf.pack(fill="x", padx=8, pady=(0, 4))
        sf.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(sf, text="State:").grid(row=row, column=0, sticky="w", padx=(0, 8))
        self._status_var = tk.StringVar(value="idle")
        self._status_lbl = ttk.Label(sf, textvariable=self._status_var,
                                     font=("Segoe UI", 9, "bold"))
        self._status_lbl.grid(row=row, column=1, sticky="w")

        row += 1
        ttk.Label(sf, text="Phase:").grid(row=row, column=0, sticky="w", padx=(0, 8))
        self._phase_var = tk.StringVar(value="")
        ttk.Label(sf, textvariable=self._phase_var, foreground="#555").grid(
            row=row, column=1, sticky="w")

        row += 1
        ttk.Label(sf, text="File:").grid(row=row, column=0, sticky="w", padx=(0, 8))
        self._file_var = tk.StringVar(value="—")
        ttk.Label(sf, textvariable=self._file_var, wraplength=780,
                  justify="left").grid(row=row, column=1, sticky="w")

        row += 1
        ttk.Label(sf, text="Progress:").grid(row=row, column=0, sticky="w", padx=(0, 8))
        prog_frame = ttk.Frame(sf)
        prog_frame.grid(row=row, column=1, sticky="ew", pady=2)
        self._progressbar = ttk.Progressbar(prog_frame, length=540, maximum=100,
                                             mode="determinate")
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

        # --- Notebook ---
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self._build_queue_tab()
        self._build_scheduled_tab()

    # ------------------------------------------------------------------
    # Queue tab

    def _build_queue_tab(self) -> None:
        frame = ttk.Frame(self._nb, padding=(4, 4))
        self._nb.add(frame, text="  Queue  ")

        cols = ("file", "status")
        self._qtree = ttk.Treeview(frame, columns=cols, show="headings",
                                   selectmode="extended")
        self._qtree.heading("file",   text="File",   anchor="w")
        self._qtree.heading("status", text="Status", anchor="center")
        self._qtree.column("file",   stretch=True, anchor="w",      minwidth=200)
        self._qtree.column("status", width=110,    anchor="center", stretch=False)

        for tag, color in _QUEUE_TAG_COLORS.items():
            self._qtree.tag_configure(tag, foreground=color)

        vsb = ttk.Scrollbar(frame, orient="vertical",   command=self._qtree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self._qtree.xview)
        self._qtree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._qtree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    # ------------------------------------------------------------------
    # Scheduled tab  (done / failed — original files managed here)

    def _build_scheduled_tab(self) -> None:
        frame = ttk.Frame(self._nb, padding=(4, 4))
        self._nb.add(frame, text="  Scheduled  ")

        # --- Action bar ---
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 4))

        self._sched_summary_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._sched_summary_var,
                  foreground="#2E7D32",
                  font=("Segoe UI", 9, "bold")).pack(side="left", padx=4)

        # Right-side buttons operate on selected row
        ttk.Button(bar, text="Open Original Folder",
                   command=self._open_original_folder).pack(side="right", padx=4)
        ttk.Button(bar, text="Delete Original File",
                   style="Danger.TButton",
                   command=self._delete_original).pack(side="right", padx=4)
        ttk.Button(bar, text="Clear List",
                   command=self._clear_scheduled).pack(side="right", padx=4)

        # --- Treeview ---
        cols = ("file", "status", "original_exists",
                "input_mb", "output_mb", "saved",
                "encoder", "completed", "output_path")
        self._stree = ttk.Treeview(frame, columns=cols, show="headings",
                                   selectmode="browse")

        self._stree.heading("file",            text="File",            anchor="w")
        self._stree.heading("status",          text="Status",          anchor="center")
        self._stree.heading("original_exists", text="Original",        anchor="center")
        self._stree.heading("input_mb",        text="Input MB",        anchor="e")
        self._stree.heading("output_mb",       text="Output MB",       anchor="e")
        self._stree.heading("saved",           text="Saved",           anchor="center")
        self._stree.heading("encoder",         text="Encoder",         anchor="center")
        self._stree.heading("completed",       text="Completed",       anchor="center")
        self._stree.heading("output_path",     text="Output Location", anchor="w")

        self._stree.column("file",            stretch=True,  anchor="w",      minwidth=160, width=200)
        self._stree.column("status",          width=70,      anchor="center", stretch=False)
        self._stree.column("original_exists", width=75,      anchor="center", stretch=False)
        self._stree.column("input_mb",        width=80,      anchor="e",      stretch=False)
        self._stree.column("output_mb",       width=80,      anchor="e",      stretch=False)
        self._stree.column("saved",           width=65,      anchor="center", stretch=False)
        self._stree.column("encoder",         width=100,     anchor="center", stretch=False)
        self._stree.column("completed",       width=140,     anchor="center", stretch=False)
        self._stree.column("output_path",     stretch=True,  anchor="w",      minwidth=160, width=240)

        for tag, color in _SCHED_TAG_COLORS.items():
            self._stree.tag_configure(tag, foreground=color)
        # Extra tag for deleted originals
        self._stree.tag_configure("original_gone", foreground="#9E9E9E")

        # Sub-frame for treeview + scrollbars (grid manager stays inside here)
        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",   command=self._stree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self._stree.xview)
        self._stree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._stree.grid(row=0, column=0, sticky="nsew", in_=tree_frame)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

    # ------------------------------------------------------------------
    # Button handlers — toolbar

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
            messagebox.showinfo("FFQueue", f"Added {added} file(s) to queue.")

    def _start(self) -> None:
        self._encoder.start()

    def _stop(self) -> None:
        self._encoder.request_stop()

    def _remove_selected(self) -> None:
        for iid in self._qtree.selection():
            self._queue.remove_item(iid)

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
    # Button handlers — Scheduled tab

    def _selected_scheduled_item(self) -> dict | None:
        sel = self._stree.selection()
        if not sel:
            messagebox.showinfo("FFQueue", "Select a row first.")
            return None
        iid = sel[0]
        for item in self._queue.get_all():
            if item["id"] == iid:
                return item
        return None

    def _open_original_folder(self) -> None:
        item = self._selected_scheduled_item()
        if not item:
            return
        original = item.get("file_path", "")
        if not original:
            return
        folder = Path(original).parent
        if folder.exists():
            subprocess.Popen(["explorer", str(folder)])
        else:
            messagebox.showwarning("FFQueue", f"Folder not found:\n{folder}")

    def _delete_original(self) -> None:
        item = self._selected_scheduled_item()
        if not item:
            return
        original = item.get("file_path", "")
        if not original:
            return
        p = Path(original)
        if not p.exists():
            messagebox.showinfo("FFQueue", "Original file is already gone.")
            return
        if not messagebox.askyesno(
            "Delete Original",
            f"Permanently delete the original file?\n\n{original}\n\n"
            "This cannot be undone.",
            icon="warning",
        ):
            return
        try:
            p.unlink()
            messagebox.showinfo("FFQueue", f"Deleted:\n{original}")
        except OSError as exc:
            messagebox.showerror("FFQueue", f"Could not delete file:\n{exc}")

    def _clear_scheduled(self) -> None:
        self._queue.clear_finished()

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
        self._refresh_scheduled()
        self.after(POLL_MS, self._poll)

    # ------------------------------------------------------------------
    # Queue tab refresh

    def _refresh_queue(self) -> None:
        items = self._queue.get_all()
        seen: set[str] = set()
        pending = encoding = 0

        for item in items:
            st = item["status"]
            if st not in ("pending", "encoding"):
                continue
            iid   = item["id"]
            fname = Path(item["file_path"]).name
            tag   = st
            seen.add(iid)
            if st == "pending":
                pending += 1
            else:
                encoding += 1

            if self._qtree.exists(iid):
                self._qtree.item(iid, values=(fname, st), tags=(tag,))
            else:
                self._qtree.insert("", "end", iid=iid, values=(fname, st), tags=(tag,))

        for iid in self._qtree.get_children():
            if iid not in seen:
                self._qtree.delete(iid)

        label = f"  Queue ({pending} pending"
        if encoding:
            label += ", 1 encoding"
        label += ")  "
        self._nb.tab(0, text=label)

    # ------------------------------------------------------------------
    # Scheduled tab refresh

    def _refresh_scheduled(self) -> None:
        items     = self._queue.get_all()
        hist      = [i for i in items if i["status"] in ("done", "failed")]
        seen: set[str] = set()

        total_saved = 0
        done_count  = 0
        fail_count  = 0

        for item in hist:
            iid    = item["id"]
            status = item["status"]
            seen.add(iid)

            fname     = Path(item["file_path"]).name
            inp       = item.get("input_size_bytes")
            out       = item.get("output_size_bytes")
            encoder   = item.get("encoder_used") or "—"
            completed = (item.get("completed_at") or "")[:19].replace("T", " ")
            final     = item.get("final_path") or "—"

            # Check if original still exists on disk
            orig_path = item.get("file_path", "")
            orig_exists = Path(orig_path).exists() if orig_path else False
            orig_label  = "exists" if orig_exists else "deleted"

            skipped = "skipped" in encoder.lower()
            if skipped:
                tag = "skipped"
            elif not orig_exists and status == "done":
                tag = "original_gone"
            else:
                tag = status

            values = (
                fname,
                status,
                orig_label,
                _mb(inp),
                _mb(out),
                _saved(inp, out),
                encoder,
                completed,
                final,
            )

            if self._stree.exists(iid):
                self._stree.item(iid, values=values, tags=(tag,))
            else:
                # Insert at top so newest appears first
                self._stree.insert("", 0, iid=iid, values=values, tags=(tag,))

            if status == "done" and inp and out and not skipped:
                total_saved += max(inp - out, 0)
                done_count  += 1
            elif status == "failed":
                fail_count  += 1

        for iid in self._stree.get_children():
            if iid not in seen:
                self._stree.delete(iid)

        # Summary line
        parts: list[str] = []
        if done_count:
            parts.append(f"{done_count} encoded")
        if fail_count:
            parts.append(f"{fail_count} failed")
        if total_saved:
            parts.append(f"{total_saved / 1_073_741_824:.2f} GB reclaimed")
        self._sched_summary_var.set("  " + "  ·  ".join(parts) if parts else "")

        total = len(hist)
        self._nb.tab(1, text=f"  Scheduled ({total})  " if total else "  Scheduled  ")
