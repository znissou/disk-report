"""Build the Windows app into dist\\:

    Disk Report\\                     the app folder (Disk Report.exe and what it needs), made by PyInstaller
    DiskReport-<version>-portable.zip that folder zipped: unzip anywhere and run Disk Report.exe
    DiskReport-Setup-<version>.exe     an installer made by Inno Setup (https://jrsoftware.org/isinfo.php),
                                       when ISCC.exe is found

    py -m pip install -r requirements-build.txt
    py build.py
"""
import glob
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from disk_report import VERSION  # noqa: E402

DIST = os.path.join(HERE, "dist")
BUILD = os.path.join(HERE, "build")
NAME = "Disk Report"

VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=({nums}), prodvers=({nums}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'disk-report'),
      StringStruct('FileDescription', 'Disk Report'),
      StringStruct('FileVersion', '{text}'),
      StringStruct('InternalName', 'Disk Report'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'Disk Report.exe'),
      StringStruct('ProductName', 'Disk Report'),
      StringStruct('ProductVersion', '{text}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def find_iscc():
    found = shutil.which("ISCC")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        hits = sorted(glob.glob(os.path.join(base or "", "Inno Setup *", "ISCC.exe")), reverse=True)
        if base and hits:
            return hits[0]
    return None


def clean(dist=DIST, build=BUILD, version=VERSION):
    """Remove what the last build of this version made (the app folder, its zip and its installer) and the build
    folder. Anything else in dist\\, such as release notes, stays."""
    shutil.rmtree(build, ignore_errors=True)
    shutil.rmtree(os.path.join(dist, NAME), ignore_errors=True)
    for name in ("DiskReport-%s-portable.zip" % version, "DiskReport-Setup-%s.exe" % version):
        try:
            os.remove(os.path.join(dist, name))
        except FileNotFoundError:
            pass


def main():
    clean()
    os.makedirs(BUILD)
    nums = ", ".join((VERSION.split(".") + ["0", "0", "0"])[:4])
    version_file = os.path.join(BUILD, "version_info.txt")
    with open(version_file, "w", encoding="utf-8") as f:
        f.write(VERSION_INFO.format(nums=nums, text=VERSION))

    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", NAME,
                    "--icon", os.path.join(HERE, "assets", "icon.ico"), "--version-file", version_file,
                    "--add-data", os.path.join(HERE, "report_template.html") + os.pathsep + ".",
                    "--add-data", os.path.join(HERE, "assets", "icon.ico") + os.pathsep + "assets",
                    "--distpath", DIST, "--workpath", BUILD, "--specpath", BUILD,
                    os.path.join(HERE, "app.py")], check=True)

    app_dir = os.path.join(DIST, NAME)
    portable = os.path.join(DIST, "DiskReport-%s-portable.zip" % VERSION)
    with zipfile.ZipFile(portable, "w", zipfile.ZIP_DEFLATED) as z:
        for folder, _, files in os.walk(app_dir):
            for name in files:
                full = os.path.join(folder, name)
                z.write(full, os.path.join(NAME, os.path.relpath(full, app_dir)))
    print("Portable zip:", portable)

    iscc = find_iscc()
    if iscc is None:
        print("Inno Setup's ISCC.exe wasn't found, so there's no installer. Get it at https://jrsoftware.org/isinfo.php")
        return 0
    subprocess.run([iscc, "/Q", "/DAppVersion=" + VERSION, "/O" + DIST, os.path.join(HERE, "installer.iss")],
                   check=True)
    print("Installer:", os.path.join(DIST, "DiskReport-Setup-%s.exe" % VERSION))
    return 0


if __name__ == "__main__":
    sys.exit(main())
