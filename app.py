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
import hashlib
import hmac
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
# Once an uninstaller and everything it started have closed, how long Windows gets to drop the app from its list
# before Disk Report says it's still installed (some uninstallers hand over to a program that's already running).
UNINSTALL_GRACE = 60
REFUSED = 2          # the administrator helper's exit code when its task file changed after the app wrote it
UNREPORTED = 3       # ... and when its results folder was swapped for a link while it worked, so it reported nothing


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


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


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
    """Append a record to the action log. Answers it as logged, with its time, which the page uses to tell records
    apart (so one it already showed doesn't count twice when the log is read again)."""
    record = dict(record, time=time.time())
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        log_error("writing the action log")
    return record


def scan_started(meta):
    """When a scan began (seconds since 1970). Newer scans save it; for older ones it's worked out from when the scan
    ended (to the minute) and how long it took, which errs on the early side."""
    if meta.get("started"):
        return meta["started"]
    try:
        ended = dt.datetime.strptime(meta["generated"], "%Y-%m-%d %H:%M").timestamp()
    except (KeyError, TypeError, ValueError):
        return None
    return ended - (meta.get("seconds") or 0)


def actions_for(scan_id, drive=None, since=None):
    """What was done since a scan, so the report can show it again after a restart: what was logged for this scan,
    and what was done on its drive while it was being made (logged for the scan before it, which the page showed
    meanwhile). For those, an uninstalled app is matched by name: its place in the list is the other scan's."""
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
                elif since is not None and drive and record.get("scan") and record.get("drive") == drive and \
                        (record.get("time") or 0) >= since:
                    record.pop("index", None)
                    out.append(record)
    except OSError:
        pass
    return out


def bin_marks(records, drive):
    """Where what Disk Report moved to the Recycle Bin is now, added to those records as "bin": {path: where}:
    "in" (still in the Recycle Bin), "restored" (back where it was), "gone" (the Recycle Bin was emptied since), or
    "mixed" (part of it back, part gone). Moves logged by Disk Report 2.0.x name no pieces: only those of whole files
    and folders can be followed."""
    moves = [r for r in records if r.get("kind") == "delete" and r.get("mode") == "trash"]
    if not moves:
        return records
    held = actions.recycle_bin_items(drive)
    for r in moves:
        where = {}
        moved = r.get("pieces")
        for p in r.get("paths") or []:
            path = p.get("path")
            if not path or p.get("state") not in ("trashed", "partial"):
                continue
            if moved is not None:
                key = actions.norm(path)
                pieces = [q for q in moved if actions.inside(actions.norm(q), key)]
            elif p["state"] == "trashed" and not r.get("contents"):
                pieces = [path]
            else:
                continue
            if not pieces:
                continue
            if any(actions.norm(q) in held for q in pieces):
                where[path] = "in"
            else:
                back = [os.path.lexists(actions.LONG + q) for q in pieces]
                where[path] = "restored" if all(back) else "gone" if not any(back) else "mixed"
        if where:
            r["bin"] = where
    return records


def installed_programs(drive, tokens):
    """[(folder, name)] for the programs Windows lists as installed, read from the registry now (a few hundredths of a
    second): the guard checks them before each delete, as the scan may be days old."""
    info = engine.SysInfo(drive, tokens)
    info.read_uninstall()
    return [(e["dir"], e["name"]) for e in info.uninstall if e["dir"]]


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


def parts_to_recycle(path, keep, leave, contents_only):
    """The largest pieces of a file or folder that hold nothing to keep or leave: the folder itself when that's
    possible, or with contents_only what's inside it (a file goes whole). Returns (pieces, kept, left): the pieces,
    and how many parts were skipped because they're needed, or listed on their own with a more careful label."""
    keep = {actions.norm(k) for k in keep}
    leave = {actions.norm(k) for k in leave} - keep
    stays = keep | leave

    def holds(p):
        key = actions.norm(p)
        return any(k == key or k.startswith(key + "\\") for k in stays)

    try:
        st = os.stat(actions.LONG + path, follow_symlinks=False)
    except OSError:
        return [], 0, 0                      # already gone
    if actions.is_link(st):
        return ([], 0, 0) if contents_only else ([path], 0, 0)   # a link goes itself, what it points to never
    if not st.st_file_attributes & actions.DIRECTORY:
        return [path], 0, 0
    if not contents_only and not holds(path):
        return [path], 0, 0
    out, kept, left, stack = [], 0, 0, [path]
    while stack:
        folder = stack.pop()
        try:
            names = os.listdir(actions.LONG + folder)
        except OSError:
            continue
        for name in names:
            p = folder + "\\" + name
            key = actions.norm(p)
            if key in keep:
                kept += 1
            elif key in leave:
                left += 1
            else:
                (stack if holds(p) else out).append(p)
    return out, kept, left


def trash_state(path, pieces, contents):
    """What became of a path after its pieces went to the Recycle Bin: trashed, partial or failed."""
    moved = sum(1 for p in pieces if not os.path.lexists(actions.LONG + p))
    if not os.path.lexists(actions.LONG + path) or (contents and moved == len(pieces)):
        return "trashed"
    return "partial" if moved else "failed"


def forever_state(path, contents, result):
    """What became of a path after delete_for_good(): deleted, emptied, partial or failed."""
    if not os.path.lexists(actions.LONG + path):
        return "deleted"
    if contents and not (result["inUse"] or result["denied"] or result["errors"] or result.get("stopped")):
        return "emptied"
    return "partial" if result["files"] or result["kept"] or result.get("left") else "failed"


def marks(verdicts, mode):
    """For the page, after a delete: the other listed things that went with it, and what was left alone."""
    inner, untouched = [], []
    for _, verdict in verdicts:
        inner += [{"path": p, "state": "trashed" if mode == "trash" else "deleted"} for p in verdict["also"]
                  if not os.path.lexists(actions.LONG + p)]
        # only what's still there: something deleted earlier from its own row stays marked as deleted
        untouched += [p for p in verdict["keep"] + verdict["leave"] if os.path.lexists(actions.LONG + p)]
    return {"inner": inner, "untouched": untouched}


# ------------------------------------------------------------------------------------------------ the app's API

class Loaded:
    """A saved scan, with the guard that decides what may be deleted from it."""

    def __init__(self, scan_id, data):
        self.id = scan_id
        self.data = data
        drive = data["meta"]["drive"]
        self.tokens = engine.path_tokens(drive)
        self.guard = actions.Guard(data, self.tokens, own_folders(),
                                   installs=lambda: installed_programs(drive, self.tokens))


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

    def running(self):
        return [task for task in list(self._tasks.values()) if not task["done"]]


class Api:
    """What the page can ask for. Each method answers with plain data (pywebview turns it into JSON)."""

    def __init__(self):
        self._window = None
        self._lock = threading.Lock()
        self._loaded = {}
        self._tasks = Tasks()
        self._scan = {"running": False}
        self._cancel = None
        self._closing = False

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
            "done": self.done(drive) if loaded else [],
            "protected": sorted(loaded.guard.never) if loaded else [],
            "locked": sorted(loaded.guard.locked) if loaded else [],
            "programRoots": loaded.guard.program_roots if loaded else [],
            "commands": K.COMMANDS, "admin": engine.is_admin(), "theme": settings().get("theme"),
            "scanning": self.scan_status(),
        }

    def done(self, drive):
        """What was done since the drive's scan, with where each move to the Recycle Bin is now. The page asks again
        when it's shown again, so a restore from the Recycle Bin (or the bin emptied in Windows) shows at once."""
        loaded = self._get(engine.normalize_drive(drive))
        if loaded is None:
            return []
        meta = loaded.data["meta"]
        return bin_marks(actions_for(loaded.id, meta["drive"], scan_started(meta)), meta["drive"])

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
        """Before the confirmation: may these be deleted, and what else the report lists inside them: what goes with
        them (also), and what stays (leave: listed on its own with a more careful label; keep: needed)."""
        loaded = self._get(engine.normalize_drive(drive))
        if loaded is None:
            return {"ok": False, "reason": "Scan the drive first."}
        paths = clean_paths(paths)
        found, missing = {"also": {}, "leave": {}, "keep": {}}, []
        for p in paths:
            verdict = loaded.guard.check(p, bool(contents))
            if not verdict["ok"]:
                return {"ok": False, "reason": verdict["reason"], "path": p}
            for how, items in found.items():
                for entry in verdict[how + "Items"]:
                    same = items.setdefault((entry["name"], entry["safety"]), dict(entry, size=0))
                    same["size"] = None if same["size"] is None or entry["size"] is None else \
                        same["size"] + entry["size"]
            if not os.path.lexists(actions.LONG + p):
                missing.append(p)
        sync = sorted({prov for p in paths for root, prov in loaded.data["meta"].get("syncRoots") or []
                       if actions.inside(actions.norm(p), actions.norm(root))})
        out = {"ok": True, "missing": missing, "sync": sync}
        for how, items in found.items():
            entries = sorted(items.values(), key=lambda e: -(e["size"] or 0))
            out[how], out[how + "Count"] = entries[:5], len(entries)
        # what stays inside, at the last scan: the page takes it off the size it shows
        out["staysSize"] = sum(e["size"] or 0 for how in ("leave", "keep") for e in found[how].values())
        return out

    def delete(self, drive, paths, mode, contents=False):
        """Delete what the person confirmed: mode 'trash' (Recycle Bin) or 'forever'. Returns a task to poll."""
        if mode not in ("trash", "forever"):
            return {"ok": False, "error": "Disk Report only moves things to the Recycle Bin or deletes them permanently."}
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
            verdicts.append((p, verdict))
        cluster = loaded.data["meta"].get("cluster") or 4096

        def work(task):
            if mode == "trash":
                record = self._to_recycle_bin(verdicts, contents)
            else:
                record = self._delete_for_good(verdicts, contents, cluster, task)
            record.update(marks(verdicts, mode), kind="delete", mode=mode, contents=contents,
                          drive=loaded.data["meta"]["drive"], scan=loaded.id)
            return dict(log_action(record), ok=True, usage=usage(loaded.data["meta"]["drive"]))

        return {"ok": True, "task": self._tasks.start(work)}

    def _to_recycle_bin(self, verdicts, contents):
        """Everything confirmed goes to the Recycle Bin in one go: one progress window, whose Cancel stops it all."""
        pieces, kept, left = {}, 0, 0
        for p, verdict in verdicts:
            pieces[p], k, n = parts_to_recycle(p, verdict["keep"], verdict["leave"], contents)
            kept, left = kept + k, left + n
        result = actions.recycle([q for found in pieces.values() for q in found], self._hwnd())
        # what really went to the Recycle Bin, so the page can tell later whether it was restored from there
        moved = [q for found in pieces.values() for q in found if not os.path.lexists(actions.LONG + q)]
        record = {"freed": 0, "files": 0, "inUse": 0, "denied": 0, "kept": kept, "left": left,
                  "errors": [result["error"]] if result.get("error") else [], "cancelled": bool(result.get("cancelled")),
                  "paths": [{"path": p, "state": trash_state(p, pieces[p], contents)} for p, _ in verdicts]}
        if len(moved) <= 500:            # a cache emptied of more pieces than that isn't followed
            record["pieces"] = moved
        return record

    @staticmethod
    def _delete_for_good(verdicts, contents, cluster, task):
        total = {"freed": 0, "files": 0, "inUse": 0, "denied": 0, "kept": 0, "left": 0, "errors": [],
                 "stopped": False}
        states = []
        for p, verdict in verdicts:
            if total["stopped"]:
                states.append({"path": p, "state": "failed"})
                continue

            def progress(counts, done=dict(total)):
                task["progress"] = {"files": done["files"] + counts["files"], "freed": done["freed"] + counts["freed"]}

            result = actions.delete_for_good(p, contents, verdict["keep"], cluster, progress, leave=verdict["leave"],
                                             stop=lambda: task["stop"])
            for k in ("freed", "files", "inUse", "denied", "kept", "left"):
                total[k] += result[k]
            total["errors"] += result["errors"]
            total["stopped"] = result["stopped"]
            states.append({"path": p, "state": forever_state(p, contents, result)})
        return dict(total, paths=states)

    def delete_as_admin(self, drive, paths, contents=False):
        """The same as delete(..., 'forever'), in a helper Windows starts with administrator rights."""
        loaded = self._get(engine.normalize_drive(drive))
        if loaded is None:
            return {"ok": False, "error": "Scan the drive first."}
        paths = clean_paths(paths)
        verdicts = []
        for p in paths:
            verdict = loaded.guard.check(p, bool(contents))
            if not verdict["ok"]:
                return {"ok": False, "error": verdict["reason"]}
            verdicts.append((p, verdict))

        def work(task):
            os.makedirs(TEMP_DIR, exist_ok=True)
            name = uuid.uuid4().hex
            task_file = os.path.join(TEMP_DIR, name + ".json")
            # resolved now, so the helper can tell if the folder is swapped for a link before it writes its results
            result_file = os.path.join(os.path.realpath(TEMP_DIR), name + ".result.json")
            scan_file = os.path.join(SCANS_DIR, loaded.id)
            write_json(task_file, {"scan": scan_file, "scanHash": sha256(scan_file), "tokens": loaded.tokens,
                                   "own": own_folders(), "paths": list(paths), "contents": bool(contents),
                                   "cluster": loaded.data["meta"].get("cluster") or 4096, "result": result_file})
            # The task file sits in a folder any program of yours can write to: the helper only acts on it when it
            # still has the fingerprint taken here, which reaches it on its command line.
            digest = sha256(task_file)
            if getattr(sys, "frozen", False):
                program, params = sys.executable, '--elevated "%s" %s' % (task_file, digest)
            else:
                program, params = sys.executable, '"%s" --elevated "%s" %s' % (os.path.abspath(__file__), task_file,
                                                                               digest)
            try:
                handle = actions.start(program, params, admin=True, hwnd=self._hwnd())
            except OSError as e:
                if getattr(e, "winerror", None) == actions.CANCELLED_BY_USER:
                    return {"ok": False, "cancelled": True}
                raise
            code = actions.wait(handle)
            actions.close(handle)
            results = read_json(result_file, []) or []
            for f in (task_file, result_file):
                try:
                    os.remove(f)
                except OSError:
                    pass
            total = {"freed": 0, "files": 0, "inUse": 0, "denied": 0, "kept": 0, "left": 0, "errors": []}
            states = []
            for r in results:
                if r.get("refused"):
                    total["errors"].append(r["refused"])
                    states.append({"path": r["path"], "state": "failed"})
                    continue
                for k in ("freed", "files", "inUse", "denied", "kept", "left"):
                    total[k] += r.get(k, 0)
                total["errors"] += r.get("errors", [])
                states.append({"path": r["path"], "state": forever_state(r["path"], contents, r)})
            if code == REFUSED:          # it touched nothing
                total["errors"].append("The administrator helper refused the task: its file was changed after Disk "
                                       "Report wrote it.")
                states = [{"path": p, "state": "failed"} for p in paths]
            elif code == UNREPORTED:
                total["errors"].append("The administrator helper didn't report back: Disk Report's temporary folder "
                                       "was changed while it worked.")
            elif not results:
                total["errors"].append("The administrator helper didn't report back.")
            record = dict(total, kind="delete", mode="forever", admin=True, contents=bool(contents), paths=states,
                          drive=loaded.data["meta"]["drive"], scan=loaded.id)
            record.update(marks(verdicts, "forever"))
            return dict(log_action(record), ok=True, usage=usage(loaded.data["meta"]["drive"]))

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
            logged = log_action(dict(result, kind="empty-bin", drive=drive, scan=loaded.id if loaded else None))
            return dict(logged, usage=usage(drive))

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
            already = state == "already"
            if already:              # gone before the click: as good as uninstalled, and remembered as such
                state = "removed"
            loaded.guard._installed.pop(group, None)
            log_action({"kind": "uninstall", "group": group, "index": index, "name": entry["name"], "state": state,
                        "already": already, "error": error, "drive": loaded.data["meta"]["drive"], "scan": loaded.id})
            return {"ok": state == "removed", "state": state, "already": already, "error": error, "group": group,
                    "index": index, "name": entry["name"], "usage": usage(loaded.data["meta"]["drive"])}

        return {"ok": True, "task": self._tasks.start(work)}

    def _uninstall(self, entry, task):
        """Run an app's uninstaller and wait. Answers (state, error): removed; already (it was gone before, removed
        outside Disk Report); kept (the uninstaller closed and Windows still lists the app); stopped (the person
        stopped waiting); cancelled; error."""
        if entry["kind"] == "store":
            if not actions.store_app_installed(entry["package"], entry.get("dir")):
                return "already", None
            result = actions.remove_store_app(entry["package"])
            return ("removed", None) if result["ok"] else ("error", result["error"])
        if not actions.is_registered(entry["ref"]):
            return "already", None
        try:
            program, params = actions.uninstall_command(entry["ref"])
        except FileNotFoundError:    # removed between the two looks
            return "already", None
        except (OSError, LookupError) as e:
            return "error", str(e)
        try:
            handle = actions.start(program, params, hwnd=self._hwnd())
        except OSError as e:
            if getattr(e, "winerror", None) == actions.CANCELLED_BY_USER:
                return "cancelled", None
            return "error", "Windows couldn't start the uninstaller: %s" % (e.strerror or e)
        # Some uninstallers hand over to a copy of themselves and exit at once, so every process the uninstaller
        # starts is followed, and what counts in the end is whether Windows still lists the app.
        tree = actions.ProcessTree(handle)
        started, closed = time.time(), None
        try:
            while True:
                if not actions.is_registered(entry["ref"]):
                    return "removed", None
                if task["stop"] or time.time() - started > 3600:
                    return "stopped", None
                running = tree.alive() if tree.root else True   # no process to follow: wait until stopped
                task["progress"] = {"running": running}
                if running:
                    closed = None
                elif closed is None:
                    closed = time.time()
                elif time.time() - closed > UNINSTALL_GRACE:
                    return "kept", None
                # often at first, to see the short-lived processes an uninstaller may hand over through
                time.sleep(0.2 if time.time() - started < 10 else 1.0)
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

    # -- closing
    def _on_closing(self):   # with an underscore, so the page can't call it: pywebview offers it public methods
        """The window's close button. While something is being deleted or uninstalled, the window stays until it's
        done (a permanent delete stops at once, so does waiting for an uninstaller), so that what happened is in the
        log; then it closes by itself. Clicking close again closes at once."""
        busy = self._tasks.running()
        if not busy or self._closing:
            return True
        self._closing = True
        for task in busy:
            task["stop"] = True
        threading.Thread(target=self._close_when_done, daemon=True).start()
        return False

    def _close_when_done(self):
        try:
            self._window.run_js("window.closingSoon && closingSoon()")
        except Exception:   # the page tells the person why the window stays; nothing else depends on it
            pass
        while self._tasks.running():
            time.sleep(0.2)
        self._window.destroy()


# ------------------------------------------------------------------------------------------------ elevated helper

def elevated(task_file, digest=""):
    """Runs with administrator rights: delete what the app asked for, after the same checks, and report back.

    `digest` is the task file's SHA-256, taken by the app and passed on the command line: a task file changed since
    is refused (exit code REFUSED), and so is a scan file changed after the app wrote the task. The results go to a
    new file at the path the app resolved, never through a folder swapped for a link since (exit code UNREPORTED)."""
    try:
        with open(task_file, "rb") as f:
            raw = f.read()
    except OSError:
        return REFUSED
    if not digest or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), digest.lower()):
        return REFUSED
    task = json.loads(raw.decode("utf-8"))
    results = []
    try:
        try:
            with open(task["scan"], "rb") as f:
                scan_raw = f.read()
        except OSError:
            scan_raw = b""
        if hashlib.sha256(scan_raw).hexdigest() != task.get("scanHash"):
            results = [{"path": p, "refused": "The scan changed after Disk Report checked it. Scan again."}
                       for p in task["paths"]]
        else:
            scan = json.loads(scan_raw.decode("utf-8"))
            guard = actions.Guard(scan, task["tokens"], task["own"],
                                  installs=lambda: installed_programs(scan["meta"]["drive"], task["tokens"]))
            for path in task["paths"]:
                verdict = guard.check(path, task["contents"])
                if not verdict["ok"]:
                    results.append({"path": path, "refused": verdict["reason"]})
                    continue
                result = actions.delete_for_good(path, task["contents"], verdict["keep"], task["cluster"],
                                                 leave=verdict["leave"])
                results.append(dict(result, path=path))
    finally:
        reported = actions.write_new(task["result"], json.dumps(results, ensure_ascii=False).encode("utf-8"))
    return 0 if reported else UNREPORTED


# ------------------------------------------------------------------------------------------------ window

def page():
    with open(engine.TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    return html.replace("__REPORT_TITLE__", APP_NAME).replace("__REPORT_DATA__", "null")


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--elevated":
        return elevated(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
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
    window.events.closing += api._on_closing
    webview.start(private_mode=False, storage_path=os.path.join(DATA_DIR, "webview"),
                  icon=ICON if os.path.isfile(ICON) else None, debug=bool(os.environ.get("DISK_REPORT_DEBUG")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
