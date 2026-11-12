"""Tests for what the app does when you click: deleting, the guard that decides what may go, uninstall commands.

    py -m unittest discover tests

Everything happens in temporary folders, except two read-only looks at the registry and the Recycle Bin.
Set DISK_REPORT_TEST_RECYCLE=1 to also send one tiny temporary file to the Recycle Bin.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import winreg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import actions  # noqa: E402
import catalog  # noqa: E402
import disk_report  # noqa: E402

LONG = actions.LONG


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
