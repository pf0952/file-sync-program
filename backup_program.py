"""
Backup Program

Copies a network drive to a local drive, skipping files that haven't changed.

Build:
    pip install pyinstaller
    pyinstaller --onefile --windowed --name BackupProgram backup_program.py
"""

import json
import os
import queue
import shutil
import stat
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Backup Program"
VERSION = "1.0"

DEFAULT_SOURCE = "Z:\\"

CHUNK = 1024 * 1024
TIME_SLOP = 2.0         # FAT timestamps are only accurate to 2 seconds
ERROR_LIMIT = 25
RETRIES = 2


def winpath(path):
    r"""Add the \\?\ prefix so paths longer than 260 characters still work."""
    if os.name != "nt":
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC" + p[1:]
    return "\\\\?\\" + p


def nice_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def settings_file():
    if getattr(sys, "frozen", False):
        here = os.path.dirname(sys.executable)
    else:
        here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "backup_settings.json")


class BackupWorker(threading.Thread):
    """Does the actual copying. Talks to the window through a queue."""

    def __init__(self, source, dest, out_queue, cancel_event):
        super().__init__(daemon=True)
        self.source = os.path.abspath(source)
        self.dest = os.path.abspath(dest)
        self.q = out_queue
        self.cancel = cancel_event

        self.files_copied = 0
        self.files_skipped = 0
        self.bytes_copied = 0
        self.errors = []
        self.error_run = 0

    def log(self, text):
        self.q.put(("log", text))

    def status(self, text):
        self.q.put(("status", text))

    def error(self, text):
        self.errors.append(text)
        self.log("  ERROR: " + text)

    def source_ok(self):
        try:
            return os.path.isdir(winpath(self.source))
        except OSError:
            return False

    def source_gone(self):
        if self.source_ok():
            return False
        self.error(f"Lost connection to {self.source}. The drive may have been disconnected.")
        return True

    def out_of_date(self, src_path, dst_path, src_stat):
        try:
            dst_stat = os.stat(winpath(dst_path))
        except FileNotFoundError:
            return True
        except OSError as exc:
            self.error(f"Could not check {dst_path}: {exc}")
            return True

        if src_stat.st_size != dst_stat.st_size:
            return True
        return src_stat.st_mtime - dst_stat.st_mtime > TIME_SLOP

    def scan(self):
        """Work out what needs copying. Returns (jobs, total_bytes) or None if stopped."""
        jobs = []
        total = 0
        checked = 0
        tick = 0.0

        self.q.put(("phase", "scan"))
        self.log(f"Scanning {self.source}")

        for root, dirs, files in os.walk(self.source, onerror=self.walk_problem):
            if self.cancel.is_set():
                return None

            rel = os.path.relpath(root, self.source)
            dst_root = self.dest if rel == "." else os.path.join(self.dest, rel)

            for name in files:
                if self.cancel.is_set():
                    return None

                src_path = os.path.join(root, name)
                dst_path = os.path.join(dst_root, name)
                checked += 1

                try:
                    info = os.stat(winpath(src_path))
                except OSError as exc:
                    self.error(f"Could not read {src_path}: {exc}")
                    if self.source_gone():
                        return None
                    continue

                if stat.S_ISDIR(info.st_mode):
                    continue

                if self.out_of_date(src_path, dst_path, info):
                    jobs.append((src_path, dst_path, info.st_size))
                    total += info.st_size
                else:
                    self.files_skipped += 1

                now = time.monotonic()
                if now - tick > 0.25:
                    tick = now
                    self.status(f"Scanning: {checked:,} checked, "
                                f"{len(jobs):,} to copy ({nice_size(total)})")

        self.log(f"Found {checked:,} files. {len(jobs):,} need copying "
                 f"({nice_size(total)}), {self.files_skipped:,} already current.")
        return jobs, total

    def walk_problem(self, exc):
        self.error(f"Could not open {getattr(exc, 'filename', '?')}: {exc}")

    def copy_one(self, src_path, dst_path, count_bytes):
        # Write to a temp name and rename at the end, so a dropped connection
        # can't leave a half-written file that looks complete next time.
        parent = os.path.dirname(dst_path)
        if parent:
            os.makedirs(winpath(parent), exist_ok=True)
        temp = dst_path + ".part"

        with open(winpath(src_path), "rb") as fin, open(winpath(temp), "wb") as fout:
            while True:
                if self.cancel.is_set():
                    raise InterruptedError
                block = fin.read(CHUNK)
                if not block:
                    break
                fout.write(block)
                count_bytes(len(block))

        shutil.copystat(winpath(src_path), winpath(temp))
        os.replace(winpath(temp), winpath(dst_path))

    def drop_temp(self, dst_path):
        try:
            os.remove(winpath(dst_path + ".part"))
        except OSError:
            pass

    def copy_files(self, jobs, total):
        self.q.put(("phase", "copy"))
        done = 0
        tick = 0.0
        last_sent = [0.0]
        start = time.monotonic()

        for n, (src_path, dst_path, size) in enumerate(jobs, 1):
            if self.cancel.is_set():
                return

            before = done

            def count_bytes(amount):
                nonlocal done
                done += amount
                now = time.monotonic()
                if now - last_sent[0] > 0.1:
                    last_sent[0] = now
                    self.q.put(("progress", (done, total)))

            copied = False
            for attempt in range(RETRIES + 1):
                try:
                    self.copy_one(src_path, dst_path, count_bytes)
                    copied = True
                    break
                except InterruptedError:
                    self.drop_temp(dst_path)
                    return
                except OSError as exc:
                    self.drop_temp(dst_path)
                    done = before
                    if self.source_gone():
                        return
                    if attempt < RETRIES:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    self.error(f"Could not copy {src_path}: {exc}")
                    self.error_run += 1
                    if self.error_run >= ERROR_LIMIT:
                        self.error(f"Giving up after {ERROR_LIMIT} failures in a row.")
                        return

            if copied:
                self.error_run = 0
                self.files_copied += 1
                self.bytes_copied += size
                done = before + size
                self.log(f"  {os.path.relpath(src_path, self.source)} ({nice_size(size)})")

            now = time.monotonic()
            if now - tick > 0.2:
                tick = now
                rate = done / max(now - start, 0.001)
                self.q.put(("progress", (done, total)))
                self.status(f"Copying {n:,} of {len(jobs):,}, "
                            f"{nice_size(done)} of {nice_size(total)} "
                            f"({nice_size(rate)}/s)")

        self.q.put(("progress", (total, total)))

    def run(self):
        start = time.monotonic()
        stopped = False
        try:
            if not self.source_ok():
                self.error(f"Can't reach {self.source}. If it's a network drive, "
                           "check that you're connected to it.")
                raise SystemExit

            try:
                os.makedirs(winpath(self.dest), exist_ok=True)
            except OSError as exc:
                self.error(f"Can't create {self.dest}: {exc}")
                raise SystemExit

            if not os.path.isdir(winpath(self.dest)):
                self.error(f"Can't reach {self.dest}.")
                raise SystemExit

            found = self.scan()
            if found is None:
                stopped = self.cancel.is_set()
                raise SystemExit
            jobs, total = found

            if total:
                try:
                    free = shutil.disk_usage(self.dest).free
                    if free < total:
                        self.error(f"Only {nice_size(free)} free but {nice_size(total)} "
                                   "needed. Copying until the drive fills up.")
                except OSError:
                    pass

            if not jobs:
                self.log("Everything is already up to date.")
            else:
                self.copy_files(jobs, total)
                stopped = self.cancel.is_set()

        except SystemExit:
            pass
        except Exception as exc:
            self.error(f"Unexpected problem: {exc!r}")
        finally:
            self.q.put(("done", {
                "copied": self.files_copied,
                "skipped": self.files_skipped,
                "bytes": self.bytes_copied,
                "errors": list(self.errors),
                "stopped": stopped or self.cancel.is_set(),
                "seconds": time.monotonic() - start,
            }))


class Window(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {VERSION}")
        self.geometry("760x520")
        self.minsize(620, 440)

        self.q = queue.Queue()
        self.worker = None
        self.cancel = threading.Event()

        self.source = tk.StringVar(value=DEFAULT_SOURCE)
        self.dest = tk.StringVar(value="")
        self.status = tk.StringVar(value="Ready")

        self.build()
        self.load_settings()
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.after(100, self.check_queue)

    def build(self):
        paths = ttk.Frame(self)
        paths.pack(fill="x", padx=8, pady=6)
        paths.columnconfigure(1, weight=1)

        ttk.Label(paths, text="Copy from:").grid(row=0, column=0, sticky="w")
        self.source_box = ttk.Entry(paths, textvariable=self.source)
        self.source_box.grid(row=0, column=1, sticky="ew", padx=6)
        self.source_btn = ttk.Button(paths, text="Browse", width=10, command=self.pick_source)
        self.source_btn.grid(row=0, column=2)

        ttk.Label(paths, text="Copy to:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.dest_box = ttk.Entry(paths, textvariable=self.dest)
        self.dest_box.grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        self.dest_btn = ttk.Button(paths, text="Browse", width=10, command=self.pick_dest)
        self.dest_btn.grid(row=1, column=2, pady=(6, 0))

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=8, pady=2)
        self.start_btn = ttk.Button(buttons, text="Start Backup", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(buttons, text="Clear", command=self.clear_log).pack(side="left")
        ttk.Button(buttons, text="Save Log", command=self.save_log).pack(side="left", padx=6)

        self.bar = ttk.Progressbar(self, mode="determinate", maximum=1000)
        self.bar.pack(fill="x", padx=8, pady=6)

        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=8)

        box = ttk.LabelFrame(self, text="Log")
        box.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log_box = tk.Text(box, wrap="none", height=14, state="disabled",
                               font=("Consolas", 9))
        vbar = ttk.Scrollbar(box, orient="vertical", command=self.log_box.yview)
        hbar = ttk.Scrollbar(box, orient="horizontal", command=self.log_box.xview)
        self.log_box.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.log_box.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)

    def load_settings(self):
        try:
            with open(settings_file(), "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            self.source.set(saved.get("source", DEFAULT_SOURCE))
            self.dest.set(saved.get("dest", ""))
        except (OSError, ValueError):
            pass

    def save_settings(self):
        try:
            with open(settings_file(), "w", encoding="utf-8") as fh:
                json.dump({"source": self.source.get(), "dest": self.dest.get()}, fh, indent=2)
        except OSError:
            pass

    def write_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def save_log(self):
        name = filedialog.asksaveasfilename(
            title="Save log", defaultextension=".txt",
            initialfile=time.strftime("backup-%Y%m%d-%H%M%S.txt"),
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not name:
            return
        try:
            with open(name, "w", encoding="utf-8") as fh:
                fh.write(self.log_box.get("1.0", "end"))
            self.status.set("Log saved to " + name)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Couldn't save the log:\n{exc}")

    def pick_source(self):
        start = self.source.get() if os.path.isdir(self.source.get()) else ""
        chosen = filedialog.askdirectory(title="Folder to copy from", initialdir=start)
        if chosen:
            self.source.set(os.path.normpath(chosen))

    def pick_dest(self):
        start = self.dest.get() if os.path.isdir(self.dest.get()) else ""
        chosen = filedialog.askdirectory(title="Folder to copy to", initialdir=start)
        if chosen:
            self.dest.set(os.path.normpath(chosen))

    def start(self):
        source = self.source.get().strip()
        dest = self.dest.get().strip()

        if not source or not dest:
            messagebox.showwarning(APP_NAME, "Pick a folder to copy from and a folder to copy to.")
            return

        if not os.path.isdir(winpath(source)):
            messagebox.showerror(APP_NAME, f"Can't reach this folder:\n\n{source}\n\n"
                                           "If it's a network drive, check that you're connected.")
            return

        a = os.path.normcase(os.path.abspath(source)).rstrip("\\/")
        b = os.path.normcase(os.path.abspath(dest)).rstrip("\\/")
        if b == a or b.startswith(a + os.sep):
            messagebox.showerror(APP_NAME, "The destination can't be the source folder "
                                           "or a folder inside it.")
            return

        self.save_settings()
        self.running(True)
        self.bar.configure(mode="determinate", value=0)
        self.write_log("=" * 70)
        self.write_log(time.strftime("Started %d %b %Y at %H:%M:%S"))
        self.write_log(f"  From: {source}")
        self.write_log(f"    To: {dest}")

        self.cancel = threading.Event()
        self.worker = BackupWorker(source, dest, self.q, self.cancel)
        self.worker.start()

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.cancel.set()
            self.status.set("Stopping")
            self.stop_btn.configure(state="disabled")

    def running(self, busy):
        state = "disabled" if busy else "normal"
        for widget in (self.start_btn, self.source_box, self.dest_box,
                       self.source_btn, self.dest_btn):
            widget.configure(state=state)
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def check_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()

                if kind == "log":
                    self.write_log(payload)
                elif kind == "status":
                    self.status.set(payload)
                elif kind == "phase":
                    if payload == "scan":
                        self.bar.configure(mode="indeterminate")
                        self.bar.start(12)
                    else:
                        self.bar.stop()
                        self.bar.configure(mode="determinate", value=0)
                elif kind == "progress":
                    done, total = payload
                    share = (done / total) if total else 1.0
                    self.bar.configure(value=max(0, min(1000, int(share * 1000))))
                elif kind == "done":
                    self.finished(payload)
        except queue.Empty:
            pass
        self.after(100, self.check_queue)

    def finished(self, result):
        self.bar.stop()
        self.bar.configure(mode="determinate")
        if not result["stopped"] and not result["errors"]:
            self.bar.configure(value=1000)

        minutes, seconds = divmod(int(result["seconds"]), 60)
        if result["stopped"]:
            headline = "Stopped"
        elif result["errors"]:
            headline = "Finished with problems"
        else:
            headline = "Finished"

        self.write_log(f"{headline} after {minutes}m {seconds}s. "
                       f"Copied {result['copied']:,} ({nice_size(result['bytes'])}), "
                       f"skipped {result['skipped']:,}, "
                       f"{len(result['errors']):,} problem(s).")
        self.write_log("")
        self.status.set(f"{headline}. Copied {result['copied']:,}, "
                        f"skipped {result['skipped']:,}, "
                        f"{len(result['errors']):,} problem(s).")

        self.running(False)
        self.worker = None

        if result["errors"]:
            shown = "\n".join("- " + e for e in result["errors"][:5])
            if len(result["errors"]) > 5:
                shown += f"\n\nand {len(result['errors']) - 5} more, see the log."
            messagebox.showwarning(APP_NAME, headline + ".\n\n" + shown)
        elif not result["stopped"]:
            messagebox.showinfo(APP_NAME,
                                f"Copied {result['copied']:,} file(s), "
                                f"{nice_size(result['bytes'])}.\n"
                                f"{result['skipped']:,} file(s) were already up to date.")

    def quit_app(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "A backup is running. Stop it and close?"):
                return
            self.cancel.set()
            self.worker.join(timeout=5)
        self.save_settings()
        self.destroy()


if __name__ == "__main__":
    Window().mainloop()
