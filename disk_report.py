#!/usr/bin/env python3
"""Explain what is using the space on a Windows drive, as an HTML report.

The report sorts everything into your files, apps and their data, caches, developer tools
and Windows itself, and says for each item what it is, whether it's safe to delete and how
to clean it. It only reads: nothing is ever deleted or changed.

    py disk_report.py              scan C: and open the report
    py disk_report.py --drive D    scan another drive
    py disk_report.py --no-open    don't open the report when done
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
import winreg
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import catalog as K  # noqa: E402

VERSION = "1.0"
MB = 1024 ** 2
GB = 1024 ** 3
KEEP_NODE = 2 * MB      # folders smaller than this are folded into their parent
FILE_NODE = 256 * MB    # files at least this big get their own entry, so rules can name them
BIG_FILE = 20 * MB      # files at least this big are remembered (biggest files, duplicates)
MIN_ITEM = 1 * MB       # smaller items are summed up instead of listed
DETAIL_MIN = 10 * MB    # items at least this big get a breakdown of what's inside
NOTE_MIN = 50 * MB      # "no matching app" notes only for folders at least this big

CLOUD_ONLY = 0x00400000 | 0x00040000 | 0x00001000  # recall on data access / on open, offline
NAME_SURROGATE = 0x20000000   # reparse tag bit set by junctions and symlinks
REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT
DIRECTORY = stat.FILE_ATTRIBUTE_DIRECTORY
LONG = "\\\\?\\"              # lets Windows APIs open paths longer than 260 characters
NO_WINDOW = 0x08000000

MARKERS = {"package.json": K.MARK_PACKAGE_JSON, "pubspec.yaml": K.MARK_PUBSPEC, "pyvenv.cfg": K.MARK_PYVENV,
           "cargo.toml": K.MARK_CARGO, "build.gradle": K.MARK_GRADLE, "build.gradle.kts": K.MARK_GRADLE,
           "settings.gradle": K.MARK_GRADLE, "settings.gradle.kts": K.MARK_GRADLE,
           "pyproject.toml": K.MARK_PYPROJECT, "setup.py": K.MARK_PYPROJECT, "cmakelists.txt": K.MARK_CMAKE,
           "pom.xml": K.MARK_POM}
PROJECT_EXTS = {".csproj", ".fsproj", ".vbproj"}
# the marker names as they're usually written, so the scan doesn't have to lower-case every file name
MARKER_NAMES = dict(MARKERS)
MARKER_NAMES.update({"Cargo.toml": K.MARK_CARGO, "CMakeLists.txt": K.MARK_CMAKE, "Package.json": K.MARK_PACKAGE_JSON,
                     "Pubspec.yaml": K.MARK_PUBSPEC})

FILE_TYPES = {
    "Videos": "mp4 mkv avi mov wmv webm m4v ts mts m2ts flv mpg mpeg 3gp vob",
    "Pictures": "jpg jpeg png gif webp heic heif bmp tif tiff raw cr2 cr3 nef arw dng orf rw2 psd psb svg ico",
    "Music & audio": "mp3 wav flac m4a aac ogg opus wma aiff mid",
    "Documents": "pdf doc docx xls xlsx xlsm ppt pptx odt ods odp rtf txt md epub csv",
    "Archives & disk images": "zip rar 7z tar gz tgz bz2 xz zst iso img dmg vhd vhdx vmdk qcow2 ova cab wim",
    "Installers": "exe msi msix msixbundle appx appxbundle apk xapk aab",
    "AI models & data": "pt pth ckpt safetensors onnx gguf h5 tflite npy npz parquet pkl feather arrow",
    "Code & project files": "py pyc pyd js mjs cjs ts tsx jsx json map css scss html dart java kt class jar go rs "
                            "c h cpp hpp cs dll so lib a o obj pdb wasm lock yaml yml toml xml gradle sh bat ps1 ipynb",
    "Databases": "db sqlite sqlite3 mdb accdb",
    "Unfinished downloads": "crdownload part partial fdmdownload download opdownload",
}
TYPE_OF = {ext: kind for kind, exts in FILE_TYPES.items() for ext in exts.split()}
DAY = 86400
AGE_LIMITS = (30 * DAY, 365 * DAY, 3 * 365 * DAY)   # under a month / a year / three years / older

STOP_WORDS = set("""microsoft corporation corp inc llc ltd limited gmbh co company software technologies
technology the for and x64 x86 64 bit 32 version update app apps desktop client windows edition tools runtime
service services preview beta ag sa sas srl group international solutions systems system labs studios
entertainment interactive media digital network foundation project team pc""".split())
SYNC_PROVIDERS = {"onedrive": "OneDrive", "synologydrive": "Synology Drive", "crossdevice": "Phone Link",
                  "googledrivefs": "Google Drive", "dropbox": "Dropbox", "icloud": "iCloud"}


def say(text, end="\n"):
    sys.stdout.write(text + end)
    sys.stdout.flush()


def human(n):
    if n >= 100 * GB:
        return "%.0f GB" % (n / GB)
    if n >= GB:
        return "%.1f GB" % (n / GB)
    if n >= MB:
        return "%.0f MB" % (n / MB)
    return "%.0f KB" % (n / 1024)


def norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def words(text):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP_WORDS]


def fill(text, caps, app=None):
    """Put wildcard captures ({0}, {1}...) and the app name ({app}) into a rule's text."""
    if not text:
        return text
    text = re.sub(r"\{(\d)\}", lambda m: caps[int(m.group(1))] if int(m.group(1)) < len(caps) else m.group(0), text)
    if app is not None:
        text = text.replace("{app}", app)
    return text


def pretty_folder(name):
    return name[:1].upper() + name[1:] if name.islower() else name


def clean_app_name(name):
    """'NVIDIA Graphics Driver 616.64' -> 'NVIDIA Graphics Driver', 'Foo (64-bit)' -> 'Foo'."""
    out = re.sub(r"\s*\([^)]*\)", "", name)
    out = re.sub(r"\s+(x64|x86|64-bit|32-bit|amd64)\b", "", out, flags=re.I)
    out = re.sub(r"\s+v?\d+([.\-_]\d+)*[a-z]?\s*$", "", out, flags=re.I)
    return out.strip(" -") or name


def alias(name):
    """The app group a program name belongs to, with optional category/safety/description overrides."""
    low = name.lower()
    for rx, group, cat, safety, desc in K.GROUP_ALIASES:
        if re.search(rx, low):
            return group, cat, safety, desc
    return name, None, None, None


def canon_group(name, from_registry=False):
    if not name:
        return None
    return alias(clean_app_name(name) if from_registry else name)[0]


# ---------------------------------------------------------------------------------------- Windows facts

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


KNOWN_FOLDERS = {"%DOCUMENTS%": ("FDD39AD0-238F-46AF-ADB4-6C85480369C7", "Documents"),
                 "%DESKTOP%": ("B4BFCC3A-DB2C-424C-B029-7FE99A87C641", "Desktop"),
                 "%DOWNLOADS%": ("374DE290-123F-4565-9164-39C4925E467B", "Downloads"),
                 "%PICTURES%": ("33E28130-4E1E-4676-835A-98395C3BC3BB", "Pictures"),
                 "%VIDEOS%": ("18989B1D-99B5-455B-841C-AB7C74E4DDFC", "Videos"),
                 "%MUSIC%": ("4BD8D571-6D19-48D3-BE97-422220080E43", "Music")}


def known_folder(guid_text):
    u = uuid.UUID(guid_text)
    guid = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
    out = ctypes.c_wchar_p()
    get = ctypes.windll.shell32.SHGetKnownFolderPath
    get.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    if get(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
        return None
    try:
        return out.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))


def path_tokens(drive):
    env = os.environ
    home = os.path.expanduser("~")
    tokens = {
        "%ROOT%": drive,
        "%LOCALLOW%": os.path.join(home, "AppData", "LocalLow"),
        "%LOCAL%": env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local"),
        "%ROAMING%": env.get("APPDATA") or os.path.join(home, "AppData", "Roaming"),
        "%PD%": env.get("ProgramData") or drive + "\\ProgramData",
        "%PF86%": env.get("ProgramFiles(x86)") or drive + "\\Program Files (x86)",
        "%PF%": env.get("ProgramW6432") or env.get("ProgramFiles") or drive + "\\Program Files",
        "%CF86%": env.get("CommonProgramFiles(x86)") or drive + "\\Program Files (x86)\\Common Files",
        "%CF%": env.get("CommonProgramW6432") or env.get("CommonProgramFiles") or drive + "\\Program Files\\Common Files",
        "%WIN%": env.get("SystemRoot") or drive + "\\Windows",
        "~": home,
        "%FLUTTER%": None,
    }
    for token, (guid, fallback) in KNOWN_FOLDERS.items():
        try:
            tokens[token] = known_folder(guid) or os.path.join(home, fallback)
        except Exception:   # any ctypes failure: fall back to the usual location
            tokens[token] = os.path.join(home, fallback)
    flutter = shutil.which("flutter")
    if flutter:
        tokens["%FLUTTER%"] = os.path.dirname(os.path.dirname(os.path.realpath(flutter)))
    return tokens


def cluster_size(root):
    spc, bps, free, total = (wintypes.DWORD() for _ in range(4))
    ok = ctypes.windll.kernel32.GetDiskFreeSpaceW(root, ctypes.byref(spc), ctypes.byref(bps),
                                                  ctypes.byref(free), ctypes.byref(total))
    return (spc.value * bps.value) if ok and spc.value * bps.value else 4096


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def subkeys(key):
    i = 0
    while True:
        try:
            yield winreg.EnumKey(key, i)
        except OSError:
            return
        i += 1


def values(key):
    out = {}
    i = 0
    while True:
        try:
            name, data, _ = winreg.EnumValue(key, i)
        except OSError:
            return out
        out[name] = data
        i += 1


def run_powershell(script, timeout=180):
    full = "$ErrorActionPreference='SilentlyContinue';[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + script
    try:
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-Command", full], capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.decode("utf-8-sig", "replace").strip() or None


def exe_path(command):
    """The program path at the start of an uninstall string or icon path, if any."""
    if not command or not isinstance(command, str):
        return None
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        path = command[1:end] if end > 0 else command[1:]
    else:
        m = re.match(r"(.+?\.(exe|ico|dll))", command, re.I)
        path = m.group(1) if m else command.split(",")[0]
    path = re.sub(r",\s*-?\d+$", "", path.strip())
    if ":\\" not in path or os.path.basename(path).lower() in ("msiexec.exe", "rundll32.exe", "cmd.exe"):
        return None
    return path


GENERIC_SUBDIRS = {"bin", "bin64", "x64", "x86", "application", "app", "program", "current", "lib", "uninstall",
                   "uninstaller", "_uninstaller", "uninst", "setup", "installer", "resources"}


class SysInfo:
    """What Windows knows about the installed apps, read once before classifying."""

    def __init__(self, drive, tokens):
        self.drive = drive
        self.tokens = tokens
        self.admin = is_admin()
        self.uninstall = []
        self.packages = []
        self.package_names = {}
        self.pfns = set()
        self.pythons = set()
        self.avd_images = {}
        self.avd_names = {}
        self.sync_roots = []
        self.restore_points = None
        self.apps = AppIndex()
        self.linked_dirs = set()

    def load(self):
        self.read_uninstall()
        self.read_packages()
        self.read_pythons()
        self.read_avds()
        self.read_sync_roots()
        if self.admin:
            self.read_restore_points()
        for e in self.uninstall:
            if not e.get("system"):
                self.apps.add(e["name"], e.get("publisher", ""))
            if e["dir"]:
                self.linked_dirs.add(e["dir"].lower())
        for p in self.packages:
            if p.get("l"):
                self.linked_dirs.add(p["l"].rstrip("\\").lower())
            if not p.get("fw") and p.get("f") in self.package_names:
                self.apps.add(self.package_names[p["f"]], "")
        self.linked_dirs |= self.pythons

    def containers(self):
        t = self.tokens
        keys = [t["%ROOT%"], t["~"], t["~"] + "\\AppData", t["%LOCAL%"], t["%ROAMING%"], t["%LOCALLOW%"],
                t["%LOCAL%"] + "\\Programs", t["%PD%"], t["%PF%"], t["%PF86%"], t["%CF%"], t["%CF86%"], t["%WIN%"],
                t["%ROOT%"] + "\\Users", t["%LOCAL%"] + "\\Packages"]
        return {k.rstrip("\\").lower() for k in keys if k}

    def install_dir(self, entry):
        containers = self.containers()
        win = self.tokens["%WIN%"].lower()
        candidates = [entry.get("InstallLocation")]
        candidates += [os.path.dirname(p) for p in (exe_path(entry.get("DisplayIcon")),
                                                    exe_path(entry.get("UninstallString"))) if p]
        for c in candidates:
            if not c or not isinstance(c, str):
                continue
            c = c.strip().strip('"').rstrip("\\")
            if len(c) < 4 or not c.lower().startswith(self.drive.lower()):
                continue
            low = c.lower()
            if low.startswith(win) or "\\package cache" in low or "\\temp\\" in low or low in containers:
                continue
            while (os.path.basename(c).lower() in GENERIC_SUBDIRS or re.fullmatch(r"[\d.]+|app-[\d.]+",
                                                                                     os.path.basename(c).lower())) \
                    and os.path.dirname(c).lower() not in containers:
                c = os.path.dirname(c)
            if c.lower() in containers:
                continue
            if os.path.isdir(c):
                return c
        return None

    def read_uninstall(self):
        seen = set()
        views = [(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                 (winreg.HKEY_CURRENT_USER, 0)]
        for hive, view in views:
            try:
                root = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0,
                                      winreg.KEY_READ | view)
            except OSError:
                continue
            with root:
                for sub in list(subkeys(root)):
                    try:
                        with winreg.OpenKey(root, sub) as k:
                            v = values(k)
                    except OSError:
                        continue
                    name = v.get("DisplayName")
                    if not isinstance(name, str) or not name.strip() or v.get("ParentKeyName"):
                        continue
                    entry = {"name": name.strip(), "publisher": (v.get("Publisher") or "").strip()
                             if isinstance(v.get("Publisher"), str) else "",
                             "system": v.get("SystemComponent") == 1,
                             "dir": self.install_dir(v)}
                    key = (entry["name"].lower(), (entry["dir"] or "").lower())
                    if key not in seen:
                        seen.add(key)
                        self.uninstall.append(entry)

    def read_packages(self):
        out = run_powershell(
            "$p=@(Get-AppxPackage|ForEach-Object{[pscustomobject]@{n=$_.Name;f=$_.PackageFamilyName;"
            "l=$_.InstallLocation;fw=[bool]$_.IsFramework;s=[string]$_.SignatureKind;nr=[bool]$_.NonRemovable}});"
            "$s=@(Get-StartApps|ForEach-Object{[pscustomobject]@{n=$_.Name;a=$_.AppID}});"
            "ConvertTo-Json -Compress -Depth 3 @{p=$p;s=$s}")
        if not out:
            return
        try:
            data = json.loads(out)
        except ValueError:
            return
        self.packages = [p for p in (data.get("p") or []) if isinstance(p, dict)]
        self.pfns = {(p.get("f") or "").lower() for p in self.packages}
        for s in data.get("s") or []:
            app_id = (s or {}).get("a") or ""
            if "!" in app_id and s.get("n"):
                self.package_names.setdefault(app_id.split("!")[0], s["n"])

    def package_name(self, pfn):
        if pfn in self.package_names:
            return self.package_names[pfn]
        for p in self.packages:
            if (p.get("f") or "").lower() == pfn.lower():
                return pretty_package(p.get("n") or pfn)
        return pretty_package(pfn.split("_")[0])

    def read_pythons(self):
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    base = winreg.OpenKey(hive, r"Software\Python", 0, winreg.KEY_READ | view)
                except OSError:
                    continue
                with base:
                    for company in list(subkeys(base)):
                        try:
                            with winreg.OpenKey(base, company) as ck:
                                for tag in list(subkeys(ck)):
                                    try:
                                        path = winreg.QueryValue(ck, tag + r"\InstallPath")
                                    except OSError:
                                        continue
                                    if path:
                                        self.pythons.add(path.rstrip("\\").lower())
                        except OSError:
                            continue

    def read_avds(self):
        root = os.environ.get("ANDROID_AVD_HOME") or os.path.join(self.tokens["~"], ".android", "avd")
        try:
            names = os.listdir(root)
        except OSError:
            return
        for name in names:
            if not name.lower().endswith(".avd"):
                continue
            display, image = name[:-4].replace("_", " "), None
            try:
                with open(os.path.join(root, name, "config.ini"), encoding="utf-8", errors="replace") as f:
                    for line in f:
                        key, _, value = line.partition("=")
                        key = key.strip().lower()
                        if key == "avd.ini.displayname" and value.strip():
                            display = value.strip()
                        elif key == "image.sysdir.1":
                            image = value.strip().replace("/", "\\").strip("\\").lower()
            except OSError:
                pass
            self.avd_names[name.lower()] = display
            if image:
                self.avd_images.setdefault(image, []).append(display)

    def read_sync_roots(self):
        try:
            base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                  r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\SyncRootManager")
        except OSError:
            return
        with base:
            for name in list(subkeys(base)):
                provider = name.split("!")[0]
                provider = SYNC_PROVIDERS.get(provider.lower(), provider)
                try:
                    with winreg.OpenKey(base, name + r"\UserSyncRoots") as k:
                        for path in values(k).values():
                            if isinstance(path, str) and path:
                                self.sync_roots.append((path.rstrip("\\"), provider))
                except OSError:
                    continue

    def read_restore_points(self):
        out = run_powershell(
            "$v=(Get-CimInstance Win32_Volume -Filter \"DriveLetter='%s'\").DeviceID;"
            "$t=0;Get-CimInstance Win32_ShadowStorage|ForEach-Object{if(-not $v -or $_.Volume.DeviceID -eq $v)"
            "{$t+=$_.UsedSpace}};$t" % self.drive, timeout=60)
        try:
            self.restore_points = int(float(out)) if out else None
        except ValueError:
            self.restore_points = None

    def sync_root_of(self, path):
        low = path.lower()
        for root, provider in self.sync_roots:
            r = root.lower()
            if low == r or low.startswith(r + "\\"):
                return root, provider
        return None


def pretty_package(name):
    """'5319275A.WhatsAppDesktop' -> 'WhatsAppDesktop', 'MicrosoftWindows.Speech.en-US.1' -> 'Speech en-US'."""
    parts = name.split(".")
    if len(parts) > 1:
        parts = parts[1:]
    while len(parts) > 1 and parts[-1].isdigit():
        parts.pop()
    return " ".join(parts) or name


class AppIndex:
    """Finds which installed app a folder name belongs to."""

    def __init__(self):
        self.apps = []

    def add(self, display, publisher):
        clean = clean_app_name(display)
        w = words(clean)
        initials = "".join(x[0] for x in w) if len(w) >= 3 else ""
        self.apps.append((norm(clean), w, words(publisher), canon_group(display, True), display, initials))

    def has(self, word):
        w = norm(word)
        return any(w in a[0] for a in self.apps)

    def match(self, folder):
        """Returns (group, [matching app names]) or (None, [])."""
        f, fw = norm(folder), words(folder)
        if len(f) < 3:
            return None, []
        best, found = 0, {}
        for n, w, pw, group, display, initials in self.apps:
            if not n:
                continue
            if f == n:
                score = 100
            elif len(f) >= 4 and n.startswith(f):
                score = 85
            elif len(n) >= 5 and f.startswith(n):
                score = 80
            elif fw and w and fw[0] == w[0] and len(fw[0]) >= 4:
                score = 70
            elif initials and f == initials:
                score = 65                                   # IDM -> Internet Download Manager
            elif fw and pw and fw[0] == pw[0] and len(fw[0]) >= 4:
                score = 60
            elif w and len(w[0]) >= 4 and f.startswith(w[0]):
                score = 55                                   # Anki2 -> Anki Launcher
            elif len(f) >= 7 and f in n:
                score = 50
            elif len(n) >= 5 and n in f:
                score = 45                                   # com.bilingify.readest -> Readest
            else:
                continue
            if score > best:
                best, found = score, {group: display}
            elif score == best:
                found.setdefault(group, display)
        if not found:
            return None, []
        if len(found) == 1:
            return next(iter(found)), list(found.values())
        return pretty_folder(folder), sorted(found.values())


# ---------------------------------------------------------------------------------------- scanning

class Node:
    __slots__ = ("name", "size", "files", "cloud", "kids", "mtime", "marks", "isfile")

    def __init__(self, name, mtime=0.0, isfile=False):
        self.name = name
        self.size = 0
        self.files = 0
        self.cloud = 0
        self.kids = None
        self.mtime = mtime
        self.marks = 0
        self.isfile = isfile

    def kid(self, name):
        return self.kids.get(name.lower()) if self.kids else None


class Scanner:
    """Walks the drive once and keeps a tree of every folder of 2 MB or more."""

    def __init__(self, cluster, stat_roots, windows_dir):
        self.cluster = cluster
        self.stats = {p.lower(): [{}, [0, 0, 0, 0]] for p in stat_roots}   # per your-files folder: types, ages
        self.windows_dir = windows_dir.lower()
        self.big_files = []      # (path, size on disk, size, modified)
        self.windows_files = []  # (path, size on disk, chain of folders) for the hard-link check
        self.denied = []
        self.files = 0
        self.dirs = 0
        self.stack = []
        self.now = time.time()
        self.started = time.time()
        self.next_report = 0.0
        self.quiet = False

    def report(self, final=False):
        now = time.time()
        if self.quiet:
            return
        if final or now >= self.next_report:
            self.next_report = now + 0.5
            say("\r  Scanning... %s files in %s folders (%d s)   " % (format(self.files, ","), format(self.dirs, ","),
                                                                     now - self.started), end="\n" if final else "")

    def scan(self, path, node, acc, in_windows):
        self.dirs += 1
        try:
            entries = os.scandir(LONG + path)
        except OSError:
            self.denied.append(path)
            return
        self.stack.append(node)
        base = path if path.endswith("\\") else path + "\\"
        chain = tuple(self.stack) if in_windows else None
        subdirs, kids = [], []
        size = files = cloud = marks = 0
        c = self.cluster
        with entries:
            for e in entries:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                attrs = st.st_file_attributes
                if attrs & REPARSE and st.st_reparse_tag & NAME_SURROGATE:
                    continue   # junction or symlink: what it points to is counted where it really is
                if attrs & DIRECTORY:
                    subdirs.append((e.name, st.st_mtime))
                    continue
                if attrs & CLOUD_ONLY:
                    cloud += st.st_size
                    continue
                n = st.st_size
                d = 0 if n < 800 else -(-n // c) * c   # size on disk; tiny files live in the file table
                size += d
                files += 1
                name = e.name
                if name in MARKER_NAMES:
                    marks |= MARKER_NAMES[name]
                elif name.endswith("proj") and os.path.splitext(name.lower())[1] in PROJECT_EXTS:
                    marks |= K.MARK_DOTNET
                if acc is not None:
                    low = name.lower()
                    dot = low.rfind(".")
                    kind = TYPE_OF.get(low[dot + 1:], "Other") if dot > 0 else "Other"
                    rec = acc[0].get(kind)
                    if rec is None:
                        acc[0][kind] = [d, 1]
                    else:
                        rec[0] += d
                        rec[1] += 1
                    age = self.now - st.st_mtime
                    acc[1][0 if age < AGE_LIMITS[0] else 1 if age < AGE_LIMITS[1] else 2 if age < AGE_LIMITS[2] else 3] += d
                if d >= BIG_FILE:
                    if not in_windows:
                        self.big_files.append((base + name, d, n, st.st_mtime))
                    if d >= FILE_NODE:
                        f = Node(name, st.st_mtime, True)
                        f.size, f.files = d, 1
                        kids.append(f)
                if chain is not None:
                    self.windows_files.append((base + name, d, chain))
        self.files += files
        node.size, node.files, node.cloud, node.marks = size, files, cloud, marks
        for name, mtime in subdirs:
            child = Node(name, mtime)
            cpath = base + name
            low = cpath.lower()
            self.scan(cpath, child, self.stats.get(low, acc), in_windows or low == self.windows_dir)
            node.size += child.size
            node.files += child.files
            node.cloud += child.cloud
            if child.size >= KEEP_NODE or child.cloud >= KEEP_NODE:
                kids.append(child)
        if kids:
            node.kids = {k.name.lower(): k for k in kids}
        self.stack.pop()
        self.report()

    def dedupe_windows(self):
        """Count Windows files that are stored once but appear in two folders (hard links) only once."""
        files = self.windows_files
        sxs = self.windows_dir + "\\winsxs\\"
        files.sort(key=lambda f: f[0].lower().startswith(sxs))   # keep the System32 copy, drop the WinSxS one

        def link_id(path):
            try:
                s = os.stat(LONG + path, follow_symlinks=False)
            except OSError:
                return None
            return s.st_ino if s.st_nlink > 1 else None

        seen, saved = set(), 0
        with ThreadPoolExecutor(max_workers=16) as pool:
            for (path, d, chain), ino in zip(files, pool.map(link_id, [f[0] for f in files], chunksize=512)):
                if ino is None:
                    continue
                if ino in seen:
                    for n in chain:
                        n.size -= d
                        n.files -= 1
                    saved += d
                else:
                    seen.add(ino)
        self.windows_files = []
        return saved

    def graft(self, tree, drive, path):
        """Scan a folder whose parent can't be listed (Store apps in WindowsApps) and hang it in the tree."""
        parts = path[len(drive):].strip("\\").split("\\")
        chain, node = [tree], tree
        for part in parts[:-1]:
            k = node.kid(part)
            if k is None:
                k = Node(part)
                node.kids = node.kids or {}
                node.kids[part.lower()] = k
            chain.append(k)
            node = k
        if node.kid(parts[-1]) is not None:
            return
        leaf = Node(parts[-1])
        self.scan(path, leaf, None, False)
        node.kids = node.kids or {}
        node.kids[parts[-1].lower()] = leaf
        for n in chain:
            n.size += leaf.size
            n.files += leaf.files
            n.cloud += leaf.cloud


def run_with_big_stack(fn):
    """Folders can nest deeply; recurse in a thread with room for it."""
    sys.setrecursionlimit(20000)
    threading.stack_size(128 * MB)   # Windows allows less than 256 MB
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:   # re-raised in the caller
            box["error"] = exc

    t = threading.Thread(target=target)
    t.start()
    t.join()
    threading.stack_size(0)
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------------------- classifying

class Item:
    __slots__ = ("id", "name", "cat", "safety", "desc", "action", "group", "paths", "nodes", "size", "files",
                 "cloud", "notes", "flags", "container", "kind", "details")

    def __init__(self, ident, name, cat, safety, desc, action=None, group=None):
        self.id = ident
        self.name = name
        self.cat = cat
        self.safety = safety
        self.desc = desc
        self.action = action
        self.group = group
        self.paths = []
        self.nodes = []
        self.size = 0
        self.files = 0
        self.cloud = 0
        self.notes = []
        self.flags = set()
        self.container = False
        self.kind = None
        self.details = {}


PLACES = {"local": "Local data (caches, databases, downloads)", "roaming": "Settings and data",
          "locallow": "Data", "programdata": "Data shared by all users"}


class Classifier:
    def __init__(self, tree, drive, tokens, info, scanner):
        self.tree = tree
        self.drive = drive
        self.tokens = tokens
        self.info = info
        self.scanner = scanner
        self.items = []
        self.claims = {}
        self.merged = {}
        self.ide_dirs = defaultdict(list)
        self.container_keys = set()

    # -- paths
    def resolve(self, pattern):
        for token in sorted(self.tokens, key=len, reverse=True):
            if pattern.startswith(token):
                value = self.tokens[token]
                return value.rstrip("\\") + pattern[len(token):] if value else None
        return pattern

    def node_at(self, path):
        if not path.lower().startswith(self.drive.lower()):
            return None
        node = self.tree
        for part in path[len(self.drive):].strip("\\").split("\\"):
            if part:
                node = node.kid(part)
                if node is None:
                    return None
        return node

    def match(self, pattern):
        path = self.resolve(pattern)
        if not path or not path.lower().startswith(self.drive.lower()):
            return []
        found = [(self.tree, self.drive, [])]
        for part in path[len(self.drive):].strip("\\").split("\\"):
            if not part:
                continue
            low, wild, nxt = part.lower(), "*" in part or "?" in part, []
            for node, p, caps in found:
                if not node.kids:
                    continue
                if wild:
                    for k in node.kids.values():
                        if fnmatch.fnmatchcase(k.name.lower(), low):
                            nxt.append((k, p + "\\" + k.name, caps + [k.name]))
                else:
                    k = node.kids.get(low)
                    if k is not None:
                        nxt.append((k, p + "\\" + k.name, caps))
            found = nxt
            if not found:
                break
        return found

    def is_linked(self, path):
        key = path.lower()
        return any(d == key or d.startswith(key + "\\") or key.startswith(d + "\\") for d in self.info.linked_dirs)

    def owner(self, path):
        key = path.lower()
        while True:
            item = self.claims.get(key)
            if item is not None:
                return item
            cut = key.rfind("\\")
            if cut <= 0:
                return self.claims.get(self.drive.lower())
            key = key[:cut]

    # -- items
    def new(self, name, cat, safety, desc, action=None, group=None):
        item = Item(len(self.items), name, cat, safety, desc, action, group)
        self.items.append(item)
        return item

    def claim(self, path, node, item):
        self.claims[path.lower()] = item
        item.paths.append(path)
        item.nodes.append(node)

    def group_for(self, raw, caps):
        """Resolve a rule's group: plain name, '@match:folder' or '@pkg:family'. Returns (group, app name)."""
        if not raw:
            return None, None
        raw = fill(raw, caps)
        if raw.startswith("@match:"):
            folder = raw[7:]
            group, _ = self.info.apps.match(folder)
            group = group or pretty_folder(folder)
            return group, group
        if raw.startswith("@pkg:"):
            name = self.info.package_name(raw[5:])
            return canon_group(name, True), name
        return canon_group(raw), canon_group(raw)

    # -- passes
    def run(self):
        self.claim_containers()
        for rule in K.RULES:
            self.apply(rule)
        self.finish_ide_versions()
        self.claim_registry()
        self.claim_packages()
        self.auto_items()
        self.find_artifacts()
        self.assign()

    def claim_containers(self):
        for pattern, kind, name, cat, safety, desc in K.CONTAINERS:
            path = self.resolve(pattern)
            node = self.node_at(path) if path else None
            if node is None or path.lower() in self.claims:
                continue
            item = self.new(name, cat, safety, desc)
            item.container = True
            item.kind = kind
            self.claim(path if path != self.drive else self.drive + "\\", node, item)
            self.claims[path.rstrip("\\").lower()] = item
            self.container_keys.add(path.rstrip("\\").lower())

    def apply(self, rule):
        for node, path, caps in self.match(rule["path"]):
            key = path.lower()
            if key in self.claims:
                continue
            if rule["cat"] == K.CACHE:
                parent = self.owner(path)
                if parent is not None and not parent.container and parent.cat == K.CACHE:
                    continue   # already inside a cache: don't carve a cache out of a cache
            group, app = self.group_for(rule["group"], caps)
            name = fill(rule["name"], caps, app or "")
            if rule["merge"]:
                mkey = fill(rule["merge"], caps)
                item = self.merged.get(mkey)
                if item is None:
                    item = self.new(name, rule["cat"], rule["safety"], fill(rule["desc"], caps, app),
                                    fill(rule["action"], caps, app), group)
                    self.merged[mkey] = item
                    item.kind = "merged"
            else:
                item = self.new(name, rule["cat"], rule["safety"], fill(rule["desc"], caps, app),
                                fill(rule["action"], caps, app), group)
            self.claim(path, node, item)
            if rule["split"]:
                item.flags.add("split")
            if rule["special"]:
                getattr(self, "special_" + rule["special"])(item, node, path, caps)
            if rule["needs"] and not self.info.apps.has(rule["needs"]):
                item.notes.append("%s doesn't seem to be installed any more, so this is probably left over."
                                  % (group or rule["needs"]))
                item.flags.add("leftover")
                item.safety = K.OPTIONAL
                item.action = "Delete it if you no longer use %s." % (group or rule["needs"])

    # -- special rules
    def special_python(self, item, node, path, caps):
        m = re.match(r"python(\d)(\d+)", node.name, re.I)
        item.name = "Python %s.%s" % (m.group(1), m.group(2)) if m else "Python (" + node.name + ")"
        site = node.kid("Lib")
        site = site.kid("site-packages") if site else None
        if site and site.kids:
            biggest = sorted((k for k in site.kids.values() if not k.isfile and not k.name.endswith("-info")),
                             key=lambda k: -k.size)[:6]
            if biggest:
                item.notes.append("Biggest installed packages: " +
                                  ", ".join("%s (%s)" % (k.name, human(k.size)) for k in biggest) + ".")
        if path.lower() not in self.info.pythons:
            item.safety = K.OPTIONAL
            item.flags.add("leftover")
            item.notes.append("Windows doesn't list this Python as installed (the py launcher doesn't see it). "
                              "It's probably left over from an uninstall.")
            item.action = "Check that nothing you use runs from this folder, then delete it."

    def special_android_image(self, item, node, path, caps):
        api, tag, abi = (caps + ["", "", ""])[:3]
        tags = {"google_apis": "Google APIs", "google_apis_playstore": "Google Play", "default": "AOSP"}
        label = tags.get(tag.replace("_ps16k", ""), tag)
        if tag.endswith("_ps16k"):
            label += ", 16 KB pages"
        item.name = "Emulator image: %s (%s)" % (api.replace("android-", "Android "), label)
        users = self.info.avd_images.get(("system-images\\%s\\%s\\%s" % (api, tag, abi)).lower())
        if users:
            item.notes.append("Used by your emulator: %s." % ", ".join(users))
        else:
            item.notes.append("None of your emulators uses this image.")
            item.safety = K.SAFE
            item.flags.add("unused")

    def special_avd(self, item, node, path, caps):
        folder = caps[0]
        display = self.info.avd_names.get(folder.lower(), folder[:-4].replace("_", " "))
        item.name = item.name.replace(folder, display)

    def special_updater(self, item, node, path, caps):
        base = node.name[:-len("-updater")]
        group, apps = self.info.apps.match(base)
        item.group = group or pretty_folder(base)
        item.name = "%s update downloads" % item.group
        if not group:
            item.notes.append("No installed app matches '%s': this is probably left over." % base)
            item.flags.add("leftover")

    def special_ide_versions(self, item, node, path, caps):
        m = re.match(r"(.*?)(\d{4}(?:\.\d+)+)$", node.name)
        if m:
            family = (path.lower().rsplit("\\", 1)[0], m.group(1).lower())
            self.ide_dirs[family].append((tuple(int(x) for x in m.group(2).split(".")), item))
            item.name = item.name.replace(node.name, m.group(2))

    def finish_ide_versions(self):
        for (_, family), found in self.ide_dirs.items():
            if len(found) < 2:
                continue
            found.sort(key=lambda f: f[0])
            for _, item in found[:-1]:
                item.flags.add("leftover")
                item.notes.append("This is from an older version you no longer use.")
                item.safety = K.SAFE
                item.action = "Delete the folder: the current version keeps its own copy."

    # -- installed apps
    def claim_registry(self):
        for e in self.info.uninstall:
            d = e["dir"]
            if not d or d.lower() in self.claims or d.lower() in self.container_keys:
                continue
            node = self.node_at(d)
            if node is None:
                continue
            inside = self.owner(d)
            if inside is not None and not inside.container and "split" not in inside.flags:
                continue   # part of a folder the catalog already explains as a whole
            group, cat, safety, desc = alias(clean_app_name(e["name"]))
            safety = safety or K.UNINSTALL
            desc = desc or ("Installed program" + (" from " + e["publisher"] if e["publisher"] else "") + ".")
            action = ("Settings → Apps → Installed apps → %s → ⋯ → Uninstall." % e["name"]) \
                if safety in (K.UNINSTALL, K.OPTIONAL) else None
            self.claim(d, node, self.new(e["name"], cat or K.APP, safety, desc, action, group))

    def claim_packages(self):
        win = self.tokens["%WIN%"].lower()
        frameworks = system_apps = addons = None
        for p in self.info.packages:
            loc = (p.get("l") or "").rstrip("\\")
            if not loc or not loc.lower().startswith(self.drive.lower()) or loc.lower().startswith(win):
                continue
            if loc.lower() in self.claims:
                continue
            node = self.node_at(loc)
            if node is None:
                continue
            pfn = p.get("f") or ""
            name = self.info.package_name(pfn)
            microsoft = pfn.lower().endswith(("_8wekyb3d8bbwe", "_cw5n1h2txyewy"))
            if microsoft and not p.get("fw") and pfn not in self.info.package_names:
                if addons is None:
                    addons = self.new("Windows add-ons (language, speech, codecs…)", K.SYSTEM, K.KEEP,
                                      "Language packs, speech packs, video codecs, widgets and other Windows "
                                      "components installed through the Microsoft Store.",
                                      "Remove languages you don't use in Settings → Time & language → Language & region.",
                                      "Windows")
                    addons.kind = "merged"
                self.claim(loc, node, addons)
                continue
            if p.get("fw"):
                if frameworks is None:
                    frameworks = self.new("Store app frameworks & runtimes", K.SYSTEM, K.KEEP,
                                          "Shared libraries (Visual C++, .NET, WinUI…) that Store apps need.",
                                          "Windows removes them when no app needs them any more.", "Windows")
                    frameworks.kind = "merged"
                self.claim(loc, node, frameworks)
            elif p.get("s") == "System" or p.get("nr"):
                if system_apps is None:
                    system_apps = self.new("Built-in Windows apps", K.SYSTEM, K.KEEP,
                                           "Apps that come with Windows and can't be uninstalled normally.", None,
                                           "Windows")
                    system_apps.kind = "merged"
                self.claim(loc, node, system_apps)
            else:
                group = canon_group(name, True)
                self.claim(loc, node, self.new(name, K.APP, K.UNINSTALL, "Microsoft Store app.",
                                               "Settings → Apps → Installed apps → %s → ⋯ → Uninstall." % name, group))

    # -- everything else in the containers
    def auto_items(self):
        for pattern, kind, *_ in K.CONTAINERS:
            cpath = self.resolve(pattern)
            node = self.node_at(cpath) if cpath else None
            if node is None or not node.kids:
                continue
            for k in list(node.kids.values()):
                path = cpath.rstrip("\\") + "\\" + k.name
                if path.lower() not in self.claims:
                    self.auto(kind, k, path)

    def auto(self, kind, node, path):
        name = node.name
        if node.isfile:
            cat = {"root": K.OTHER, "profile": K.USER, "windows": K.SYSTEM}.get(kind, K.APPDATA)
            safety = K.REVIEW if cat == K.USER else K.UNKNOWN if cat != K.SYSTEM else K.KEEP
            self.claim(path, node, self.new(name, cat, safety, "A large file directly in this folder."))
            return
        if kind == "root":
            if self.is_linked(path):
                group, _ = self.info.apps.match(name)
                item = self.new(name, K.APP, K.UNINSTALL, "Contains files of an installed app.", None, group)
            else:
                item = self.new(name, K.OTHER, K.UNKNOWN,
                                "A folder at the top of C:. It isn't part of Windows and no installed app points to "
                                "it, so it was probably made by you, by a portable tool or by a driver installer.",
                                "Open it to see what it is. Delete it only if you know you don't need it.")
        elif kind == "users":
            item = self.new("Account: " + name, K.OTHER, K.REVIEW,
                            "Files of another account on this PC. Only that account or an administrator can see "
                            "inside, so the size may be incomplete.", "Sign in to that account to clean it.")
        elif kind == "profile":
            if name.startswith("."):
                group, apps = self.info.apps.match(name.lstrip("."))
                item = self.new(name, K.APPDATA, K.KEEP if group else K.UNKNOWN,
                                "Hidden settings or data folder of %s." % (group or "a tool or app"),
                                None if group else "Search the folder name to find which tool uses it.", group)
            else:
                item = self.new(name, K.USER, K.REVIEW, "A folder in your user profile.",
                                "Open it and delete what you no longer need.")
        elif kind in PLACES or kind == "appdata":
            group, apps = self.info.apps.match(name)
            place = PLACES.get(kind, "Data")
            if group:
                item = self.new(name, K.APPDATA, K.UNINSTALL, "%s of %s." % (place, group),
                                "To remove it, uninstall %s (if the folder stays afterwards, delete it)." % group, group)
                if len(apps) > 1:
                    more = " and %d more" % (len(apps) - 4) if len(apps) > 4 else ""
                    item.notes.append("Used by several apps: " + ", ".join(apps[:4]) + more + ".")
            else:
                item = self.new(name, K.APPDATA, K.UNKNOWN, "%s of an app or tool called '%s'." % (place, name),
                                "Search the folder name to see which program uses it.")
                if node.size >= NOTE_MIN:
                    item.flags.add("nomatch")
                    item.notes.append("No installed app has a similar name. It may be left over from an app you "
                                      "removed, or belong to a tool without an installer.")
        elif kind == "packages":
            display = self.info.package_name(name)
            group = canon_group(display, True)
            if name.lower() in self.info.pfns:
                item = self.new(display + " (app data)", K.APPDATA, K.UNINSTALL,
                                "Data of the Microsoft Store app %s." % display,
                                "Settings → Apps → Installed apps → %s → ⋯ → Advanced options: 'Reset' empties it, "
                                "'Uninstall' removes it." % display, group)
            else:
                item = self.new(display + " (app data)", K.APPDATA, K.OPTIONAL,
                                "Data of the Microsoft Store app %s." % display, "Delete the folder.", group)
                item.flags.add("leftover")
                item.notes.append("This app isn't installed any more: this is leftover data.")
        elif kind in ("programs", "pf"):
            item = self.program_folder(node, path)
        elif kind == "shared":
            group, _ = self.info.apps.match(name)
            item = self.new(name, K.APP, K.KEEP, "Components shared by %s." % (group or "installed programs"), None,
                            group)
        elif kind == "windowsapps":
            item = self.new(name, K.APP, K.KEEP, "Files of a Microsoft Store app.")
        else:
            item = self.new(name, K.SYSTEM, K.KEEP, "Part of Windows.")
        self.claim(path, node, item)

    def program_folder(self, node, path):
        name = node.name
        if self.is_linked(path):
            inside = [e["name"] for e in self.info.uninstall if e["dir"] and e["dir"].lower().startswith(path.lower() + "\\")]
            groups = {canon_group(n, True) for n in inside}
            group = groups.pop() if len(groups) == 1 else self.info.apps.match(name)[0] or pretty_folder(name)
            item = self.new(name + ": other files", K.APP, K.UNINSTALL,
                            "Files in this folder that aren't part of one specific installed app: shared "
                            "components, or leftovers of older versions.",
                            "Uninstall the apps that live here to remove them.", group)
            self.split_old_copies(node, path, item)
            return item
        group, apps = self.info.apps.match(name)
        if group:
            return self.new(name, K.APP, K.UNINSTALL, "Program files of %s." % group,
                            "To remove it, uninstall %s in Settings → Apps." % group, group)
        item = self.new(name, K.APP, K.UNKNOWN, "A program folder.",
                        "Check what it is (look at the files and dates). If it belongs to an app you removed, delete "
                        "it (needs administrator).")
        item.flags.add("unlinked")
        item.notes.append("Windows' list of installed apps doesn't point to this folder. It may be left over from an "
                          "app you removed, or be a portable app you run directly from here.")
        return item

    def split_old_copies(self, node, path, parent):
        """In a folder like 'Program Files\\Android', find 'Android Studio1' next to the installed 'Android Studio2'."""
        if not node.kids:
            return
        linked = {}
        for k in node.kids.values():
            kpath = path + "\\" + k.name
            if self.is_linked(kpath):
                linked[re.sub(r"\d+$", "", norm(k.name))] = k.name
        for k in node.kids.values():
            kpath = path + "\\" + k.name
            if k.isfile or kpath.lower() in self.claims or self.is_linked(kpath) or k.size < 100 * MB:
                continue
            base = re.sub(r"\d+$", "", norm(k.name))
            if base in linked:
                item = self.new(node.name + "\\" + k.name, K.APP, K.OPTIONAL,
                                "Another copy of a program that is installed in '%s'." % linked[base],
                                "Check it isn't the copy you use (compare the dates), then delete it "
                                "(needs administrator).", parent.group)
                item.flags.add("leftover")
                item.notes.append("Windows' list of installed apps points to '%s', not to this folder: it looks "
                                  "like an older copy left behind by an update or reinstall." % linked[base])
                self.claim(kpath, k, item)

    # -- rebuildable project folders
    def find_artifacts(self):
        merged = {}
        for owner in [it for it in self.items if it.cat in (K.USER, K.OTHER) and not it.container]:
            for node, path in list(zip(owner.nodes, owner.paths)):
                stack = [(node, path)]
                while stack:
                    parent, ppath = stack.pop()
                    if not parent.kids:
                        continue
                    for k in parent.kids.values():
                        if k.isfile:
                            continue
                        kpath = ppath + "\\" + k.name
                        other = self.claims.get(kpath.lower())
                        if other is not None and other is not owner:
                            continue
                        spec = K.ARTIFACTS.get(k.name.lower())
                        if spec and self.artifact_ok(spec[1], parent, k):
                            kind = spec[0]
                            item = merged.get(kind)
                            if item is None:
                                name, cat, safety, desc, action = K.ARTIFACT_KINDS[kind]
                                item = merged[kind] = self.new(name, cat, safety, desc, action)
                                item.kind = "artifacts"
                            self.claim(kpath, k, item)
                        else:
                            stack.append((k, kpath))

    @staticmethod
    def artifact_ok(cond, parent, node):
        if cond is None:
            return True
        where, bits = cond
        return bool((parent.marks if where == "parent" else node.marks) & bits)

    # -- who owns every byte
    def assign(self):
        root = self.claims[self.drive.lower()]
        stack = [(self.tree, self.drive.lower(), root)]
        while stack:
            node, key, owner = stack.pop()
            item = self.claims.get(key, owner)
            size, files, cloud = node.size, node.files, node.cloud
            if node.kids:
                for k in node.kids.values():
                    size -= k.size
                    files -= k.files
                    cloud -= k.cloud
                    stack.append((k, key + "\\" + k.name.lower(), item))
            item.size += size
            item.files += files
            item.cloud += cloud


# ---------------------------------------------------------------------------------------- details

def top_level_nodes(item):
    """The item's folders, without folders nested inside another of its folders."""
    pairs = sorted(zip(item.paths, item.nodes), key=lambda p: len(p[0]))
    kept = []
    for path, node in pairs:
        low = path.lower()
        if not any(low.startswith(k.lower() + "\\") for k, _ in kept):
            kept.append((path, node))
    return kept


def add_details(cls, scanner, info):
    by_id = cls.items
    for item in cls.items:
        if item.container:
            continue
        tops = top_level_nodes(item)
        raw = sum(n.size for _, n in tops)
        details = item.details
        if raw - item.size >= MB:
            outside = []
            for path, node in tops:
                stack = [(node, path)]
                while stack:
                    n, p = stack.pop()
                    for k in (n.kids or {}).values():
                        kp = p + "\\" + k.name
                        other = cls.claims.get(kp.lower())
                        if other is not None and other is not item:
                            if k.size >= MB:
                                outside.append([kp, k.size, other.id])
                        else:
                            stack.append((k, kp))
            outside.sort(key=lambda r: -r[1])
            details["elsewhere"] = outside[:12]
            details["raw"] = raw
        if item.kind in ("merged", "artifacts") or len(tops) > 1:
            parts = [[p, n.size, n.mtime] for p, n in tops if n.size >= MB]
            parts.sort(key=lambda r: -r[1])
            details["parts"] = parts[:60]
            details["partCount"] = len(parts)
        elif item.size >= DETAIL_MIN and tops:
            path, node = tops[0]
            kids = sorted((node.kids or {}).values(), key=lambda k: -k.size)
            rows, shown = [], 0
            for k in kids[:14]:
                if k.size < MB:
                    break
                other = cls.claims.get((path + "\\" + k.name).lower())
                rows.append([k.name, k.size, 1 if k.isfile else 0, other.id if other is not None and other is not item
                             else None])
                shown += k.size
            if rows:
                rest = node.size - shown
                details["kids"] = rows
                if rest >= MB:
                    details["rest"] = rest
        stats = scanner.stats.get(item.paths[0].lower()) if item.paths else None
        if stats and item.cat == K.USER:
            details["types"] = sorted(([k, v[0], v[1]] for k, v in stats[0].items() if v[0] >= MB),
                                      key=lambda r: -r[1])
            details["ages"] = stats[1]
        if item.cloud >= NOTE_MIN:
            item.notes.append("It also holds %s of online-only files, which use no space on this PC." % human(item.cloud))
    # biggest files of each item
    best = defaultdict(list)
    for path, d, n, mtime in scanner.big_files:
        item = cls.owner(path)
        if item is not None and not item.container:
            best[item.id].append([path, d, mtime])
    for ident, files in best.items():
        files.sort(key=lambda r: -r[1])
        by_id[ident].details["big"] = files[:10]
    # synced folders
    for root, provider in info.sync_roots:
        item = cls.owner(root)
        if item is not None and item.cat == K.USER and root.lower() != (item.paths[0].lower() if item.paths else ""):
            item.notes.append("'%s' inside it is synced with %s." % (os.path.basename(root), provider))


def sample_hash(path, size, chunk=1024 * 1024):
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    try:
        with open(LONG + path, "rb") as f:
            for offset in (0, max(0, size // 2 - chunk // 2), max(0, size - chunk)):
                f.seek(offset)
                h.update(f.read(chunk))
    except OSError:
        return None
    return h.hexdigest()


def find_duplicates(cls, scanner, info):
    """Same-size files in your own folders whose start, middle and end are identical."""
    by_size = defaultdict(list)
    for path, d, n, mtime in scanner.big_files:
        item = cls.owner(path)
        if item is not None and item.cat in (K.USER, K.OTHER):
            by_size[n].append((path, d, mtime))
    groups = []
    for size, files in by_size.items():
        if len(files) < 2:
            continue
        by_hash = defaultdict(list)
        for path, d, mtime in files:
            h = sample_hash(path, size)
            if h:
                by_hash[h].append((path, d, mtime))
        for same in by_hash.values():
            if len(same) > 1:
                groups.append({"size": same[0][1], "copies": [[p, m, sync_label(info, p)] for p, _, m in same]})
    groups.sort(key=lambda g: -g["size"] * (len(g["copies"]) - 1))
    return groups[:100]


def sync_label(info, path):
    found = info.sync_root_of(path)
    return found[1] if found else None


def find_duplicate_folders(cls, info):
    """Folders with the same size, the same number of files and the same file list."""
    candidates = defaultdict(list)
    for owner in [it for it in cls.items if it.cat in (K.USER, K.OTHER) and not it.container]:
        for node, path in zip(owner.nodes, owner.paths):
            stack = [(node, path)]
            while stack:
                n, p = stack.pop()
                if n.size >= 100 * MB and n.files >= 3 and not n.isfile:
                    candidates[(n.size, n.files)].append(p)
                for k in (n.kids or {}).values():
                    kp = p + "\\" + k.name
                    if cls.claims.get(kp.lower(), owner) is owner:
                        stack.append((k, kp))
    groups = []
    for (size, files), paths in sorted(candidates.items(), key=lambda kv: -kv[0][0]):
        if len(paths) < 2 or files > 50000:
            continue
        paths = [p for p in paths if not any(o != p and (p.lower().startswith(o.lower() + "\\")
                                                         or o.lower().startswith(p.lower() + "\\")) for o in paths)]
        if len(paths) < 2:
            continue
        if any(any(p.lower().startswith(g.lower() + "\\") for g in done["paths"]) for done in groups for p in paths):
            continue
        listings = {}
        for p in paths:
            listing = set()
            for dp, _, fns in os.walk(LONG + p):
                rel = dp[len(LONG + p):].lower()
                for f in fns:
                    try:
                        listing.add((rel, f.lower(), os.stat(os.path.join(dp, f), follow_symlinks=False).st_size))
                    except OSError:
                        pass
            listings.setdefault(frozenset(listing), []).append(p)
        for same in listings.values():
            if len(same) > 1:
                groups.append({"size": size, "files": files, "paths": same,
                               "sync": [sync_label(info, p) for p in same]})
        if len(groups) >= 30:
            break
    return groups


# ---------------------------------------------------------------------------------------- output

def build_data(cls, scanner, info, usage, drive, seconds, dedupe_saved, dupes, dup_folders):
    total, used, free = usage
    counted = sum(it.size for it in cls.items)
    hidden_size = max(0, used - counted)
    restore = info.restore_points
    if restore:
        item = cls.claims.get((drive + "\\System Volume Information").lower())
        if item is not None:
            item.size += restore
            hidden_size = max(0, hidden_size - restore)
    hidden = cls.new("Space the scan can't see", K.OTHER, K.KEEP,
                     "Space in use that no folder accounts for: System Restore points (unless you run the report "
                     "as administrator), Store app files and folders Windows protects, the file table (MFT) and "
                     "other file-system data.",
                     "Run the report as administrator to measure restore points. Settings → System → About → "
                     "System protection → Configure lets you delete restore points.")
    hidden.size = hidden_size
    hidden.kind = "hidden"
    hidden.paths = []

    items = []
    small = defaultdict(lambda: [0, 0])
    for it in cls.items:
        if it.size < MIN_ITEM and it.kind != "hidden":
            small[it.cat][0] += it.size
            small[it.cat][1] += 1
            continue
        rec = {"id": it.id, "name": it.name, "cat": it.cat, "safety": it.safety, "size": it.size, "files": it.files,
               "desc": it.desc, "paths": it.paths[:1] if it.kind in ("merged", "artifacts") else it.paths[:5]}
        if it.action:
            rec["action"] = it.action
        if it.group:
            rec["group"] = it.group
        if it.notes:
            rec["notes"] = it.notes
        if it.flags:
            rec["flags"] = sorted(it.flags)
        if it.container:
            rec["container"] = True
        if it.kind in ("merged", "artifacts", "hidden"):
            rec["kind"] = it.kind
        rec.update(it.details)
        items.append(rec)
    return {
        "meta": {
            "version": VERSION, "drive": drive, "total": total, "used": used, "free": free,
            "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "seconds": round(seconds),
            "files": scanner.files, "folders": scanner.dirs, "denied": len(scanner.denied), "admin": info.admin,
            "cloud": cls.tree.cloud, "hardlinks": dedupe_saved, "restorePoints": restore,
            "cluster": scanner.cluster,
            "syncRoots": [[p, prov] for p, prov in info.sync_roots if p.lower().startswith(drive.lower())],
        },
        "items": items,
        "small": {cat: {"size": v[0], "count": v[1]} for cat, v in small.items()},
        "dupes": dupes,
        "dupFolders": dup_folders,
    }


def write_report(data, out_dir, drive):
    with open(os.path.join(HERE, "report_template.html"), encoding="utf-8") as f:
        template = f.read()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = template.replace("__REPORT_TITLE__", "Disk report – %s" % drive).replace("__REPORT_DATA__", payload)
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    path = os.path.join(out_dir, "disk-report-%s-%s.html" % (drive.rstrip(":"), stamp))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def main():
    parser = argparse.ArgumentParser(description="Explain what uses the space on a Windows drive.")
    parser.add_argument("--drive", default=os.environ.get("SystemDrive", "C:"), help="drive to scan (default: C)")
    parser.add_argument("--out", default=os.path.join(HERE, "reports"), help="folder for the report")
    parser.add_argument("--no-open", action="store_true", help="don't open the report when it's ready")
    parser.add_argument("--json", metavar="FILE", help="also save the raw results as JSON")
    args = parser.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass

    drive = args.drive.strip().rstrip(":\\/").upper() + ":"
    root = drive + "\\"
    if not os.path.isdir(root):
        say("Drive %s not found." % drive)
        return 1
    started = time.time()
    say("Disk report for %s%s" % (root, " (administrator)" if is_admin() else ""))

    tokens = path_tokens(drive)
    info = SysInfo(drive, tokens)
    say("  Reading the list of installed apps...")
    info.load()
    say("  %d installed programs, %d Store apps" % (len(info.uninstall), len(info.packages)))

    usage = shutil.disk_usage(root)
    stat_roots = [tokens[t] for t in ("%DOCUMENTS%", "%DESKTOP%", "%DOWNLOADS%", "%PICTURES%", "%VIDEOS%", "%MUSIC%")
                  if tokens.get(t)]
    scanner = Scanner(cluster_size(root), stat_roots, tokens["%WIN%"])
    tree = Node(drive)
    run_with_big_stack(lambda: scanner.scan(root, tree, None, False))
    scanner.report(final=True)

    say("  Checking Windows files that are stored once but appear twice...")
    saved = scanner.dedupe_windows()
    say("  %s of Windows files counted once instead of twice" % human(saved))

    windows_apps = (tokens["%PF%"] + "\\WindowsApps").lower()
    store = [p["l"].rstrip("\\") for p in info.packages if (p.get("l") or "").lower().startswith(windows_apps + "\\")]
    if store:
        say("  Measuring %d Store apps..." % len(store))
        scanner.quiet = True
        run_with_big_stack(lambda: [scanner.graft(tree, drive, p) for p in store])
        scanner.denied = [d for d in scanner.denied if d.lower() != windows_apps]

    say("  Explaining what was found...")
    cls = Classifier(tree, drive, tokens, info, scanner)
    cls.run()
    add_details(cls, scanner, info)
    say("  Looking for duplicate files...")
    dupes = find_duplicates(cls, scanner, info)
    dup_folders = find_duplicate_folders(cls, info)

    data = build_data(cls, scanner, info, (usage.total, usage.used, usage.free), drive, time.time() - started,
                      saved, dupes, dup_folders)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        if not os.path.exists(os.path.join(HERE, "report_template.html")):
            say("Saved %s" % args.json)
            return 0
    path = write_report(data, args.out, drive)
    say("Done in %d s. Report: %s" % (time.time() - started, path))
    if not args.no_open:
        os.startfile(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
