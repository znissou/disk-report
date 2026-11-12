"""The actions the app offers on what a scan found: show something in File Explorer, delete it (to the Recycle Bin
or for good), empty the Recycle Bin, uninstall an app, run a cleaning command in a terminal window.

Each function acts on what it's given, only when the person clicks and confirms in the app, and reports what
happened. Guard decides what may be deleted at all: never Windows, program or user folders themselves, never
something the report says to keep, never an app that is still installed (uninstall it instead), and never what
such things hold inside a folder you delete: those parts are skipped and reported.
"""
from __future__ import annotations

import ctypes
import os
import re
import stat
import subprocess
import time
import winreg
from ctypes import wintypes

LONG = "\\\\?\\"                 # lets Windows APIs use paths longer than 260 characters
REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT
DIRECTORY = stat.FILE_ATTRIBUTE_DIRECTORY
READONLY = stat.FILE_ATTRIBUTE_READONLY
NAME_SURROGATE = 0x20000000      # reparse tag bit of junctions and symlinks (not of OneDrive placeholders)
CLOUD_ONLY = 0x00400000 | 0x00040000 | 0x00001000
IN_USE = (32, 33)                # sharing violation, lock violation
DENIED = (5,)                    # access denied
NO_WINDOW = 0x08000000
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
GUID = re.compile(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}")
PACKAGE = re.compile(r"[A-Za-z0-9._~-]+")

shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

# Windows Settings pages the app may open (the catalog's texts point to them)
SETTINGS = {
    "apps": "ms-settings:appsfeatures",
    "storage": "ms-settings:storagesense",
    "language": "ms-settings:regionlanguage",
    "fonts": "ms-settings:fonts",
    "protection": "SystemPropertiesProtection.exe",
}


def norm(path):
    """Compare paths case-insensitively and without a trailing backslash ('C:\\' -> 'c:')."""
    return os.path.normpath(path).rstrip("\\").lower()


def inside(path, folder):
    """True when `path` is `folder` itself or anything in it (both normalized)."""
    return path == folder or path.startswith(folder + "\\")


def size_on_disk(n, cluster):
    return 0 if n < 800 else -(-n // cluster) * cluster


def is_link(st):
    return bool(st.st_file_attributes & REPARSE and st.st_reparse_tag & NAME_SURROGATE)


# ------------------------------------------------------------------------------------------------ deleting

def delete_for_good(path, contents_only=False, keep=(), cluster=4096, progress=None):
    """Delete a file or folder without the Recycle Bin, or with contents_only only what's inside the folder.

    Links (junctions, symlinks) are removed, never followed. Paths in `keep` are left alone, and so are the
    folders that hold them. Files in use or locked are skipped and counted. progress(counts) is called now and
    then. Returns counts: freed bytes, files deleted, inUse, denied, kept, missing, errors.
    """
    out = {"freed": 0, "files": 0, "inUse": 0, "denied": 0, "kept": 0, "missing": False, "errors": []}
    keep = {norm(k) for k in keep}
    path = os.path.normpath(path)        # the \\?\ prefix turns off Windows' own clean-up of / and ..
    try:
        st = os.stat(LONG + path, follow_symlinks=False)
    except FileNotFoundError:
        out["missing"] = True
        return out
    except OSError as e:
        _failed(out, path, e)
        return out
    if is_link(st):
        if contents_only:
            out["errors"].append("It's a link to another folder, so nothing was deleted.")
        else:
            _remove_link(path, st, out)
        return out
    if not st.st_file_attributes & DIRECTORY:
        _delete_file(path, st, out, cluster)
        return out
    next_report = 0.0
    stack = [(path, False)]
    while stack:
        folder, emptied = stack.pop()
        if emptied:
            if folder != path or not contents_only:
                _remove_dir(folder, out)
            continue
        stack.append((folder, True))
        try:
            with os.scandir(LONG + folder) as found:
                entries = list(found)
        except OSError as e:
            _failed(out, folder, e)
            continue
        for entry in entries:
            p = folder + "\\" + entry.name
            if norm(p) in keep:
                out["kept"] += 1
                continue
            try:
                est = entry.stat(follow_symlinks=False)
            except OSError as e:
                _failed(out, p, e)
                continue
            if is_link(est):
                _remove_link(p, est, out)
            elif est.st_file_attributes & DIRECTORY:
                stack.append((p, False))
            else:
                _delete_file(p, est, out, cluster)
        if progress is not None and time.time() >= next_report:
            next_report = time.time() + 0.3
            progress(out)
    return out


def _failed(out, path, err):
    code = getattr(err, "winerror", None)
    if isinstance(err, FileNotFoundError):
        return
    if code in IN_USE:
        out["inUse"] += 1
    elif code in DENIED or isinstance(err, PermissionError):
        out["denied"] += 1
    elif len(out["errors"]) < 5:
        out["errors"].append("%s: %s" % (path, getattr(err, "strerror", None) or err))


def _delete_file(path, st, out, cluster):
    freed = 0 if st.st_file_attributes & CLOUD_ONLY else size_on_disk(st.st_size, cluster)
    try:
        os.unlink(LONG + path)
    except FileNotFoundError:
        return
    except OSError as e:
        if not (st.st_file_attributes & READONLY and getattr(e, "winerror", None) in DENIED):
            _failed(out, path, e)
            return
        try:                                   # a read-only file: clear the flag, as Explorer does
            os.chmod(LONG + path, stat.S_IWRITE)
            os.unlink(LONG + path)
        except OSError as e2:
            _failed(out, path, e2)
            return
    out["freed"] += freed
    out["files"] += 1


def _remove_dir(folder, out):
    try:
        os.rmdir(LONG + folder)
        return
    except OSError as e:
        code = getattr(e, "winerror", None)
        if code == 145 or isinstance(e, FileNotFoundError):   # not empty: something inside was skipped
            return
        if code not in DENIED:
            _failed(out, folder, e)
            return
    try:
        os.chmod(LONG + folder, stat.S_IWRITE)
        os.rmdir(LONG + folder)
    except OSError as e:
        if getattr(e, "winerror", None) != 145:
            _failed(out, folder, e)


def _remove_link(path, st, out):
    try:
        if st.st_file_attributes & DIRECTORY:
            os.rmdir(LONG + path)      # removes the junction or link itself, not the folder it points to
        else:
            os.unlink(LONG + path)
    except OSError as e:
        _failed(out, path, e)


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p),
                ("pTo", ctypes.c_void_p), ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
shell32.SHFileOperationW.restype = ctypes.c_int
FO_DELETE = 3
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_WANTNUKEWARNING = 0x4000


def recycle(paths, hwnd=None):
    """Move files or folders to the Recycle Bin, with Windows' own progress window and its warning before
    deleting for good anything too big for the Recycle Bin. Returns {"ok", "cancelled", "error"}."""
    paths = [os.path.normpath(p) for p in paths]
    paths = [p for p in paths if os.path.lexists(LONG + p)]
    if not paths:
        return {"ok": True, "missing": True}
    if any(len(p) >= 260 for p in paths):
        return {"ok": False, "error": "A path is too long for the Recycle Bin. Delete it permanently instead."}
    buf = ctypes.create_unicode_buffer("\0".join(paths) + "\0")   # the list ends with two nulls
    op = SHFILEOPSTRUCTW(hwnd=hwnd, wFunc=FO_DELETE, pFrom=ctypes.addressof(buf),
                         fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_WANTNUKEWARNING)
    code = shell32.SHFileOperationW(ctypes.byref(op))
    if op.fAnyOperationsAborted:
        return {"ok": False, "cancelled": True}
    if code:
        return {"ok": False, "error": "Windows couldn't move it to the Recycle Bin (code %#x)." % (code & 0xFFFFFFFF)}
    return {"ok": True}


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


shell32.SHQueryRecycleBinW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SHQUERYRBINFO)]
shell32.SHQueryRecycleBinW.restype = ctypes.c_long
shell32.SHEmptyRecycleBinW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
shell32.SHEmptyRecycleBinW.restype = ctypes.c_long
SHERB_NOCONFIRMATION = 0x1


def recycle_bin(drive):
    """What the Recycle Bin holds for one drive ('C:'): {"size": bytes, "items": count}, or None."""
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    if shell32.SHQueryRecycleBinW(drive + "\\", ctypes.byref(info)) != 0:
        return None
    return {"size": info.i64Size, "items": info.i64NumItems}


def empty_recycle_bin(drive, hwnd=None):
    """Empty the Recycle Bin of one drive for good. Returns {"ok", "freed", "items"}."""
    before = recycle_bin(drive) or {"size": 0, "items": 0}
    if not before["items"]:
        return {"ok": True, "freed": 0, "items": 0}
    code = shell32.SHEmptyRecycleBinW(hwnd, drive + "\\", SHERB_NOCONFIRMATION)
    after = recycle_bin(drive) or {"size": 0, "items": 0}
    if code != 0 and after["items"]:
        return {"ok": False, "error": "Windows couldn't empty the Recycle Bin (code %#x)." % (code & 0xFFFFFFFF)}
    return {"ok": True, "freed": max(0, before["size"] - after["size"]), "items": before["items"] - after["items"]}


# ------------------------------------------------------------------------------------------------ starting things

class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
shell32.ShellExecuteExW.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
SEE_MASK_NOCLOSEPROCESS = 0x40
SEE_MASK_NOASYNC = 0x100
SEE_MASK_FLAG_NO_UI = 0x400
CANCELLED_BY_USER = 1223          # the person said no to Windows' administrator prompt


def start(file, params="", admin=False, hwnd=None, folder=None):
    """Start a program, document or ms-settings: page the way Explorer would. Windows asks for administrator
    rights when the program needs them (or always with admin=True). Returns a process handle or None; raises
    OSError (winerror 1223 when the person declined the prompt)."""
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI
    info.hwnd = hwnd
    info.lpVerb = "runas" if admin else None
    info.lpFile = file
    info.lpParameters = params or None
    info.lpDirectory = folder
    info.nShow = 1
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return info.hProcess


def wait(handle, seconds=None):
    """Wait for a process started by start(); returns its exit code, or None if it's still running."""
    if not handle:
        return 0
    ms = 0xFFFFFFFF if seconds is None else int(seconds * 1000)
    if kernel32.WaitForSingleObject(handle, ms) != 0:
        return None
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    return code.value


def close(handle):
    if handle:
        kernel32.CloseHandle(handle)


def reveal(path):
    """Show a file or folder in File Explorer: a folder opens, a file is selected in its folder. When it no longer
    exists, the closest folder that does opens instead."""
    if os.path.isdir(path):
        os.startfile(path)
        return {"ok": True}
    if os.path.exists(path):
        subprocess.Popen('explorer.exe /select,"%s"' % path)
        return {"ok": True}
    parent = os.path.dirname(path)
    while parent and not os.path.isdir(parent) and os.path.dirname(parent) != parent:
        parent = os.path.dirname(parent)
    if os.path.isdir(parent):
        os.startfile(parent)
    return {"ok": False, "missing": True, "opened": parent}


def run_command(command, admin=False, hwnd=None):
    """Open a terminal window that runs one command and stays open, so the person sees what it does and can
    answer its questions."""
    params = "/k title Disk Report & echo ^> %s & echo. & %s" % (command, command)
    close(start("cmd.exe", params, admin=admin, hwnd=hwnd, folder=os.path.expanduser("~")))


def open_settings(key, hwnd=None):
    close(start(SETTINGS[key], hwnd=hwnd))


def copy_text(text):
    """Put text on the clipboard."""
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    data = (text + "\0").encode("utf-16-le")
    for _ in range(10):                       # another program may hold the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(0x0002, len(data))   # GMEM_MOVEABLE
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(13, handle))   # CF_UNICODETEXT
    finally:
        user32.CloseClipboard()


# ------------------------------------------------------------------------------------------------ uninstalling

def _uninstall_key(ref):
    hive = winreg.HKEY_LOCAL_MACHINE if ref.get("hive") == "HKLM" else winreg.HKEY_CURRENT_USER
    view = {64: winreg.KEY_WOW64_64KEY, 32: winreg.KEY_WOW64_32KEY}.get(ref.get("view"), 0)
    return winreg.OpenKey(hive, UNINSTALL_KEY + "\\" + ref["key"], 0, winreg.KEY_READ | view)


def _value(key, name):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def is_registered(ref):
    """True while Windows still lists the app as installed."""
    try:
        with _uninstall_key(ref):
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return True    # can't tell: assume it's still there


def split_command(command):
    """'"C:\\x\\unins000.exe" /x' or 'C:\\Program Files\\x\\uninstall.exe /S' -> (program, arguments)."""
    command = os.path.expandvars(command.strip())
    if command.startswith('"'):
        end = command.find('"', 1)
        if end > 0:
            return command[1:end], command[end + 1:].strip()
        return command[1:], ""
    m = re.match(r"(.+?\.exe)(?=\s|$)(.*)", command, re.I)
    if m:
        return m.group(1), m.group(2).strip()
    program, _, rest = command.partition(" ")
    return program, rest.strip()


def uninstall_command(ref):
    """What Windows runs to uninstall this app (as Settings → Apps does), read from the registry now, not from
    the scan. Returns (program, arguments). MSI installs go through msiexec /x, which shows Windows' own dialog."""
    with _uninstall_key(ref) as key:
        command = _value(key, "UninstallString")
        msi = _value(key, "WindowsInstaller") == 1
    command = command if isinstance(command, str) else ""
    if msi or re.match(r'^\s*"?(?:[a-z]:\\[^"]*\\)?msiexec(?:\.exe)?"?\s', command, re.I):
        code = ref["key"] if GUID.fullmatch(ref["key"]) else (GUID.search(command) or [None])[0]
        if code:
            return "msiexec.exe", "/x " + code
    if not command.strip():
        raise LookupError("Windows has no uninstall command for this app.")
    return split_command(command)


def _powershell(script, timeout):
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + script],
                          capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, creationflags=NO_WINDOW)


def remove_store_app(package):
    """Remove a Microsoft Store app for this account, as Settings → Apps does. Returns {"ok", "error"}."""
    if not PACKAGE.fullmatch(package):
        return {"ok": False, "error": "That isn't a Store app name."}
    try:
        done = _powershell("$ErrorActionPreference='Stop'; Remove-AppxPackage -Package '%s'" % package, 900)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e)}
    if done.returncode == 0:
        return {"ok": True}
    text = done.stderr.decode("utf-8", "replace").strip().splitlines()
    return {"ok": False, "error": text[0] if text else "Windows couldn't remove the app."}


def store_app_installed(package, folder=None):
    if folder:
        return os.path.isdir(folder)
    if not PACKAGE.fullmatch(package):
        return False
    try:
        done = _powershell("@(Get-AppxPackage | Where-Object PackageFullName -eq '%s').Count" % package, 60)
        return done.stdout.decode("utf-8", "replace").strip() not in ("", "0")
    except (OSError, subprocess.SubprocessError):
        return True


# ------------------------------------------------------------------------------------------------ what may go

def item_paths(item):
    """Every folder or file the report attributes to an item."""
    out = list(item.get("paths") or [])
    out += item.get("targets") or []
    out += [part[0] for part in item.get("parts") or []]
    return out


class Guard:
    """Decides what may be deleted, from the scan and a few places that are never deleted whole.

    check(path) answers with {"ok": True, "keep": [...]} (parts to leave alone inside it) or
    {"ok": False, "reason": "..."}.
    """

    def __init__(self, scan, tokens, own=()):
        meta = scan["meta"]
        self.drive = meta["drive"].upper()
        self.root = norm(self.drive + "\\")
        self.apps = scan.get("apps") or {}
        self.claims = {}
        for item in scan["items"]:
            for p in item_paths(item):
                self.claims.setdefault(norm(p), item)
        t = {k: v for k, v in tokens.items() if v}
        self.windows = norm(t["%WIN%"])
        self.programs = [norm(t[k]) for k in ("%PF%", "%PF86%", "%PD%") if k in t]
        self.store = norm(t["%PF%"] + "\\WindowsApps")
        home = t["~"]
        never = [self.drive + "\\", t["%WIN%"], t["%ROOT%"] + "\\Users", home, home + "\\AppData", t["%LOCAL%"],
                 t["%ROAMING%"], t["%LOCALLOW%"], t["%LOCAL%"] + "\\Packages", t["%LOCAL%"] + "\\Programs",
                 t["%LOCAL%"] + "\\Temp", t["%PD%"], t["%PF%"], t["%PF86%"], t.get("%CF%", ""), t.get("%CF86%", ""),
                 self.drive + "\\$Recycle.Bin", self.drive + "\\System Volume Information",
                 self.drive + "\\pagefile.sys", self.drive + "\\hiberfil.sys", self.drive + "\\swapfile.sys"]
        never += [t[k] for k in ("%DOCUMENTS%", "%DESKTOP%", "%DOWNLOADS%", "%PICTURES%", "%VIDEOS%", "%MUSIC%")
                  if k in t]
        never += [p for p, _ in meta.get("syncRoots") or []]
        never += list(own)
        self.never = {norm(p) for p in never if p}
        self._installed = {}

    def owner(self, key):
        """The item a (normalized) path belongs to: the deepest folder the report attributes to an item."""
        while True:
            item = self.claims.get(key)
            if item is not None:
                return item
            cut = key.rfind("\\")
            if cut <= 0:
                return self.claims.get(self.root)
            key = key[:cut]

    def installed(self, group):
        """Whether an app group still has a program Windows lists as installed (unknown counts as installed)."""
        found = self._installed.get(group)
        if found is not None and time.time() - found[1] < 5:
            return found[0]
        entries = self.apps.get(group) if group else None
        if not entries:
            answer = True
        else:
            answer = any(is_registered(e["ref"]) if e["kind"] == "win32"
                         else store_app_installed(e["package"], e.get("dir")) for e in entries)
        self._installed[group] = (answer, time.time())
        return answer

    def check(self, path, contents_only=False):
        if not path or not os.path.isabs(path) or path.startswith("\\\\") or ".." in path.split("\\"):
            return {"ok": False, "reason": "That isn't a path Disk Report can act on."}
        key = norm(path)
        if not key.startswith(self.root + "\\"):
            return {"ok": False, "reason": "That isn't on the %s drive this report is about." % self.drive}
        item = self.owner(key)
        if item is None:
            return {"ok": False, "reason": "That isn't part of this report."}
        name = item.get("name", "")
        if contents_only:
            if self.claims.get(key) is not item or item.get("cat") != "cache" or \
                    item.get("safety") not in ("safe", "optional"):
                return {"ok": False, "reason": "Only cache folders can be emptied."}
        elif key in self.never:
            return {"ok": False, "reason": "Disk Report never deletes this folder itself. Open it and delete what's "
                                           "inside instead."}
        if inside(key, self.store):
            return {"ok": False, "reason": "Microsoft Store apps are removed by uninstalling them."}
        safety = item.get("safety")
        if item.get("container") or safety == "keep":
            return {"ok": False, "reason": "“%s” is needed by Windows or by an app you use." % name}
        if safety == "uninstall" and self.installed(item.get("group")):
            return {"ok": False, "reason": "%s is still installed. Uninstall it instead: deleting its folders by "
                                           "hand can break it." % (item.get("group") or name)}
        if inside(key, self.windows) and safety not in ("safe", "optional"):
            return {"ok": False, "reason": "Disk Report only deletes the parts of Windows its catalog says are safe."}
        if any(inside(key, p) for p in self.programs) and safety not in ("safe", "optional", "unknown"):
            return {"ok": False, "reason": "That's part of an installed program. Uninstall the program instead."}
        keep, names = [], []
        for claimed, other in self.claims.items():
            if other is item or not claimed.startswith(key + "\\"):
                continue
            needed = other.get("container") or other.get("safety") == "keep" or \
                (other.get("safety") == "uninstall" and self.installed(other.get("group")))
            if needed:
                keep.append(claimed)
                if other.get("name") not in names:
                    names.append(other.get("name"))
        return {"ok": True, "keep": keep, "keepNames": names[:6]}
