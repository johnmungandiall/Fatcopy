"""Fatcopy window — a small tkinter front end for the copy engine.

Design notes that matter for reliability
----------------------------------------
* The copy runs on a worker thread; **no widget is ever touched from that
  thread**.  Workers push messages onto a :class:`queue.Queue` and the main
  thread drains it from a periodic ``after`` callback, which is how tkinter
  must be driven.
* Settings live in ``%APPDATA%\\Fatcopy\\settings.json`` and are written
  atomically with a ``.bak`` of the previous file, so a crash mid-save cannot
  lose the user's choices.
* Closing the window during a copy asks first and then cancels cleanly instead
  of killing the process mid-write.
* Everything the user typed is validated *before* the run starts, so a typo
  produces one clear message rather than a half-finished copy.

"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import __version__
from .core import (
    CopyOptions,
    CopyPlanError,
    CopyStats,
    build_plan,
    human_bytes,
    human_time,
    load_config,
    run_copy,
    save_config,
    verify_plan,
)

APP_TITLE = f"Fatcopy {__version__} — fast copy"

_VERIFY_CHOICES = {
    "Quick (size only)": "size",
    "Full (compare every byte)": "content",
    "Off (fastest, no check)": "none",
}
_VERIFY_LABELS = {value: label for label, value in _VERIFY_CHOICES.items()}

_OVERWRITE_CHOICES = {
    "Replace the old file": "overwrite",
    "Skip files that already exist": "skip",
    "Only copy when the file changed": "newer",
}
_OVERWRITE_LABELS = {value: label for label, value in _OVERWRITE_CHOICES.items()}

_BUFFER_CHOICES = ["1 MB", "4 MB", "8 MB", "16 MB", "32 MB"]
_BUFFER_BYTES = {
    "1 MB": 1024 * 1024,
    "4 MB": 4 * 1024 * 1024,
    "8 MB": 8 * 1024 * 1024,
    "16 MB": 16 * 1024 * 1024,
    "32 MB": 32 * 1024 * 1024,
}

_LOG_LINES = 1500
_MAX_MESSAGE = 400


class FatcopyApp:
    """The whole window.  One instance per process."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = load_config()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.cancel = threading.Event()
        self.stats = CopyStats()
        self.worker: threading.Thread | None = None
        self.plan = None
        self.running = False
        self.started_at = 0.0
        self.log_lines = 0

        self.source_var = tk.StringVar(value=str(self.settings.get("source", "")))
        self.dest_var = tk.StringVar(value=str(self.settings.get("dest", "")))
        self.status_var = tk.StringVar(value="Ready")
        self.detail_var = tk.StringVar(value="Pick a source and a destination, then press Start.")
        self.file_var = tk.StringVar(value="")
        self.move_var = tk.BooleanVar(value=bool(self.settings.get("move", False)))
        self.hidden_var = tk.BooleanVar(value=bool(self.settings.get("skip_hidden", False)))
        self.preserve_var = tk.BooleanVar(value=bool(self.settings.get("preserve", True)))
        self.pipeline_var = tk.BooleanVar(value=bool(self.settings.get("pipeline", True)))
        self.dry_var = tk.BooleanVar(value=False)
        self.exclude_var = tk.StringVar(value=str(self.settings.get("exclude", "")))
        self.threads_var = tk.IntVar(value=int(self.settings.get("threads", 0) or 0))
        self.buffer_var = tk.StringVar(value=str(self.settings.get("buffer_label", "4 MB")))
        self.verify_var = tk.StringVar(
            value=_VERIFY_LABELS.get(str(self.settings.get("verify", "size")), "Quick (size only)")
        )
        self.overwrite_var = tk.StringVar(
            value=_OVERWRITE_LABELS.get(
                str(self.settings.get("overwrite", "overwrite")), "Replace the old file"
            )
        )

        self._build_window()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(150, self._pump)

    # ------------------------------------------------------------------ UI
    def _build_window(self) -> None:
        root = self.root
        root.title(APP_TITLE)
        root.geometry(str(self.settings.get("geometry", "880x620")))
        root.minsize(760, 520)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        outer = ttk.Frame(root, padding=10)
        outer.pack(fill="both", expand=True)

        paths = ttk.LabelFrame(outer, text="What to copy where", padding=8)
        paths.pack(fill="x")
        paths.columnconfigure(1, weight=1)

        ttk.Label(paths, text="From").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(paths, textvariable=self.source_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(paths, text="Folder…", width=10,
                   command=self._pick_source_folder).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(paths, text="File…", width=8,
                   command=self._pick_source_file).grid(row=0, column=3, padx=(4, 0))

        ttk.Label(paths, text="To").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Entry(paths, textvariable=self.dest_var).grid(row=1, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(paths, text="Folder…", width=10,
                   command=self._pick_dest).grid(row=1, column=2, padx=(6, 0), pady=(6, 0))
        ttk.Button(paths, text="Swap", width=8,
                   command=self._swap).grid(row=1, column=3, padx=(4, 0), pady=(6, 0))

        options = ttk.LabelFrame(outer, text="Options", padding=8)
        options.pack(fill="x", pady=(8, 0))
        options.columnconfigure(1, weight=1)
        options.columnconfigure(3, weight=1)

        ttk.Checkbutton(options, text="Move instead of copy (deletes verified sources)",
                        variable=self.move_var).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(options, text="Leave hidden/system files alone",
                        variable=self.hidden_var).grid(row=0, column=2, columnspan=2, sticky="w")
        ttk.Checkbutton(options, text="Keep timestamps and attributes",
                        variable=self.preserve_var).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(options, text="Overlap read and write for big files",
                        variable=self.pipeline_var).grid(row=1, column=2, columnspan=2, sticky="w")
        ttk.Checkbutton(options, text="Preview only (copy nothing)",
                        variable=self.dry_var).grid(row=2, column=0, columnspan=2, sticky="w")

        ttk.Label(options, text="Check each copy").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(options, textvariable=self.verify_var, state="readonly",
                     values=list(_VERIFY_CHOICES)).grid(row=3, column=1, sticky="ew",
                                                        pady=(6, 0))
        ttk.Label(options, text="If the file is already there").grid(row=3, column=2, sticky="w",
                                                                    padx=(10, 0), pady=(6, 0))
        ttk.Combobox(options, textvariable=self.overwrite_var, state="readonly",
                     values=list(_OVERWRITE_CHOICES)).grid(row=3, column=3, sticky="ew",
                                                           pady=(6, 0))

        ttk.Label(options, text="Files at once").grid(row=4, column=0, sticky="w", pady=(6, 0))
        threads = ttk.Spinbox(options, from_=0, to=64, textvariable=self.threads_var, width=8)
        threads.grid(row=4, column=1, sticky="w", pady=(6, 0))
        ttk.Label(options, text="(0 = automatic)").grid(row=4, column=1, sticky="w",
                                                        padx=(70, 0), pady=(6, 0))
        ttk.Label(options, text="Block size").grid(row=4, column=2, sticky="w", padx=(10, 0),
                                                   pady=(6, 0))
        ttk.Combobox(options, textvariable=self.buffer_var, state="readonly", width=10,
                     values=_BUFFER_CHOICES).grid(row=4, column=3, sticky="w", pady=(6, 0))

        ttk.Label(options, text="Skip names like").grid(row=5, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(options, textvariable=self.exclude_var).grid(row=5, column=1, columnspan=3,
                                                               sticky="ew", pady=(6, 0))
        ttk.Label(options, text="separate with commas, e.g. *.tmp, Thumbs.db",
                  foreground="#666").grid(row=6, column=1, columnspan=3, sticky="w")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(10, 0))
        self.start_button = ttk.Button(actions, text="Start copy", command=self._start, width=16)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self._cancel,
                                        state="disabled", width=12)
        self.cancel_button.pack(side="left", padx=(6, 0))
        self.verify_button = ttk.Button(actions, text="Verify last copy", command=self._verify,
                                        state="disabled", width=16)
        self.verify_button.pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Open destination", command=self._open_destination,
                   width=16).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Save log…", command=self._save_log,
                   width=12).pack(side="right")

        progress = ttk.LabelFrame(outer, text="Progress", padding=8)
        progress.pack(fill="x", pady=(10, 0))
        progress.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(progress, mode="determinate", maximum=100.0)
        self.bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress, textvariable=self.status_var, width=14,
                  anchor="e").grid(row=0, column=1, padx=(8, 0))
        ttk.Label(progress, textvariable=self.detail_var).grid(row=1, column=0, columnspan=2,
                                                               sticky="w", pady=(6, 0))
        ttk.Label(progress, textvariable=self.file_var, foreground="#555").grid(
            row=2, column=0, columnspan=2, sticky="w"
        )

        log_frame = ttk.LabelFrame(outer, text="Activity", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.log_widget = scrolledtext.ScrolledText(log_frame, height=10, wrap="none",
                                                    state="disabled", font=("Consolas", 9))
        self.log_widget.pack(fill="both", expand=True)
        self.log_widget.tag_configure("problem", foreground="#b00020")

        self._log(f"Fatcopy {__version__} ready.")
        self._log("Tip: for a very large folder, 'Full' checking is slower but proves every byte.")

    # ----------------------------------------------------------- pickers
    def _pick_source_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Folder to copy")
        if chosen:
            self.source_var.set(os.path.normpath(chosen))

    def _pick_source_file(self) -> None:
        chosen = filedialog.askopenfilename(title="File to copy")
        if chosen:
            self.source_var.set(os.path.normpath(chosen))

    def _pick_dest(self) -> None:
        chosen = filedialog.askdirectory(title="Copy into this folder")
        if chosen:
            self.dest_var.set(os.path.normpath(chosen))

    def _swap(self) -> None:
        source, destination = self.source_var.get(), self.dest_var.get()
        self.source_var.set(destination)
        self.dest_var.set(source)

    def _open_destination(self) -> None:
        path = self.dest_var.get().strip()
        if not path or not os.path.isdir(path):
            messagebox.showinfo("Fatcopy", "The destination folder does not exist yet.")
            return
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                import subprocess

                subprocess.Popen(["xdg-open", path])
        except OSError as exc:
            messagebox.showerror("Fatcopy", f"Could not open the folder:\n{exc}")

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save the activity log",
            defaultextension=".txt",
            initialfile="fatcopy-log.txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.log_widget.get("1.0", "end"))
        except OSError as exc:
            messagebox.showerror("Fatcopy", f"Could not save the log:\n{exc}")

    # ------------------------------------------------------------- options
    def _options(self) -> CopyOptions:
        return CopyOptions(
            threads=int(self.threads_var.get() or 0),
            buffer_bytes=_BUFFER_BYTES.get(self.buffer_var.get(), 4 * 1024 * 1024),
            pipeline=bool(self.pipeline_var.get()),
            verify=_VERIFY_CHOICES.get(self.verify_var.get(), "size"),
            overwrite=_OVERWRITE_CHOICES.get(self.overwrite_var.get(), "overwrite"),
            preserve=bool(self.preserve_var.get()),
            move=bool(self.move_var.get()),
            skip_hidden=bool(self.hidden_var.get()),
            exclude=tuple(part.strip() for part in self.exclude_var.get().split(",") if part.strip()),
            dry_run=bool(self.dry_var.get()),
        )

    def _settings_payload(self) -> dict:
        return {
            "source": self.source_var.get(),
            "dest": self.dest_var.get(),
            "move": bool(self.move_var.get()),
            "skip_hidden": bool(self.hidden_var.get()),
            "preserve": bool(self.preserve_var.get()),
            "pipeline": bool(self.pipeline_var.get()),
            "exclude": self.exclude_var.get(),
            "threads": int(self.threads_var.get() or 0),
            "buffer_label": self.buffer_var.get(),
            "verify": _VERIFY_CHOICES.get(self.verify_var.get(), "size"),
            "overwrite": _OVERWRITE_CHOICES.get(self.overwrite_var.get(), "overwrite"),
            "geometry": self.root.geometry(),
        }

    # --------------------------------------------------------------- copy
    def _start(self) -> None:
        if self.running:
            return
        source = self.source_var.get().strip().strip('"')
        destination = self.dest_var.get().strip().strip('"')
        if not source or not destination:
            messagebox.showwarning("Fatcopy", "Choose a source and a destination first.")
            return
        if not os.path.lexists(source):
            messagebox.showerror("Fatcopy", f"This source does not exist:\n{source}")
            return
        if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(destination)):
            messagebox.showerror("Fatcopy", "The source and the destination are the same folder.")
            return
        if self.move_var.get() and not messagebox.askyesno(
            "Move instead of copy?",
            "The source files will be deleted once each copy has been checked.\n\n"
            "Continue?",
        ):
            return

        options = self._options()
        try:
            plan = build_plan(source, destination, options)
        except CopyPlanError as exc:
            messagebox.showerror("Fatcopy", str(exc))
            return

        self.plan = plan
        self.stats = CopyStats()
        self.cancel = threading.Event()
        self.running = True
        self.started_at = time.monotonic()

        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.verify_button.configure(state="disabled")
        self.bar.configure(value=0.0)
        self.status_var.set("Starting…")
        self.file_var.set("")

        if plan.files_total == 0 and not plan.links:
            self._log(f"Nothing to copy — {len(plan.skipped)} item(s) were left alone.")
        else:
            self._log(
                f"Starting: {plan.files_total} file(s), {human_bytes(plan.bytes_total)}, "
                f"{plan.dirs_total} folder(s) — {options.threads} at a time, "
                f"check {options.verify}"
                + (" [preview only]" if options.dry_run else "")
            )
        for path, reason in plan.skipped[:20]:
            self._log(f"  left alone ({reason}): {path}")

        self.worker = threading.Thread(target=self._work, args=(source, destination, options),
                                       name="fatcopy-gui", daemon=True)
        self.worker.start()

    def _work(self, source: str, destination: str, options: CopyOptions) -> None:
        """Runs on the worker thread — talks to the UI only through the queue."""
        push = self.events.put
        try:
            result = run_copy(
                source,
                destination,
                options,
                cancel=self.cancel,
                stats=self.stats,
                on_file=lambda job, phase: push(
                    ("file", job.display_dst) if phase == "start" else ("filedone", job.display_dst)
                ),
                on_error=lambda message: push(("error", message)),
                on_log=lambda message: push(("log", message)),
            )
            push(("done", result))
        except CopyPlanError as exc:
            push(("fatal", str(exc)))
        except BaseException as exc:
            push(("fatal", f"{type(exc).__name__}: {exc}"))

    def _cancel(self) -> None:
        if not self.running:
            return
        self.cancel.set()
        self.cancel_button.configure(state="disabled")
        self.status_var.set("Stopping…")
        self._log("Cancelling — finishing the files already in flight.")

    def _verify(self) -> None:
        if self.running or self.plan is None:
            return
        if not messagebox.askyesno(
            "Verify the last copy?",
            f"Compare every byte of {self.plan.files_total} file(s) against the source.\n\n"
            "This reads both sides completely and can take a while.",
        ):
            return
        self.running = True
        self.cancel = threading.Event()
        self.stats = CopyStats()
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.verify_button.configure(state="disabled")
        self.bar.configure(value=0.0)
        self.status_var.set("Verifying…")
        self._log("Verifying the last copy, byte for byte.")

        def work():
            try:
                result = verify_plan(
                    self.plan,
                    self._options(),
                    cancel=self.cancel,
                    stats=self.stats,
                    on_error=lambda message: self.events.put(("error", message)),
                )
                self.events.put(("verified", result))
            except BaseException as exc:
                self.events.put(("fatal", f"{type(exc).__name__}: {exc}"))

        self.worker = threading.Thread(target=work, name="fatcopy-verify", daemon=True)
        self.worker.start()

    # -------------------------------------------------------------- pump
    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        if self.running:
            self._paint()
        self.root.after(150, self._pump)

    def _handle(self, kind: str, payload) -> None:
        if kind == "log":
            self._log(payload)
        elif kind == "error":
            self._log("! " + payload, problem=True)
        elif kind == "file":
            self.file_var.set("Copying " + self._shorten(payload))
        elif kind == "filedone":
            self.file_var.set("Copied " + self._shorten(payload))
        elif kind == "verified":
            self._finish(payload, verified=True)
        elif kind == "done":
            self._finish(payload, verified=False)
        elif kind == "fatal":
            self.running = False
            self._buttons_idle()
            self.status_var.set("Failed")
            self.detail_var.set(payload)
            self._log("! " + payload, problem=True)
            messagebox.showerror("Fatcopy", payload)

    def _shorten(self, path: str) -> str:
        if len(path) <= 90:
            return path
        return "…" + path[-89:]

    def _paint(self) -> None:
        snap = self.stats.snapshot()
        if snap["done"]:
            self.bar.configure(value=100.0)
        else:
            self.bar.configure(value=snap["percent"])
        if snap["files_total"]:
            self.status_var.set(f"{snap['percent']:.0f}%")
            detail = (
                f"{human_bytes(snap['bytes_done'])} of {human_bytes(snap['bytes_total'])}  ·  "
                f"{snap['files_done']} of {snap['files_total']} files  ·  "
                f"{human_bytes(snap['speed'])}/s"
            )
            if snap["eta"]:
                detail += f"  ·  about {human_time(snap['eta'])} left"
            self.detail_var.set(detail)
        else:
            self.status_var.set("Working…")
            self.detail_var.set(f"{human_bytes(snap['bytes_done'])} so far")

    def _finish(self, stats: CopyStats, *, verified: bool) -> None:
        self.running = False
        self._buttons_idle()
        summary = stats.summary()
        self.bar.configure(value=100.0 if not summary["cancelled"] else self.bar["value"])
        problems = summary["errors"]

        if summary["cancelled"]:
            self.status_var.set("Cancelled")
            detail = f"Stopped after {summary['files_copied']} file(s)."
        elif problems:
            self.status_var.set("Finished with problems")
            detail = f"{summary['files_copied']} file(s) copied, {problems} problem(s)."
        elif verified:
            self.status_var.set("Verified")
            detail = f"All {summary['files_copied']} file(s) match the source."
        else:
            self.status_var.set("Done")
            detail = (
                f"{summary['files_copied']} of {summary['files_planned']} file(s) copied in "
                f"{human_time(summary['elapsed_seconds'])} at "
                f"{human_bytes(summary['average_bytes_per_second'])}/s."
            )
        self.detail_var.set(detail)
        self._log(detail)

        if summary["partial_files"]:
            self._log("These files were still being written when the copy stopped:", problem=True)
            for path in summary["partial_files"][:20]:
                self._log("  " + self._shorten(path), problem=True)
        for line in summary["error_messages"]:
            self._log("! " + line, problem=True)

        self._save_settings()
        self.verify_button.configure(state="normal" if self.plan else "disabled")

        if not summary["cancelled"]:
            self.plan = None
            self.verify_button.configure(state="disabled")
            if problems:
                messagebox.showwarning(
                    "Fatcopy",
                    f"{summary['files_copied']} file(s) copied.\n\n"
                    f"{problems} problem(s) — see the Activity list.",
                )
            else:
                messagebox.showinfo("Fatcopy", detail)

    def _buttons_idle(self) -> None:
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")

    # ---------------------------------------------------------------- log
    def _log(self, message: str, problem: bool = False) -> None:
        text = str(message)[:_MAX_MESSAGE]
        stamp = time.strftime("%H:%M:%S")
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", f"{stamp}  {text}\n", "problem" if problem else "")
        self.log_lines += 1
        if self.log_lines > _LOG_LINES:
            self.log_widget.delete("1.0", f"{self.log_lines - _LOG_LINES}.0")
            self.log_lines = _LOG_LINES
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def _save_settings(self) -> None:
        payload = self._settings_payload()
        if not save_config(payload):
            self._log("Could not save the settings file (the copy itself is unaffected).",
                      problem=True)

    # -------------------------------------------------------------- close
    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno(
                "A copy is running",
                "Stop it and close the window?\n\n"
                "Files that are part-way through are reported in the Activity list.",
            ):
                return
            self.cancel.set()
            deadline = time.monotonic() + 20
            while self.running and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.05)
        self._save_settings()
        self.root.destroy()


def main(argv=None) -> int:
    """Open the window and block until it is closed."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        sys.stderr.write(
            f"fatcopy: cannot open a window here ({exc}).\n"
            "Use the command line form instead:  fatcopy <source> <destination>\n"
        )
        return 2
    app = FatcopyApp(root)

    if "--self-test" in (argv or sys.argv[1:]):
        root.after(400, root.destroy)

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
