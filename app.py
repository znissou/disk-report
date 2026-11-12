"""Disk Report as a Windows app.

It opens on the latest scan of your drive, scans again when you ask, and lets you act on what it found from
the same place: show a folder in File Explorer, delete it, uninstall an app, run a cleaning command. Every
action is for one thing, starts only when you click it and confirm, and is written to a log.

    py app.py                 start the app (it needs pywebview: py -m pip install pywebview)

Scans, settings and the log are kept in %LOCALAPPDATA%\\Disk Report (or in DISK_REPORT_DATA when it's set).
"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import shutil
import sys
import threading
import time
import traceback
import uuid
import winreg

import actions
import catalog as K
import disk_report as engine

APP_NAME = "Disk Report"
DATA_DIR = os.environ.get("DISK_REPORT_DATA") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME)
SCANS_DIR = os.path.join(DATA_DIR, "scans")
TEMP_DIR = os.path.join(DATA_DIR, "temp")
LOG_FILE = os.path.join(DATA_DIR, "actions.jsonl")
ERROR_FILE = os.path.join(DATA_DIR, "errors.log")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
ICON = os.path.join(getattr(sys, "_MEIPASS", engine.HERE), "assets", "icon.ico")
KEEP_SCANS = 10      # per drive
# Held while the app runs. installer.iss names it too (AppMutex), so Setup and the uninstaller wait until the
# app is closed instead of replacing or removing its files under it.
MUTEX = "DiskReportApp"


# ------------------------------------------------------------------------------------------------ files

def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".part"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(temp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def log_error(where):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(ERROR_FILE, "a", encoding="utf-8") as f:
            f.write("%s %s\n%s\n" % (dt.datetime.now().isoformat(timespec="seconds"), where, traceback.format_exc()))
    except OSError:
        pass


def settings():
    return read_json(SETTINGS_FILE, {}) or {}


def save_setting(**values):
    current = settings()
    current.update(values)
    write_json(SETTINGS_FILE, current)


def scan_files(drive):
    """This drive's saved scans, newest first ('C-2026-11-12_142830.json'...)."""
    try:
        names = os.listdir(SCANS_DIR)
    except OSError:
        return []
    prefix = drive.rstrip(":").upper() + "-"
    return sorted((n for n in names if n.startswith(prefix) and n.endswith(".json")), reverse=True)


def save_scan(drive, data):
    name = "%s-%s.json" % (drive.rstrip(":").upper(), dt.datetime.now().strftime("%Y-%m-%d_%H%M%S"))
    write_json(os.path.join(SCANS_DIR, name), data)
    for old in scan_files(drive)[KEEP_SCANS:]:
        try:
            os.remove(os.path.join(SCANS_DIR, old))
        except OSError:
            pass
    return name


def log_action(record):
    record = dict(record, time=time.time())
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        log_error("writing the action log")


def actions_for(scan_id):
    """What was done since a scan, so the report can show it again after a restart."""
    out = []
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("scan") == scan_id:
                    out.append(record)
    except OSError:
        pass
    return out


# ------------------------------------------------------------------------------------------------ Windows facts

def fixed_drives():
    out = []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if not mask & (1 << i):
            continue
        drive = chr(65 + i) + ":"
        if ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") != 3:   # fixed drives only
            continue
        try:
            u = shutil.disk_usage(drive + "\\")
        except OSError:
            continue
        out.append({"drive": drive, "total": u.total, "used": u.used, "free": u.free})
    return out


def usage(drive):
    try:
        u = shutil.disk_usage(drive + "\\")
        return {"total": u.total, "used": u.used, "free": u.free}
    except OSError:
        return None


def windows_dark():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def already_open():
    """Take the app's mutex for as long as this process lives. When another Disk Report window already has
    it, bring that window back instead and answer True."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    already_open.handle = kernel32.CreateMutexW(None, False, MUTEX)   # kept: closing it would release it
    if ctypes.get_last_error() != 183:                                   # ERROR_ALREADY_EXISTS
        return False
    hwnd = ctypes.windll.user32.FindWindowW(None, APP_NAME)
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 9)                         # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    return True


def scan_error(error):
    """What to tell the person when a scan fails."""
    own = getattr(sys, "_MEIPASS", None)
    missing = getattr(error, "filename", None)
    if own and (isinstance(error, ImportError) or (isinstance(error, FileNotFoundError) and missing and
                                                   os.path.normcase(str(missing)).startswith(os.path.normcase(own)))):
        return "Some of Disk Report's own files are missing, so it can't scan. Reinstall Disk Report to fix this."
    return "The scan stopped: %s" % error


def own_folders():
    """Folders the app never deletes: its own."""
    here = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else engine.HERE
    return [DATA_DIR, here]


def clean_paths(paths):
    """Paths from the page, written the way Windows writes them (backslashes, no '.' parts)."""
    return [os.path.normpath(p) for p in paths if isinstance(p, str) and p]


def parts_to_recycle(path, keep, contents_only):
    """The largest pieces of a folder that hold nothing to keep: the folder itself when that's possible."""
    keep = {actions.norm(k) for k in keep}

    def holds(p):
        key = actions.norm(p)
        return any(k == key or k.startswith(key + "\\") for k in keep)

    if not contents_only and not holds(path):
        return [path]
    out, stack = [], [path]
    while stack:
        folder = stack.pop()
        try:
            names = os.listdir(actions.LONG + folder)
        except OSError:
            continue
        for name in names:
            p = folder + "\\" + name
            if actions.norm(p) in keep:
                continue
            (stack if holds(p) else out).append(p)
    return out


# ------------------------------------------------------------------------------------------------ the app's API

class Loaded:
    """A saved scan, with the guard that decides what may be deleted from it."""

    def __init__(self, scan_id, data):
        self.id = scan_id
        self.data = data
        self.tokens = engine.path_tokens(data["meta"]["drive"])
        self.guard = actions.Guard(data, self.tokens, own_folders())


class Tasks:
    """Work that takes a while (deleting, waiting for an uninstaller), polled by the page."""

    def __init__(self):
        self._tasks = {}

    def start(self, work):
        task_id = uuid.uuid4().hex[:12]
        task = {"done": False, "result": None, "progress": None, "stop": False}
        self._tasks[task_id] = task

        def run():
            try:
                task["result"] = work(task)
            except Exception as e:   # reported to the page; details go to errors.log
                log_error("task")
                task["result"] = {"ok": False, "error": "Something went wrong: %s" % e}
            finally:
                task["done"] = True

        threading.Thread(target=run, daemon=True).start()
        return task_id

    def view(self, task_id):
        task = self._tasks.get(task_id)
        if task is None:
            return {"done": True, "result": {"ok": False, "error": "This task is unknown."}}
        return {"done": task["done"], "result": task["result"] if task["done"] else None,
                "progress": task["progress"]}

    def stop(self, task_id):
        task = self._tasks.get(task_id)
        if task is not None:
            task["stop"] = True


class Api:
    """What the page can ask for. Each method answers with plain data (pywebview turns it into JSON)."""

    def __init__(self):
        self._window = None
        self._lock = threading.Lock()
        self._loaded = {}
        self._tasks = Tasks()
        self._scan = {"running": False}
        self._cancel = None

    # -- state
    def state(self, drive=None):
        drives = fixed_drives()
        letters = [d["drive"] for d in drives]
        drive = engine.normalize_drive(drive or settings().get("drive") or os.environ.get("SystemDrive", "C:"))
        if drive not in letters and letters:
            drive = letters[0]
        if settings().get("drive") != drive:
            save_setting(drive=drive)
        loaded = self._get(drive)
        return {
            "version": engine.VERSION, "drives": drives, "drive": drive, "usage": usage(drive),
            "scan": loaded.data if loaded else None, "scanId": loaded.id if loaded else None,
            "done": actions_for(loaded.id) if loaded else [],
            "protected": sorted(loaded.guard.never) if loaded else [],
            "commands": K.COMMANDS, "admin": engine.is_admin(), "theme": settings().get("theme"),
            "scanning": self.scan_status(),
        }

    def usage(self, drive):
        return usage(engine.normalize_drive(drive))

    def set_theme(self, theme):
        save_setting(theme=theme if theme in ("light", "dark") else None)
        return True

    def _get(self, drive):
        files = scan_files(drive)
        if not files:
            return None
        loaded = self._loaded.get(drive)
        if loaded is None or loaded.id != files[0]:
            data = read_json(os.path.join(SCANS_DIR, files[0]))
            if not data or "meta" not in data:
                return None
            loaded = self._loaded[drive] = Loaded(files[0], data)
        return loaded

    def _hwnd(self):
        """The window's handle, so that Windows' own dialogs (progress, administrator prompt) open on top of it."""
        try:
            return int(self._window.native.Handle.ToInt64())
        except Exception:   # read from another thread or not shown yet: find it by its title instead
            return ctypes.windll.user32.FindWindowW(None, APP_NAME) or None

    # -- scanning
    def scan(self, drive):
        drive = engine.normalize_drive(drive)
        with self._lock:
            if self._scan.get("running"):
                return {"ok": False, "error": "A scan is already running."}
            u = usage(drive)
            if u is None:
                return {"ok": False, "error": "Drive %s isn't available." % drive}
            self._cancel = threading.Event()
            # the last scan's number of files and folders makes a steadier progress bar than bytes
            last = self._get(drive)
            expected = (last.data["meta"].get("files", 0) + last.data["meta"].get("folders", 0)) if last else 0
            self._scan = {"running": True, "drive": drive, "stage": "apps", "text": "Starting...", "files": 0,
                          "folders": 0, "counted": 0, "used": u["used"], "expected": expected,
                          "started": time.time()}
            cancel = self._cancel
        threading.Thread(target=self._scan_worker, args=(drive, cancel), daemon=True).start()
        return {"ok": True}

    def _scan_worker(self, drive, cancel):
        def progress(stage, text, scanner):
            with self._lock:
                self._scan["stage"] = stage
                if text:
                    self._scan["text"] = text
                if scanner is not None:
                    self._scan.update(files=scanner.files, folders=scanner.dirs, counted=scanner.counted)

        try:
            data = engine.run(drive, progress=progress, cancel=cancel)
            result = {"finished": True, "scanId": save_scan(drive, data)}
        except engine.Cancelled:
            result = {"cancelled": True}
        except Exception as e:
            log_error("scan")
            result = {"error": scan_error(e)}
        with self._lock:
            self._scan.update(result, running=False, ended=time.time())

    def scan_status(self):
        with self._lock:
            out = dict(self._scan)
        if "started" in out:
            out["seconds"] = round((out.get("ended") or time.time()) - out["started"])
        return out

    def cancel_scan(self):
        if self._cancel is not None:
            self._cancel.set()
        return True

    # -- looking
    def reveal(self, path):
        return actions.reveal(os.path.normpath(path))

    def copy(self, text):
        return actions.copy_text(str(text))

    def open_settings(self, key):
        if key not in actions.SETTINGS:
            return {"ok": False, "error": "Unknown settings page."}
        try:
            actions.open_settings(key, self._hwnd())
        except OSError as e:
            if getattr(e, "winerror", None) == actions.CANCELLED_BY_USER:
                return {"ok": False, "cancelled": True}
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    # -- deleting
    def check(self, drive, paths, contents=False):
        """Before the confirmation: may these be deleted, and what inside them stays?"""
        loaded = self._get(engine.normalize_drive(drive))
        if loaded is None:
            return {"ok": False, "reason": "Scan the drive first."}
        paths = clean_paths(paths)
        keep, names, missing = [], [], []
        for p in paths:
            verdict = loaded.guard.check(p, bool(contents))
            if not verdict["ok"]:
                return {"ok": False, "reason": verdict["reason"], "path": p}
            keep += verdict["keep"]
            names += [n for n in verdict["keepNames"] if n not in names]
            if not os.path.lexists(actions.LONG + p):
                missing.append(p)
        sync = sorted({prov for p in paths for root, prov in loaded.data["meta"].get("syncRoots") or []
                       if actions.inside(actions.norm(p), actions.norm(root))})
        return {"ok": True, "keep": len(keep), "keepNames": names[:6], "missing": missing, "sync": sync}

    def delete(self, drive, paths, mode, contents=False):
        """Delete what the person confirmed: mode 'trash' (Recycle Bin) or 'forever'. Returns a task to poll."""
        loaded = self._get(engine.normalize_drive(drive))
        if loaded is None:
            return {"ok": False, "error": "Scan the drive first."}
        paths = clean_paths(paths)
        contents = bool(contents)
        verdicts = []
        for p in paths:
            verdict = loaded.guard.check(p, contents)
            if not verdict["ok"]:
                return {"ok": False, "error": verdict["reason"]}
            verdicts.append((p, verdict["keep"]))
        cluster = loaded.data["meta"].get("cluster") or 4096

        def work(task):
            total = {"freed": 0, "files": 0, "inUse": 0, "denied": 0, "kept": 0, "errors": []}
            states, cancelled = [], False
            for p, keep in verdicts:
                if mode == "trash":
                    result = actions.recycle(parts_to_recycle(p, keep, contents), self._hwnd())
                    cancelled = cancelled or bool(result.get("cancelled"))
                    if result.get("error"):
                        total["errors"].append(result["error"])
                else:
                    def progress(counts, done=dict(total)):
                        task["progress"] = {"files": done["files"] + counts["files"],
                                            "freed": done["freed"] + counts["freed"]}

                    result = actions.delete_for_good(p, contents, keep, cluster, progress)
                    for k in ("freed", "files", "inUse", "denied", "kept"):
                        total[k] += result[k]
                    total["errors"] += result["errors"]
                states.append({"path": p, "state": self._state_of(p, mode, contents, result)})
            record = dict(total, kind="delete", mode=mode, contents=contents, paths=states, cancelled=cancelled,
                          drive=loaded.data["meta"]["drive"], scan=loaded.id)
            log_action(record)
            return dict(record, ok=True, usage=usage(loaded.data["meta"]["drive"]))

        return {"ok": True, "task": self._tasks.start(work)}

    @staticmethod
    def _state_of(path, mode, contents, result):
        exists = os.path.lexists(actions.LONG + path)
        if mode == "trash":
            if result.get("cancelled") or result.get("error"):
                return "failed" if exists and not contents else "partial"
            return "trashed" if not exists or contents else "partial"
        if contents:
            return "emptied" if not (result["inUse"] or result["denied"] or result["errors"]) else "partial"
        if not exists:
            return "deleted"
        return "partial" if result["files"] or result["kept"] else "failed"

    def delete_as_admin(self, drive, paths, contents=False):
        """The same as delete(..., 'forever'), in a helper Windows starts with administrator rights."""
        loaded = self._get(engine.normalize_drive(drive))
        if loaded is None:
            return {"ok": False, "error": "Scan the drive first."}
        paths = clean_paths(paths)
        for p in paths:
            verdict = loaded.guard.check(p, bool(contents))
            if not verdict["ok"]:
                return {"ok": False, "error": verdict["reason"]}

        def work(task):
            os.makedirs(TEMP_DIR, exist_ok=True)
            name = uuid.uuid4().hex
            task_file = os.path.join(TEMP_DIR, name + ".json")
            result_file = os.path.join(TEMP_DIR, name + ".result.json")
            write_json(task_file, {"scan": os.path.join(SCANS_DIR, loaded.id), "tokens": loaded.tokens,
                                   "own": own_folders(), "paths": list(paths), "contents": bool(contents),
                                   "cluster": loaded.data["meta"].get("cluster") or 4096, "result": result_file})
            if getattr(sys, "frozen", False):
                program, params = sys.executable, '--elevated "%s"' % task_file
            else:
                program, params = sys.executable, '"%s" --elevated "%s"' % (os.path.abspath(__file__), task_file)
            try:
                handle = actions.start(program, params, admin=True, hwnd=self._hwnd())
            except OSError as e:
                if getattr(e, "winerror", None) == actions.CANCELLED_BY_USER:
                    return {"ok": False, "cancelled": True}
                raise
            actions.wait(handle)
            actions.close(handle)
            results = read_json(result_file, []) or []
            for f in (task_file, result_file):
                try:
                    os.remove(f)
                except OSError:
                    pass
            total = {"freed": 0, "files": 0, "inUse": 0, "denied": 0, "kept": 0, "errors": []}
            states = []
            for r in results:
                if r.get("refused"):
                    total["errors"].append(r["refused"])
                    states.append({"path": r["path"], "state": "failed"})
                    continue
                for k in ("freed", "files", "inUse", "denied", "kept"):
                    total[k] += r.get(k, 0)
                total["errors"] += r.get("errors", [])
                states.append({"path": r["path"], "state": self._state_of(r["path"], "forever", contents, r)})
            if not results:
                total["errors"].append("The administrator helper didn't report back.")
            record = dict(total, kind="delete", mode="forever", admin=True, contents=bool(contents), paths=states,
                          drive=loaded.data["meta"]["drive"], scan=loaded.id)
            log_action(record)
            return dict(record, ok=True, usage=usage(loaded.data["meta"]["drive"]))

        return {"ok": True, "task": self._tasks.start(work)}

    def task(self, task_id):
        return self._tasks.view(task_id)

    def stop_task(self, task_id):
        self._tasks.stop(task_id)
        return True

    # -- Recycle Bin
    def bin_info(self, drive):
        return actions.recycle_bin(engine.normalize_drive(drive))

    def empty_bin(self, drive):
        drive = engine.normalize_drive(drive)
        loaded = self._get(drive)

        def work(task):
            result = actions.empty_recycle_bin(drive, self._hwnd())
            log_action(dict(result, kind="empty-bin", drive=drive, scan=loaded.id if loaded else None))
            return dict(result, usage=usage(drive))

        return {"ok": True, "task": self._tasks.start(work)}

    # -- uninstalling
    def uninstall(self, drive, group, index):
        loaded = self._get(engine.normalize_drive(drive))
        entries = ((loaded.data.get("apps") or {}).get(group) or []) if loaded else []
        if not isinstance(index, int) or not 0 <= index < len(entries):
            return {"ok": False, "error": "This app isn't in the report."}
        entry = entries[index]

        def work(task):
            state, error = self._uninstall(entry, task)
            loaded.guard._installed.pop(group, None)
            log_action({"kind": "uninstall", "group": group, "name": entry["name"], "state": state, "error": error,
                        "drive": loaded.data["meta"]["drive"], "scan": loaded.id})
            return {"ok": state == "removed", "state": state, "error": error, "group": group, "name": entry["name"],
                    "usage": usage(loaded.data["meta"]["drive"])}

        return {"ok": True, "task": self._tasks.start(work)}

    def _uninstall(self, entry, task):
        if entry["kind"] == "store":
            result = actions.remove_store_app(entry["package"])
            return ("removed", None) if result["ok"] else ("error", result["error"])
        try:
            program, params = actions.uninstall_command(entry["ref"])
        except (OSError, LookupError) as e:
            return "error", str(e)
        try:
            handle = actions.start(program, params, hwnd=self._hwnd())
        except OSError as e:
            if getattr(e, "winerror", None) == actions.CANCELLED_BY_USER:
                return "cancelled", None
            return "error", "Windows couldn't start the uninstaller: %s" % (e.strerror or e)
        started = time.time()
        try:
            # Some uninstallers hand over to a copy of themselves and exit at once, so what counts is whether
            # Windows still lists the app, not whether the first process is running.
            while True:
                if not actions.is_registered(entry["ref"]):
                    return "removed", None
                if task["stop"]:
                    return "stopped", None
                if time.time() - started > 3600:
                    return "stopped", None
                running = actions.wait(handle, 1.0) is None
                task["progress"] = {"running": running}
                if not running:
                    time.sleep(1.0)
        finally:
            actions.close(handle)

    # -- commands
    def run_command(self, command):
        if command not in K.COMMANDS:
            return {"ok": False, "error": "Disk Report only runs the cleaning commands from its catalog."}
        try:
            actions.run_command(command, K.COMMANDS[command], self._hwnd())
        except OSError as e:
            if getattr(e, "winerror", None) == actions.CANCELLED_BY_USER:
                return {"ok": False, "cancelled": True}
            return {"ok": False, "error": str(e)}
        log_action({"kind": "command", "command": command, "scan": None})
        return {"ok": True}


# ------------------------------------------------------------------------------------------------ elevated helper

def elevated(task_file):
    """Runs with administrator rights: delete what the app asked for, after the same checks, and report back."""
    task = read_json(task_file)
    results = []
    try:
        scan = read_json(task["scan"])
        guard = actions.Guard(scan, task["tokens"], task["own"])
        for path in task["paths"]:
            verdict = guard.check(path, task["contents"])
            if not verdict["ok"]:
                results.append({"path": path, "refused": verdict["reason"]})
                continue
            result = actions.delete_for_good(path, task["contents"], verdict["keep"], task["cluster"])
            results.append(dict(result, path=path))
    finally:
        write_json(task["result"], results)
    return 0


# ------------------------------------------------------------------------------------------------ window

def page():
    with open(engine.TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    return html.replace("__REPORT_TITLE__", APP_NAME).replace("__REPORT_DATA__", "null")


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--elevated":
        return elevated(sys.argv[2])
    if already_open():
        return 0
    try:
        import webview
    except ImportError:
        print("The app needs pywebview: py -m pip install pywebview")
        return 1
    os.makedirs(SCANS_DIR, exist_ok=True)
    try:   # its own taskbar button and icon, not Python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DiskReport.App")
    except (AttributeError, OSError):
        pass
    api = Api()
    window = webview.create_window(APP_NAME, html=page(), js_api=api, width=1240, height=900, min_size=(760, 560),
                                   text_select=True, background_color="#0d0d0d" if windows_dark() else "#f9f9f7")
    api._window = window
    webview.start(private_mode=False, storage_path=os.path.join(DATA_DIR, "webview"),
                  icon=ICON if os.path.isfile(ICON) else None, debug=bool(os.environ.get("DISK_REPORT_DEBUG")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
