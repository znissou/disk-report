"""Tests for what the app does when you click: deleting, the guard that decides what may go, uninstall commands.

    py -m unittest discover tests

Everything happens in temporary folders, except read-only looks at the registry and the Recycle Bin. The app's own
data folder (scans, action log) is a temporary one too. Moving to the Recycle Bin is imitated, except with
DISK_REPORT_TEST_RECYCLE=1, which also sends one tiny temporary file to the real Recycle Bin.
"""
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import winreg

DATA = tempfile.mkdtemp(prefix="disk-report-test-data-")
os.environ["DISK_REPORT_DATA"] = DATA       # before app is imported: it reads it once
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import actions  # noqa: E402
import app  # noqa: E402
import build  # noqa: E402
import catalog  # noqa: E402
import disk_report  # noqa: E402

LONG = actions.LONG
NO_WINDOW = 0x08000000


def tearDownModule():
    shutil.rmtree(DATA, True)


def write(path, text="x" * 5000):
    os.makedirs(os.path.dirname(LONG + path), exist_ok=True)
    with open(LONG + path, "w") as f:
        f.write(text)


class DeleteForGood(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="disk-report-test-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_deletes_a_folder_tree(self):
        target = os.path.join(self.root, "cache")
        for name in ("a.bin", "sub\\b.bin", "sub\\deeper\\c.bin"):
            write(os.path.join(target, name))
        out = actions.delete_for_good(target, cluster=4096)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(out["files"], 3)
        self.assertEqual(out["freed"], 3 * 8192)          # 5000 bytes take two 4 KB clusters
        self.assertEqual((out["inUse"], out["denied"], out["errors"]), (0, 0, []))

    def test_read_only_files_go_too(self):
        target = os.path.join(self.root, "ro")
        write(os.path.join(target, "locked.txt"))
        subprocess.run(["attrib", "+R", os.path.join(target, "locked.txt")], check=True)
        actions.delete_for_good(target)
        self.assertFalse(os.path.exists(target))

    def test_contents_only_keeps_the_folder(self):
        target = os.path.join(self.root, "Temp")
        write(os.path.join(target, "one.tmp"))
        write(os.path.join(target, "nested\\two.tmp"))
        out = actions.delete_for_good(target, contents_only=True)
        self.assertTrue(os.path.isdir(target))
        self.assertEqual(os.listdir(target), [])
        self.assertEqual(out["files"], 2)

    def test_junctions_are_removed_not_followed(self):
        outside = os.path.join(self.root, "outside")
        write(os.path.join(outside, "precious.txt"))
        target = os.path.join(self.root, "victim")
        os.makedirs(target)
        subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(target, "link"), outside], check=True,
                       capture_output=True)
        actions.delete_for_good(target)
        self.assertFalse(os.path.exists(target))
        self.assertTrue(os.path.isfile(os.path.join(outside, "precious.txt")))

    def test_contents_only_refuses_a_link(self):
        outside = os.path.join(self.root, "outside")
        write(os.path.join(outside, "precious.txt"))
        link = os.path.join(self.root, "link")
        subprocess.run(["cmd", "/c", "mklink", "/J", link, outside], check=True, capture_output=True)
        out = actions.delete_for_good(link, contents_only=True)
        self.assertTrue(out["errors"])
        self.assertTrue(os.path.isfile(os.path.join(outside, "precious.txt")))

    def test_keeps_what_it_is_told_to_keep(self):
        target = os.path.join(self.root, "Tools")
        write(os.path.join(target, "old.zip"))
        write(os.path.join(target, "Driver\\driver.sys"))
        out = actions.delete_for_good(target, keep=[os.path.join(target, "Driver")])
        self.assertTrue(os.path.isfile(os.path.join(target, "Driver", "driver.sys")))
        self.assertFalse(os.path.exists(os.path.join(target, "old.zip")))
        self.assertEqual(out["kept"], 1)

    def test_files_in_use_are_skipped_and_counted(self):
        target = os.path.join(self.root, "busy")
        write(os.path.join(target, "open.log"))
        write(os.path.join(target, "free.log"))
        with open(os.path.join(target, "open.log")):
            out = actions.delete_for_good(target)
        self.assertEqual(out["inUse"], 1)
        self.assertEqual(out["files"], 1)
        self.assertTrue(os.path.isfile(os.path.join(target, "open.log")))

    def test_long_paths(self):
        target = os.path.join(self.root, "long")
        deep = os.path.join(target, *(["a" * 40] * 8), "file.txt")
        self.assertGreater(len(deep), 300)
        write(deep)
        out = actions.delete_for_good(target)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(out["files"], 1)

    def test_missing_path(self):
        self.assertTrue(actions.delete_for_good(os.path.join(self.root, "nope"))["missing"])

    def test_forward_slashes(self):
        target = os.path.join(self.root, "slashes")
        write(os.path.join(target, "a.bin"))
        out = actions.delete_for_good(target.replace("\\", "/"))
        self.assertFalse(os.path.exists(target))
        self.assertEqual(out["files"], 1)

    def test_leaves_what_it_is_told_to_leave(self):
        target = os.path.join(self.root, ".cache")
        write(os.path.join(target, "tool\\a.bin"))
        write(os.path.join(target, "huggingface\\model.safetensors"))
        out = actions.delete_for_good(target, contents_only=True, leave=[os.path.join(target, "huggingface")])
        self.assertTrue(os.path.isfile(os.path.join(target, "huggingface", "model.safetensors")))
        self.assertFalse(os.path.exists(os.path.join(target, "tool")))
        self.assertEqual((out["left"], out["kept"], out["files"]), (1, 0, 1))

    def test_stop(self):
        target = os.path.join(self.root, "big")
        for name in ("a\\1.bin", "b\\2.bin", "3.bin"):
            write(os.path.join(target, name))
        out = actions.delete_for_good(target, stop=lambda: True)
        self.assertTrue(out["stopped"])
        self.assertEqual(out["files"], 0)
        self.assertTrue(os.path.isfile(os.path.join(target, "3.bin")))

    def test_system_files(self):
        folder = os.path.join(self.root, "Boot")
        os.makedirs(folder)
        self.assertFalse(actions.is_system(folder))
        subprocess.run(["attrib", "+H", "+S", folder], check=True)
        self.addCleanup(subprocess.run, ["attrib", "-H", "-S", folder])
        self.assertTrue(actions.is_system(folder))


class GuardRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = cls.t = disk_report.path_tokens("C:")
        win, pf = t["%WIN%"], t["%PF%"]
        cls.items = {
            "root": {"name": "Files at the top of C:", "cat": "other", "safety": "keep", "container": True,
                     "paths": ["C:\\"]},
            "profile": {"name": "Your profile", "cat": "other", "safety": "keep", "container": True, "paths": [t["~"]]},
            "windows": {"name": "Windows", "cat": "system", "safety": "keep", "container": True, "paths": [win]},
            "downloads": {"name": "Downloads", "cat": "user", "safety": "review", "paths": [t["%DOWNLOADS%"]]},
            "documents": {"name": "Documents", "cat": "user", "safety": "review", "paths": [t["%DOCUMENTS%"]]},
            "npm": {"name": "npm cache", "cat": "cache", "safety": "safe", "paths": [t["%LOCAL%"] + "\\npm-cache"]},
            "temp": {"name": "Temporary files", "cat": "cache", "safety": "safe", "paths": [t["%LOCAL%"] + "\\Temp"]},
            "tools": {"name": "Tools", "cat": "other", "safety": "unknown", "paths": ["C:\\Tools"]},
            "driver": {"name": "Some driver", "cat": "system", "safety": "keep", "paths": ["C:\\Tools\\Driver"]},
            "app": {"name": "Still installed", "cat": "app", "safety": "uninstall", "group": "Installed App",
                    "paths": [pf + "\\InstalledApp"]},
            "gone": {"name": "Gone app data", "cat": "appdata", "safety": "uninstall", "group": "Gone App",
                     "paths": [t["%LOCAL%"] + "\\GoneApp"]},
            "leftover": {"name": "Old program", "cat": "app", "safety": "unknown", "paths": [pf + "\\OldProgram"]},
            "wu": {"name": "Windows Update downloads", "cat": "cache", "safety": "safe",
                   "paths": [win + "\\SoftwareDistribution\\Download"]},
            "node": {"name": "node_modules folders", "cat": "dev", "safety": "safe", "kind": "artifacts",
                     "paths": [t["%DOCUMENTS%"] + "\\p1\\node_modules"],
                     "targets": [t["%DOCUMENTS%"] + "\\p1\\node_modules", t["%DOCUMENTS%"] + "\\p2\\node_modules"]},
            "public": {"name": "Public folder", "cat": "other", "safety": "review", "paths": ["C:\\Users\\Public"]},
            "sara": {"name": "Account: Sara", "cat": "other", "safety": "review", "paths": ["C:\\Users\\Sara"]},
            "boot": {"name": "Boot", "cat": "other", "safety": "unknown", "paths": ["C:\\Boot"]},
            "cache": {"name": "Tool caches (.cache)", "cat": "cache", "safety": "safe", "size": 100,
                      "paths": [t["~"] + "\\.cache"]},
            "puppeteer": {"name": "Puppeteer browsers", "cat": "cache", "safety": "safe", "size": 600,
                          "paths": [t["~"] + "\\.cache\\puppeteer"]},
            "hf": {"name": "Hugging Face AI models", "cat": "cache", "safety": "optional", "size": 30,
                   "paths": [t["~"] + "\\.cache\\huggingface"]},
            "avd": {"name": "Android emulator: Pixel", "cat": "dev", "safety": "optional", "size": 3000,
                    "paths": [t["~"] + "\\.android\\avd\\Pixel.avd"]},
            "snapshot": {"name": "Quick Boot snapshot: Pixel", "cat": "cache", "safety": "safe", "size": 4000,
                         "raw": 4100, "paths": [t["~"] + "\\.android\\avd\\Pixel.avd\\snapshots"]},
        }
        scan = {"meta": {"drive": "C:", "syncRoots": []}, "items": list(cls.items.values()),
                "apps": {"Gone App": [{"name": "Gone App", "kind": "win32",
                                        "ref": {"hive": "HKCU", "view": 0, "key": "{disk-report-test-not-there}"}}]}}
        cls.guard = actions.Guard(scan, t)

    def ok(self, path, contents=False):
        return self.guard.check(path, contents)["ok"]

    def test_user_folders_themselves_are_never_deleted(self):
        self.assertFalse(self.ok(self.t["%DOWNLOADS%"]))
        self.assertFalse(self.ok(self.t["%DOCUMENTS%"]))
        self.assertFalse(self.ok(self.t["~"]))
        self.assertFalse(self.ok("C:\\"))

    def test_things_inside_your_folders_can_go(self):
        self.assertTrue(self.ok(self.t["%DOWNLOADS%"] + "\\ubuntu.iso"))
        self.assertTrue(self.ok(self.t["%DOCUMENTS%"] + "\\p2\\node_modules"))

    def test_only_caches_can_be_emptied(self):
        self.assertTrue(self.ok(self.t["%LOCAL%"] + "\\Temp", contents=True))
        self.assertFalse(self.ok(self.t["%LOCAL%"] + "\\Temp"))          # the Temp folder itself stays
        self.assertTrue(self.ok(self.t["%LOCAL%"] + "\\npm-cache", contents=True))
        self.assertFalse(self.ok(self.t["%DOWNLOADS%"] + "\\x", contents=True))

    def test_keep_and_containers_are_refused(self):
        self.assertFalse(self.ok("C:\\Tools\\Driver"))
        self.assertFalse(self.ok(self.t["%WIN%"] + "\\System32"))
        self.assertFalse(self.ok(self.t["~"] + "\\NTUSER.DAT"))

    def test_a_folder_holding_something_needed_keeps_it(self):
        verdict = self.guard.check("C:\\Tools")
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["keep"], ["c:\\tools\\driver"])
        self.assertEqual(verdict["keepNames"], ["Some driver"])

    def test_installed_apps_are_uninstalled_not_deleted(self):
        self.assertFalse(self.ok(self.t["%PF%"] + "\\InstalledApp"))
        self.assertIn("Uninstall", self.guard.check(self.t["%PF%"] + "\\InstalledApp")["reason"])
        self.assertTrue(self.ok(self.t["%LOCAL%"] + "\\GoneApp"))   # its uninstall entry no longer exists

    def test_windows_and_program_files_rules(self):
        self.assertTrue(self.ok(self.t["%WIN%"] + "\\SoftwareDistribution\\Download", contents=True))
        self.assertTrue(self.ok(self.t["%PF%"] + "\\OldProgram"))
        self.assertFalse(self.ok(self.t["%PF%"]))
        self.assertFalse(self.ok(self.t["%PF%"] + "\\WindowsApps\\Some.App_1.0_x64__abc"))

    def test_odd_paths_are_refused(self):
        for path in ("", "relative\\path", "D:\\elsewhere", "C:\\Tools\\..\\Windows", "\\\\server\\share\\x"):
            self.assertFalse(self.ok(path), path)

    def test_other_accounts_are_refused(self):
        for path in ("C:\\Users\\Sara", "C:\\Users\\Sara\\Downloads\\big.iso"):
            verdict = self.guard.check(path)
            self.assertFalse(verdict["ok"], path)
            self.assertIn("another account", verdict["reason"])
        self.assertIn("c:\\users\\sara", self.guard.never)       # so the page shows no Delete button for it,
        self.assertEqual(self.guard.locked, {"c:\\users\\sara"})  # nor for anything inside it

    def test_public_folders_themselves_stay(self):
        self.assertFalse(self.ok("C:\\Users\\Public"))
        self.assertFalse(self.ok("C:\\Users\\Public\\Documents"))
        self.assertTrue(self.ok("C:\\Users\\Public\\Downloads\\setup.exe"))

    def test_protected_system_files_at_the_top_of_the_drive(self):
        real = actions.is_system
        actions.is_system = lambda path: actions.norm(path) == "c:\\boot"
        self.addCleanup(setattr, actions, "is_system", real)
        verdict = self.guard.check("C:\\Boot")
        self.assertFalse(verdict["ok"])
        self.assertIn("protected operating system", verdict["reason"])
        self.assertTrue(self.ok("C:\\Tools"))

    def test_a_safe_cache_leaves_optional_things_inside_it(self):
        home = self.t["~"]
        verdict = self.guard.check(home + "\\.cache", True)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["also"], [actions.norm(home + "\\.cache\\puppeteer")])
        self.assertEqual(verdict["leave"], [actions.norm(home + "\\.cache\\huggingface")])
        self.assertEqual([(e["name"], e["size"]) for e in verdict["leaveItems"]], [("Hugging Face AI models", 30)])

    def test_an_optional_folder_takes_its_safe_parts(self):
        home = self.t["~"]
        verdict = self.guard.check(home + "\\.android\\avd\\Pixel.avd")
        self.assertEqual(verdict["also"], [actions.norm(home + "\\.android\\avd\\Pixel.avd\\snapshots")])
        self.assertEqual(verdict["leave"], [])
        self.assertEqual(verdict["alsoItems"][0]["size"], 4100)   # what's under its folder, not only its own share


class UninstallCommands(unittest.TestCase):
    def test_split_command(self):
        self.assertEqual(actions.split_command('"C:\\Program Files\\App\\unins000.exe" /SILENT'),
                         ("C:\\Program Files\\App\\unins000.exe", "/SILENT"))
        self.assertEqual(actions.split_command("C:\\Program Files\\App\\uninstall.exe /S"),
                         ("C:\\Program Files\\App\\uninstall.exe", "/S"))
        self.assertEqual(actions.split_command("C:\\Apps\\remove.exe"), ("C:\\Apps\\remove.exe", ""))

    def test_real_entries_resolve_to_a_program(self):
        """Read-only: every uninstall command Windows lists here resolves to msiexec or a file that exists."""
        checked = 0
        for hive, view, name, bits in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY, "HKLM", 64),
                                       (winreg.HKEY_CURRENT_USER, 0, "HKCU", 0)):
            try:
                root = winreg.OpenKey(hive, actions.UNINSTALL_KEY, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with root:
                for sub in list(disk_report.subkeys(root))[:80]:
                    ref = {"hive": name, "view": bits, "key": sub}
                    try:
                        program, params = actions.uninstall_command(ref)
                    except (OSError, LookupError):
                        continue
                    checked += 1
                    if program.lower() == "msiexec.exe":
                        self.assertRegex(params, r"^/x \{[0-9A-Fa-f-]{36}\}$")
                    elif not program.lower().endswith(("rundll32.exe", "cmd.exe")) and os.path.isabs(program):
                        self.assertTrue(os.path.exists(program) or not os.path.exists(os.path.dirname(program)),
                                        "%s -> %s" % (sub, program))
        self.assertGreater(checked, 0)


class Catalog(unittest.TestCase):
    def test_runnable_commands_are_written_in_the_catalog(self):
        with open(catalog.__file__, encoding="utf-8") as f:
            text = f.read()
        written = set(re.findall(r"`([^`]+)`", text))
        for command in catalog.COMMANDS:
            self.assertIn(command, written)
            self.assertNotRegex(command, r"[<>&|]")


def scan_of(items):
    """A scan of C: with these items, under the item that holds everything at the top of C:."""
    root = {"name": "Files at the top of C:", "cat": "other", "safety": "keep", "container": True, "paths": ["C:\\"]}
    return {"meta": {"drive": "C:", "syncRoots": [], "cluster": 4096}, "items": [root] + items, "apps": {}}


class AppDeleting(unittest.TestCase):
    """The app's delete(), with the Recycle Bin imitated: what it hands to Windows and what it says happened."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="disk-report-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.calls, self.cancel = [], False
        real = actions.recycle

        def recycle(paths, hwnd=None):
            self.calls.append(list(paths))
            if self.cancel:
                return {"ok": False, "cancelled": True}
            for p in paths:
                (shutil.rmtree if os.path.isdir(p) else os.remove)(p)
            return {"ok": True}

        actions.recycle = recycle
        self.addCleanup(setattr, actions, "recycle", real)

    def api(self, items):
        shutil.rmtree(app.SCANS_DIR, True)
        app.write_json(os.path.join(app.SCANS_DIR, "C-2026-01-01_000000.json"), scan_of(items))
        api = app.Api()
        api._hwnd = lambda: None
        return api

    @staticmethod
    def finish(api, started):
        assert started["ok"], started
        while True:
            view = api.task(started["task"])
            if view["done"]:
                return view["result"]
            time.sleep(0.02)

    def test_one_recycle_bin_operation_for_all_folders(self):
        folders = [os.path.join(self.root, "p%d" % i, "node_modules") for i in (1, 2, 3)]
        for f in folders:
            write(os.path.join(f, "a.js"))
        api = self.api([{"name": "node_modules folders", "cat": "dev", "safety": "safe", "kind": "artifacts",
                         "paths": folders[:1], "targets": folders}])
        result = self.finish(api, api.delete("C:", folders, "trash", False))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([p["state"] for p in result["paths"]], ["trashed"] * 3)

    def test_cancel_stops_all_of_it(self):
        folders = [os.path.join(self.root, "p%d" % i, "node_modules") for i in (1, 2)]
        for f in folders:
            write(os.path.join(f, "a.js"))
        api = self.api([{"name": "node_modules folders", "cat": "dev", "safety": "safe", "kind": "artifacts",
                         "paths": folders[:1], "targets": folders}])
        self.cancel = True
        result = self.finish(api, api.delete("C:", folders, "trash", False))
        self.assertTrue(result["cancelled"])
        self.assertEqual([p["state"] for p in result["paths"]], ["failed", "failed"])
        self.assertTrue(all(os.path.isdir(f) for f in folders))

    def test_a_cache_that_is_one_file_goes_to_the_recycle_bin(self):
        dump = os.path.join(self.root, "MEMORY.DMP")
        write(dump)
        api = self.api([{"name": "Crash dump (blue screen)", "cat": "cache", "safety": "safe", "paths": [dump]}])
        result = self.finish(api, api.delete("C:", [dump], "trash", True))
        self.assertEqual(self.calls, [[dump]])
        self.assertEqual(result["paths"][0]["state"], "trashed")
        self.assertFalse(os.path.exists(dump))

    def test_part_moved_is_partial(self):
        tools = os.path.join(self.root, "Tools")
        write(os.path.join(tools, "old.zip"))
        write(os.path.join(tools, "Driver", "driver.sys"))
        api = self.api([{"name": "Tools", "cat": "other", "safety": "unknown", "paths": [tools]},
                        {"name": "Some driver", "cat": "system", "safety": "keep",
                         "paths": [os.path.join(tools, "Driver")]}])
        result = self.finish(api, api.delete("C:", [tools], "trash", False))
        self.assertEqual(self.calls, [[os.path.join(tools, "old.zip")]])
        self.assertEqual((result["paths"][0]["state"], result["kept"]), ("partial", 1))
        self.assertEqual(result["untouched"], [actions.norm(os.path.join(tools, "Driver"))])

    def test_what_goes_with_a_folder_is_marked(self):
        avd = os.path.join(self.root, "Pixel.avd")
        write(os.path.join(avd, "userdata.img"))
        write(os.path.join(avd, "snapshots", "default_boot", "ram.img"))
        api = self.api([{"name": "Android emulator: Pixel", "cat": "dev", "safety": "optional", "paths": [avd]},
                        {"name": "Quick Boot snapshot: Pixel", "cat": "cache", "safety": "safe",
                         "paths": [os.path.join(avd, "snapshots")]}])
        check = api.check("C:", [avd], False)
        self.assertEqual([e["name"] for e in check["also"]], ["Quick Boot snapshot: Pixel"])
        result = self.finish(api, api.delete("C:", [avd], "forever", False))
        self.assertEqual(result["paths"][0]["state"], "deleted")
        self.assertEqual(result["inner"], [{"path": actions.norm(os.path.join(avd, "snapshots")), "state": "deleted"}])

    def test_what_was_already_deleted_is_not_marked_as_left_alone(self):
        cache = os.path.join(self.root, ".cache")
        models = os.path.join(cache, "huggingface")
        write(os.path.join(cache, "tool", "a.bin"))
        write(os.path.join(models, "model.safetensors"))
        api = self.api([{"name": "Tool caches (.cache)", "cat": "cache", "safety": "safe", "paths": [cache]},
                        {"name": "Hugging Face AI models", "cat": "cache", "safety": "optional", "paths": [models]}])
        result = self.finish(api, api.delete("C:", [cache], "forever", True))
        self.assertEqual(result["untouched"], [actions.norm(models)])      # still there: left alone
        self.finish(api, api.delete("C:", [models], "forever", False))     # deleted from its own row
        write(os.path.join(cache, "tool", "b.bin"))
        result = self.finish(api, api.delete("C:", [cache], "forever", True))
        self.assertEqual(result["untouched"], [])

    def test_unknown_ways_of_deleting_are_refused(self):
        junk = os.path.join(self.root, "junk")
        write(os.path.join(junk, "a.bin"))
        api = self.api([{"name": "junk", "cat": "other", "safety": "unknown", "paths": [junk]}])
        self.assertFalse(api.delete("C:", [junk], "recycle", False)["ok"])
        self.assertTrue(os.path.isdir(junk))

    def test_a_stopped_delete_says_so(self):
        junk = os.path.join(self.root, "junk")
        write(os.path.join(junk, "a.bin"))
        verdict = {"keep": [], "leave": [], "also": []}
        result = app.Api._delete_for_good([(junk, verdict)], False, 4096, {"stop": True, "progress": None})
        self.assertTrue(result["stopped"])
        self.assertEqual(result["paths"], [{"path": junk, "state": "failed"}])
        self.assertTrue(os.path.isfile(os.path.join(junk, "a.bin")))


class UninstallWait(unittest.TestCase):
    def test_follows_what_the_uninstaller_starts(self):
        # like NSIS and Inno Setup uninstallers: start a copy of yourself, then exit at once
        child = "import time; time.sleep(4)"
        parent = subprocess.Popen([sys.executable, "-c", "import subprocess, sys; subprocess.Popen([sys.executable, "
                                   "'-c', %r], creationflags=%d)" % (child, NO_WINDOW)], creationflags=NO_WINDOW)
        tree = actions.ProcessTree(int(parent._handle))
        while parent.poll() is None:          # the app looks every 0.2 s at first
            tree.alive()
            time.sleep(0.05)
        self.assertTrue(tree.alive())
        deadline = time.time() + 20
        while tree.alive() and time.time() < deadline:
            time.sleep(0.2)
        self.assertFalse(tree.alive())

    def wait_for(self, alive, registered, root=1):
        """Api._uninstall with an imitated uninstaller and clock: how the wait ends."""
        clock = [0.0]

        class Clock:
            @staticmethod
            def time():
                return clock[0]

            @staticmethod
            def sleep(seconds):
                clock[0] += seconds

        class Tree:
            def __init__(self, handle):
                self.root = root

            def alive(self):
                return alive(clock[0])

        for module, name, value in ((app, "time", Clock), (actions, "ProcessTree", Tree),
                                    (actions, "uninstall_command", lambda ref: ("uninstaller.exe", "")),
                                    (actions, "start", lambda *a, **k: None), (actions, "close", lambda h: None),
                                    (actions, "is_registered", lambda ref: registered(clock[0]))):
            self.addCleanup(setattr, module, name, getattr(module, name))
            setattr(module, name, value)
        task = {"stop": False, "progress": None}
        state = app.Api()._uninstall({"kind": "win32", "name": "X", "ref": {}}, task)
        return state[0], clock[0]

    def test_still_installed_once_the_uninstaller_closed(self):
        state, seconds = self.wait_for(alive=lambda t: t < 30, registered=lambda t: True)
        self.assertEqual(state, "kept")
        self.assertLess(seconds, 30 + app.UNINSTALL_GRACE + 5)

    def test_removed(self):
        self.assertEqual(self.wait_for(alive=lambda t: t < 30, registered=lambda t: t < 20)[0], "removed")

    def test_nothing_to_follow_waits_for_windows(self):
        # a link handed to an app that's already running (steam://uninstall/...): no process of its own to follow
        state, seconds = self.wait_for(alive=lambda t: False, registered=lambda t: t < 300, root=0)
        self.assertEqual(state, "removed")
        self.assertGreaterEqual(seconds, 300)


class ElevatedHelper(unittest.TestCase):
    """app.elevated(), the part that runs with administrator rights, run here without them on temporary files."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="disk-report-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.target = os.path.join(self.root, "Tools")
        write(os.path.join(self.target, "old.zip"))
        self.scan = os.path.join(self.root, "C-2026-01-01_000000.json")
        app.write_json(self.scan, scan_of([{"name": "Tools", "cat": "other", "safety": "unknown",
                                            "paths": [self.target]}]))
        self.result = os.path.join(os.path.realpath(self.root), "task.result.json")   # resolved, as the app does
        self.task = os.path.join(self.root, "task.json")
        self.digest = self.write_task(self.result)

    def write_task(self, result):
        app.write_json(self.task, {"scan": self.scan, "scanHash": app.sha256(self.scan),
                                   "tokens": disk_report.path_tokens("C:"), "own": [], "paths": [self.target],
                                   "contents": False, "cluster": 4096, "result": result})
        return app.sha256(self.task)

    def test_runs_the_task_it_was_given(self):
        self.assertEqual(app.elevated(self.task, self.digest), 0)
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(app.read_json(self.result)[0]["files"], 1)

    def test_refuses_a_changed_task_file(self):
        with open(self.task, encoding="utf-8") as f:
            task = json.load(f)
        task["paths"] = [os.path.join(self.root, "something else")]
        app.write_json(self.task, task)
        self.assertEqual(app.elevated(self.task, self.digest), app.REFUSED)
        self.assertEqual(app.elevated(self.task, ""), app.REFUSED)
        self.assertTrue(os.path.isfile(os.path.join(self.target, "old.zip")))
        self.assertFalse(os.path.exists(self.result))

    def test_refuses_a_changed_scan(self):
        with open(self.scan, "a", encoding="utf-8") as f:
            f.write(" ")
        self.assertEqual(app.elevated(self.task, self.digest), 0)
        self.assertIn("refused", app.read_json(self.result)[0])
        self.assertTrue(os.path.isfile(os.path.join(self.target, "old.zip")))

    def test_never_writes_through_a_folder_swapped_for_a_link(self):
        # the results folder, resolved by the app, was then replaced by a junction to somewhere else
        elsewhere = os.path.join(self.root, "elsewhere")
        os.makedirs(elsewhere)
        swapped = os.path.join(self.root, "results")
        subprocess.run(["cmd", "/c", "mklink", "/J", swapped, elsewhere], check=True, capture_output=True)
        digest = self.write_task(os.path.join(swapped, "task.result.json"))
        self.assertEqual(app.elevated(self.task, digest), app.UNREPORTED)
        self.assertEqual(os.listdir(elsewhere), [])                 # nothing written, nothing left there
        self.assertFalse(os.path.exists(self.target))               # the confirmed delete itself was done

    def test_the_app_reports_what_the_helper_could_not(self):
        api = AppDeleting.api(self, [{"name": "Tools", "cat": "other", "safety": "unknown", "paths": [self.target]}])
        for code, states in ((app.REFUSED, [{"path": self.target, "state": "failed"}]), (app.UNREPORTED, [])):
            self.addCleanup(setattr, actions, "start", actions.start)
            self.addCleanup(setattr, actions, "wait", actions.wait)
            actions.start = lambda *a, **k: None
            actions.wait = lambda handle, seconds=None, code=code: code
            result = AppDeleting.finish(api, api.delete_as_admin("C:", [self.target], False))
            self.assertEqual(result["paths"], states)
            self.assertEqual(len(result["errors"]), 1)


class Closing(unittest.TestCase):
    """The window's close button while something is being deleted: the window waits, then closes by itself."""

    def setUp(self):
        self.calls = []
        calls = self.calls

        class Window:
            def run_js(self, script):
                calls.append("told the page")

            def destroy(self):
                calls.append("closed")

        self.api = app.Api()
        self.api._window = Window()

    def test_closes_at_once_when_nothing_runs(self):
        self.assertTrue(self.api._on_closing())

    def test_waits_for_what_runs_then_closes(self):
        release = threading.Event()
        task_id = self.api._tasks.start(lambda task: release.wait(10) and {"ok": True})
        self.assertFalse(self.api._on_closing())
        self.assertTrue(self.api._tasks._tasks[task_id]["stop"])      # a permanent delete stops at once
        release.set()
        deadline = time.time() + 10
        while "closed" not in self.calls and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.calls, ["told the page", "closed"])

    def test_closing_again_closes_at_once(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.api._tasks.start(lambda task: release.wait(10))
        self.assertFalse(self.api._on_closing())
        self.assertTrue(self.api._on_closing())


class Building(unittest.TestCase):
    def test_clean_keeps_what_the_build_did_not_make(self):
        root = tempfile.mkdtemp(prefix="disk-report-test-")
        self.addCleanup(shutil.rmtree, root, True)
        dist, work = os.path.join(root, "dist"), os.path.join(root, "build")
        for path in ("Disk Report\\Disk Report.exe", "DiskReport-2.0-portable.zip", "DiskReport-Setup-2.0.exe",
                     "release-notes.md"):
            write(os.path.join(dist, path))
        write(os.path.join(work, "version_info.txt"))
        build.clean(dist, work, "2.0")
        self.assertEqual(os.listdir(dist), ["release-notes.md"])
        self.assertFalse(os.path.exists(work))


class DriveTexts(unittest.TestCase):
    def test_the_drive_scanned_is_named(self):
        tree = disk_report.Node("D:")
        classifier = disk_report.Classifier(tree, "D:", disk_report.path_tokens("D:"), None, None)
        classifier.claim_containers()
        self.assertEqual([i.name for i in classifier.items], ["Files at the top of D:"])


class InstalledPrograms(unittest.TestCase):
    """What Windows lists as installed beats the scan's labels: the scan may be days old."""

    def setUp(self):
        t = self.t = disk_report.path_tokens("C:")
        self.pf = t["%PF%"]
        real = actions.is_registered
        actions.is_registered = lambda ref: ref.get("key") == "{installed}"
        self.addCleanup(setattr, actions, "is_registered", real)

    def guard(self, items, installs=None):
        scan = scan_of(items)
        scan["apps"] = {"CUDA": [{"name": "NVIDIA CUDA Toolkit 13.1", "kind": "win32", "ref": {"key": "{installed}"}}],
                        "Gone": [{"name": "Gone 1.0", "kind": "win32", "ref": {"key": "{gone}"}}]}
        return actions.Guard(scan, self.t, installs=installs)

    def test_optional_program_files_of_an_installed_app_go_by_uninstalling(self):
        cuda = self.pf + "\\NVIDIA GPU Computing Toolkit"
        copy = self.pf + "\\Android\\Android Studio1"
        old = self.pf + "\\Old SDK"
        models = self.t["%CF%"] + "\\Adobe\\Adobe Photoshop 2026\\AddOnModules\\sensei_model_cache"
        guard = self.guard([
            {"name": "NVIDIA CUDA Toolkit", "cat": "dev", "safety": "optional", "group": "CUDA", "paths": [cuda]},
            {"name": "Android\\Android Studio1", "cat": "app", "safety": "optional", "group": "CUDA",
             "flags": ["leftover"], "paths": [copy]},
            {"name": "Old SDK", "cat": "dev", "safety": "optional", "group": "Gone", "paths": [old]},
            {"name": "Photoshop: AI models", "cat": "cache", "safety": "optional", "group": "CUDA", "paths": [models]}])
        self.assertTrue(guard.check(models, True)["ok"])  # a cache kept in an installed app's folders may still go
        verdict = guard.check(cuda)
        self.assertFalse(verdict["ok"])
        self.assertIn("Uninstall it instead", verdict["reason"])
        self.assertFalse(guard.check(cuda + "\\CUDA\\v13.1\\bin")["ok"])
        self.assertTrue(guard.check(copy)["ok"])        # a left-over copy no app points to may go
        self.assertTrue(guard.check(old)["ok"])         # its app was uninstalled
        self.assertIn(actions.norm(self.pf), guard.program_roots)

    def test_programs_installed_since_the_scan(self):
        tools = "C:\\Tools"
        app_dir = tools + "\\NewApp"
        cache = self.t["%LOCAL%"] + "\\SomeApp\\Cache"
        installs = lambda: [(app_dir, "New App 1.0"), (self.t["%LOCAL%"] + "\\SomeApp", "Some App"),
                            (self.t["%DOWNLOADS%"], "An odd installer")]
        guard = self.guard([
            {"name": "Tools", "cat": "other", "safety": "unknown", "size": 300, "paths": [tools]},
            {"name": "NewApp", "cat": "other", "safety": "unknown", "paths": [app_dir]},
            {"name": "SomeApp cache", "cat": "cache", "safety": "safe", "paths": [cache]},
            {"name": "Downloads", "cat": "user", "safety": "review", "paths": [self.t["%DOWNLOADS%"]]}], installs)
        verdict = guard.check(app_dir)                  # the scan called it unknown; Windows now lists it
        self.assertFalse(verdict["ok"])
        self.assertIn("New App 1.0 is installed there", verdict["reason"])
        verdict = guard.check(tools)                    # a folder that holds it: deleted around it
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["keep"], [actions.norm(app_dir)])
        self.assertTrue(guard.check(cache, True)["ok"]) # a cache inside an installed app's folder may still go
        self.assertTrue(guard.check(self.t["%DOWNLOADS%"] + "\\setup.exe")["ok"])   # folders never deleted: ignored

    def test_unlisted_installed_folders_are_named_and_left_alone(self):
        tools = "C:\\Tools"
        guard = self.guard([{"name": "Tools", "cat": "other", "safety": "unknown", "paths": [tools]}],
                           lambda: [(tools + "\\Fresh", "Fresh App")])
        verdict = guard.check(tools)
        self.assertEqual(verdict["keepItems"], [{"name": "Fresh App", "safety": "keep", "size": None}])

    def test_the_app_deletes_around_a_program_installed_since(self):
        root = tempfile.mkdtemp(prefix="disk-report-test-")
        self.addCleanup(shutil.rmtree, root, True)
        tools, fresh = os.path.join(root, "Tools"), os.path.join(root, "Tools", "Fresh")
        write(os.path.join(tools, "old.zip"))
        write(os.path.join(fresh, "fresh.exe"))
        real = app.installed_programs
        app.installed_programs = lambda drive, tokens: [(fresh, "Fresh App")]
        self.addCleanup(setattr, app, "installed_programs", real)
        api = AppDeleting.api(self, [{"name": "Tools", "cat": "other", "safety": "unknown", "paths": [tools]}])
        result = AppDeleting.finish(api, api.delete("C:", [tools], "forever", False))
        self.assertTrue(os.path.isfile(os.path.join(fresh, "fresh.exe")))
        self.assertFalse(os.path.exists(os.path.join(tools, "old.zip")))
        self.assertEqual((result["paths"][0]["state"], result["kept"]), ("partial", 1))


class CarriedOver(unittest.TestCase):
    """What was done while a scan was being made shows in that new scan's report."""

    def test_actions_during_the_scan_are_carried_over(self):
        self.addCleanup(lambda: os.path.exists(app.LOG_FILE) and os.remove(app.LOG_FILE))
        if os.path.exists(app.LOG_FILE):
            os.remove(app.LOG_FILE)
        started = 1_700_000_000.0
        for record in ({"kind": "delete", "drive": "C:", "scan": "old", "time": started - 50, "paths": ["before"]},
                       {"kind": "delete", "drive": "C:", "scan": "old", "time": started + 30, "paths": ["during"]},
                       {"kind": "uninstall", "drive": "C:", "scan": "old", "time": started + 40, "index": 3,
                        "name": "Discord", "state": "removed"},
                       {"kind": "delete", "drive": "D:", "scan": "d-old", "time": started + 30, "paths": ["other"]},
                       {"kind": "command", "scan": None, "time": started + 30, "command": "npm cache clean --force"},
                       {"kind": "delete", "drive": "C:", "scan": "new", "time": started + 500, "paths": ["after"]}):
            with open(app.LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        done = app.actions_for("new", "C:", started)
        self.assertEqual([r.get("paths") or r.get("name") for r in done], [["during"], "Discord", ["after"]])
        self.assertNotIn("index", done[1])             # matched by name: its place in the list was the old scan's
        self.assertEqual(len(app.actions_for("new")), 1)

    def test_when_a_scan_started(self):
        self.assertEqual(app.scan_started({"started": 123.5, "generated": "2026-11-17 10:00", "seconds": 60}), 123.5)
        ended = app.dt.datetime(2026, 11, 17, 10, 0).timestamp()
        self.assertEqual(app.scan_started({"generated": "2026-11-17 10:00", "seconds": 150}), ended - 150)
        self.assertIsNone(app.scan_started({}))


class Labels(unittest.TestCase):
    """Labels the scan gives: old IDE settings, things you made, Docker's disk."""

    def classifier(self, apps=()):
        info = type("Info", (), {})()
        info.apps = disk_report.AppIndex()
        for name in apps:
            info.apps.add(name, "")
        tokens = disk_report.path_tokens("C:")
        return disk_report.Classifier(disk_report.Node("C:"), "C:", tokens, info, None), tokens

    @staticmethod
    def tree(cls, path):
        """Hang folders for `path` in the classifier's tree."""
        node = cls.tree
        for part in path[len("C:\\"):].split("\\"):
            node.kids = node.kids or {}
            node = node.kids.setdefault(part.lower(), disk_report.Node(part))

    def test_old_ide_settings_are_optional_and_old_caches_safe(self):
        cls, t = self.classifier()
        made = {}
        for where, cat, safety in (("%ROAMING%", catalog.APPDATA, catalog.KEEP), ("%LOCAL%", catalog.CACHE, catalog.SAFE)):
            for version in ("2024.3.2", "2026.1.2"):
                path = t[where] + "\\Google\\AndroidStudio" + version
                item = cls.new("x", cat, safety, "x")
                cls.special_ide_versions(item, disk_report.Node("AndroidStudio" + version), path, [])
                made[(cat, version)] = item
        cls.finish_ide_versions()
        self.assertEqual(made[(catalog.APPDATA, "2024.3.2")].safety, catalog.OPTIONAL)
        self.assertEqual(made[(catalog.CACHE, "2024.3.2")].safety, catalog.SAFE)
        self.assertEqual(made[(catalog.APPDATA, "2026.1.2")].safety, catalog.KEEP)     # the current one stays

    def rule(self, name):
        return next(r for r in catalog.RULES if r["name"] == name)

    def test_things_you_made_are_marked(self):
        cls, t = self.classifier()
        self.tree(cls, t["~"] + "\\VirtualBox VMs\\Work VM")
        cls.apply(self.rule("VirtualBox VM: {0}"))
        self.assertIn("yours", cls.items[0].flags)
        for name in ("VirtualBox VMs", "VMware VM: {0}", "Hyper-V disk: {0}", "Android emulator: {0}"):
            self.assertTrue(self.rule(name)["yours"], name)

    def test_dockers_disk_is_kept_while_docker_is_installed(self):
        for apps, safety in ((["Docker Desktop"], catalog.KEEP), ([], catalog.OPTIONAL)):
            cls, t = self.classifier(apps)
            self.tree(cls, t["%LOCAL%"] + "\\Docker\\wsl")
            cls.apply(self.rule("Docker images, containers & volumes"))
            self.assertEqual(cls.items[0].safety, safety, apps)
            self.assertIn("yours", cls.items[0].flags)


class UninstallGone(unittest.TestCase):
    def test_an_app_already_removed_counts_as_uninstalled(self):
        root = tempfile.mkdtemp(prefix="disk-report-test-")
        self.addCleanup(shutil.rmtree, root, True)
        api = AppDeleting.api(self, [])
        scan_file = os.path.join(app.SCANS_DIR, "C-2026-01-01_000000.json")
        scan = app.read_json(scan_file)
        scan["apps"] = {"Gone": [{"name": "Gone 1.0", "kind": "win32",
                                  "ref": {"hive": "HKCU", "view": 0, "key": "{disk-report-test-not-there}"}}],
                        "Store": [{"name": "Store App", "kind": "store", "package": "Some.App_1.0_x64__abc",
                                   "dir": os.path.join(root, "not there")}]}
        app.write_json(scan_file, scan)
        api = app.Api()
        for group in ("Gone", "Store"):
            result = AppDeleting.finish(api, api.uninstall("C:", group, 0))
            self.assertEqual((result["state"], result["already"], result["error"]), ("removed", True, None), group)


class RestoredFromTheBin(unittest.TestCase):
    """The app notices what was restored from the Recycle Bin, or emptied from it, without a new scan."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="disk-report-test-")
        self.addCleanup(shutil.rmtree, self.root, True)

    @staticmethod
    def index(folder, name, original, version=2, size=5000):
        """An index file ($I…) as Windows writes it, version 2 (Windows 10+) or 1 (Vista to 8.1)."""
        path = original + "\0"
        if version == 2:
            raw = struct.pack("<qqqi", 2, size, 133_000_000_000_000_000, len(path)) + path.encode("utf-16-le")
        else:
            raw = struct.pack("<qqq", 1, size, 133_000_000_000_000_000) + path.encode("utf-16-le").ljust(520, b"\0")
        with open(os.path.join(folder, "$I" + name), "wb") as f:
            f.write(raw)

    def test_reads_the_recycle_bins_index(self):
        mine = os.path.join(self.root, "S-1-5-21-test")
        os.makedirs(mine)
        self.index(mine, "ABC123.txt", "C:\\Users\\me\\Downloads\\old.txt")
        write(os.path.join(mine, "$RABC123.txt"))
        self.index(mine, "DEF456", "C:\\Old backups", version=1, size=7000)
        os.makedirs(os.path.join(mine, "$RDEF456"))
        self.index(mine, "GHI789", "C:\\Restored folder")       # its item went back: Windows left the index behind
        items = actions.recycle_bin_items("C:", root=self.root)
        self.assertEqual(sorted(items), ["c:\\old backups", "c:\\users\\me\\downloads\\old.txt"])
        self.assertEqual(items["c:\\old backups"][0], 7000)

    def test_where_each_move_is_now(self):
        back, still, gone = (os.path.join(self.root, n) for n in ("back", "still", "gone"))
        os.makedirs(back)
        cache = os.path.join(self.root, "cache")
        os.makedirs(os.path.join(cache, "restored piece"))
        records = [
            {"kind": "delete", "mode": "trash", "pieces": [back, still, gone], "paths": [
                {"path": back, "state": "trashed"}, {"path": still, "state": "trashed"},
                {"path": gone, "state": "trashed"}, {"path": os.path.join(self.root, "x"), "state": "failed"}]},
            {"kind": "delete", "mode": "trash", "contents": True, "paths": [{"path": cache, "state": "trashed"}],
             "pieces": [os.path.join(cache, "restored piece"), os.path.join(cache, "emptied piece")]},
            {"kind": "delete", "mode": "trash", "contents": True, "paths": [{"path": cache, "state": "trashed"}]},
            {"kind": "delete", "mode": "trash", "paths": [{"path": back, "state": "trashed"}]},   # logged by 2.0.x
            {"kind": "delete", "mode": "forever", "paths": [{"path": gone, "state": "deleted"}]}]
        real = actions.recycle_bin_items
        actions.recycle_bin_items = lambda drive: {actions.norm(still): (5000, 0)}
        self.addCleanup(setattr, actions, "recycle_bin_items", real)
        marked = app.bin_marks(records, "C:")
        self.assertEqual(marked[0]["bin"], {back: "restored", still: "in", gone: "gone"})
        self.assertEqual(marked[1]["bin"], {cache: "mixed"})
        self.assertNotIn("bin", marked[2])          # a cache emptied by 2.0.x: its pieces weren't logged
        self.assertEqual(marked[3]["bin"], {back: "restored"})
        self.assertNotIn("bin", marked[4])

    def test_a_move_logs_what_went_and_when(self):
        folder = os.path.join(self.root, "junk")
        write(os.path.join(folder, "a.bin"))
        real = actions.recycle
        actions.recycle = lambda paths, hwnd=None: [shutil.rmtree(p) for p in paths] and {"ok": True}
        self.addCleanup(setattr, actions, "recycle", real)
        api = AppDeleting.api(self, [{"name": "junk", "cat": "other", "safety": "unknown", "paths": [folder]}])
        result = AppDeleting.finish(api, api.delete("C:", [folder], "trash", False))
        self.assertEqual(result["pieces"], [folder])
        self.assertIsInstance(result["time"], float)    # the page tells records apart by it
        done = api.done("C:")
        self.assertEqual(done[-1]["time"], result["time"])
        self.assertEqual(done[-1]["bin"], {folder: "gone"})   # the stand-in Recycle Bin above keeps nothing


class RecycleBin(unittest.TestCase):
    def test_query(self):
        info = actions.recycle_bin("C:")
        self.assertIsNotNone(info)
        self.assertGreaterEqual(info["size"], 0)
        self.assertGreaterEqual(info["items"], 0)

    @unittest.skipUnless(os.environ.get("DISK_REPORT_TEST_RECYCLE"), "sends a tiny file to the Recycle Bin")
    def test_recycle_a_file(self):
        folder = tempfile.mkdtemp(prefix="disk-report-test-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "disk-report-recycle-test.txt")
        write(path, "This file was sent to the Recycle Bin by Disk Report's tests. You can delete it.")
        before = actions.recycle_bin("C:")
        self.assertEqual(actions.recycle([path]), {"ok": True})
        self.assertFalse(os.path.exists(path))
        self.assertEqual(actions.recycle_bin("C:")["items"], before["items"] + 1)


if __name__ == "__main__":
    unittest.main()
