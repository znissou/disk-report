"""What the folders on a Windows drive are, and whether they can be cleaned.

Each rule names a folder (or a big file) and explains it. Paths use these tokens:

    ~            your user profile           %LOCAL%      AppData\\Local
    %ROAMING%    AppData\\Roaming             %LOCALLOW%   AppData\\LocalLow
    %PD%         ProgramData                 %PF%         Program Files
    %PF86%       Program Files (x86)         %CF%         Program Files\\Common Files
    %WIN%        the Windows folder          %ROOT%       the drive root (C:)
    %DOCUMENTS% %DESKTOP% %DOWNLOADS% %PICTURES% %VIDEOS% %MUSIC%   your own folders
    %FLUTTER%    the Flutter SDK, when `flutter` is on PATH

A path part may contain * and ? wildcards. What they matched is available in the texts as
{0}, {1}, ... Rules are tried in order and the first rule to claim a folder wins. A rule for a
deeper folder carves that folder out of the rule for its parent, so a cache inside an app's
data folder is counted once, as cache.

Fields: name, category, safety, what it is, what to do about it, and optionally the app
it belongs to (group), a merge key (all matches become one item) and a special handler.
A group of "@match:{0}" means: find the installed app whose name looks like {0}; "@pkg:{0}" means:
the Store app whose package family name is {0}. Names may then use {app} for that app's name.
"""

# Categories
USER = "user"          # your own files
APP = "app"            # installed programs
APPDATA = "appdata"    # settings and data of apps
CACHE = "cache"        # caches, temporary files, re-downloadable content
DEV = "dev"            # SDKs, emulators, virtual machines, project build folders
SYSTEM = "system"      # Windows itself
OTHER = "other"        # not recognised

# Safety
SAFE = "safe"            # deleting loses nothing: it is rebuilt or re-downloaded
OPTIONAL = "optional"    # removable, with a trade-off explained in the action
REVIEW = "review"        # your files: only you can tell
UNINSTALL = "uninstall"  # remove it by uninstalling the app, not by deleting the folder
KEEP = "keep"            # needed
UNKNOWN = "unknown"      # not recognised


def R(path, name, cat, safety, desc, action=None, group=None, merge=None, special=None, needs=None, split=False):
    """split=True: installed programs found inside this folder are listed as their own items."""
    return {"path": path, "name": name, "cat": cat, "safety": safety, "desc": desc, "action": action,
            "group": group, "merge": merge, "special": special, "needs": needs, "split": split}


SETTINGS_TEMP = "Settings → System → Storage → Temporary files"
CHROMIUM_CACHES = ["Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
                   r"Service Worker\CacheStorage", r"Service Worker\ScriptCache"]
CHROMIUM_ROOT_CACHES = ["GrShaderCache", "ShaderCache", "GraphiteDawnCache", "component_crx_cache",
                        "extensions_crx_cache", "Crashpad"]


def browser(user_data, app, merge, action):
    """Rules for a Chromium browser: its profile, its caches."""
    rules = [R(user_data + "\\" + sub, app + " cache", CACHE, SAFE,
               "Copies of web pages, scripts and images " + app + " saved to load sites faster.",
               action, app, merge) for sub in ["*\\" + c for c in CHROMIUM_CACHES] + CHROMIUM_ROOT_CACHES]
    rules.append(R(user_data, app + " profiles", APPDATA, KEEP,
                   app + "'s bookmarks, history, saved passwords, extensions and site data.",
                   "To free space without losing anything, clear only the cache: " + action, app))
    return rules


def electron_caches(base, merge, group, name="{app} cache", extra=("Crashpad\\reports",)):
    """Cache folders that Electron/Chromium-based apps (Discord, Slack, Postman, VS Code...) keep."""
    return [R(base + "\\" + sub, name, CACHE, SAFE,
              "Web cache of the app: images, scripts and compiled code it saved to start faster, plus crash reports.",
              "Close the app, then delete these folders. The app recreates them.", group, merge)
            for sub in CHROMIUM_CACHES + list(extra)]


RULES = [
    # ------------------------------------------------------------------ your files
    R("%DOCUMENTS%", "Documents", USER, REVIEW,
      "Files you saved in Documents, including any project folders kept there.",
      "Open it and delete what you no longer need. Rebuildable project folders inside it "
      "(node_modules, .venv, build…) are listed under Developer tools, not here."),
    R("%DESKTOP%", "Desktop", USER, REVIEW, "Files and folders on your desktop.",
      "Move or delete what you no longer need."),
    R("%DOWNLOADS%", "Downloads", USER, REVIEW,
      "Everything you downloaded. Old installers, videos and archives here are usually the easiest space to free.",
      "Sort by size or date and delete what you no longer need. Installers (.exe, .msi) can always be downloaded again."),
    R("%PICTURES%", "Pictures", USER, REVIEW, "Your pictures and screenshots.", "Delete or move what you no longer need."),
    R("%VIDEOS%", "Videos", USER, REVIEW,
      "Your videos, including clips recorded by NVIDIA, Xbox Game Bar or OBS.",
      "Delete or move what you no longer need."),
    R("%MUSIC%", "Music", USER, REVIEW, "Your music and audio files.", "Delete or move what you no longer need."),
    R("~\\OneDrive*", "OneDrive", USER, REVIEW,
      "Your OneDrive folder. Only files kept on this PC use space; online-only files don't.",
      "Right-click big folders → 'Free up space' to keep them in the cloud only."),
    R("~\\CrossDevice", "Phone files (Phone Link)", USER, REVIEW,
      "Your phone's files, shown in File Explorer. They stay on the phone and are only downloaded when you open one.",
      "Nothing to do: online-only files take no space on this PC."),
    R("~\\Saved Games", "Saved games", USER, REVIEW, "Save files of some games.",
      "Keep, unless you no longer play those games."),
    R("~\\VirtualBox VMs\\*", "VirtualBox VM: {0}", DEV, OPTIONAL,
      "The disk and snapshots of a VirtualBox virtual machine.",
      "In VirtualBox: right-click the VM → Remove → Delete all files. Deleting old snapshots also shrinks it.",
      "VirtualBox"),
    R("~\\VirtualBox VMs", "VirtualBox VMs", DEV, OPTIONAL, "Folder holding VirtualBox virtual machines.",
      None, "VirtualBox"),
    R("%DOCUMENTS%\\Virtual Machines\\*", "VMware VM: {0}", DEV, OPTIONAL,
      "The disk of a VMware virtual machine.", "In VMware: right-click the VM → Manage → Delete from Disk."),
    R("%ROOT%\\$Recycle.Bin", "Recycle Bin", CACHE, SAFE,
      "Files you deleted. They keep using space until the Recycle Bin is emptied.",
      "Right-click the Recycle Bin on the desktop → Empty Recycle Bin."),

    # ------------------------------------------------------------------ files at the top of C:
    R("%ROOT%\\pagefile.sys", "Page file (virtual memory)", SYSTEM, KEEP,
      "Windows uses it as overflow when RAM is full. Its size is managed automatically.",
      "Leave it. Shrinking it can make heavy apps and games crash."),
    R("%ROOT%\\hiberfil.sys", "Hibernation file", SYSTEM, OPTIONAL,
      "Holds what's in RAM while the PC hibernates. Fast Startup uses it too.",
      "If you never use Hibernate, open Terminal (Admin) and run `powercfg /h off` (this also turns off "
      "Fast Startup). To keep Fast Startup with a smaller file, run `powercfg /h /type reduced` instead."),
    R("%ROOT%\\swapfile.sys", "Swap file", SYSTEM, KEEP, "Used by Windows for Store apps' memory. Managed automatically."),
    R("%ROOT%\\System Volume Information", "System Restore points", SYSTEM, KEEP,
      "Restore points and other data Windows protects. Only an administrator can measure it.",
      "Settings → System → About → System protection → Configure: delete old restore points or lower the maximum space."),
    R("%ROOT%\\Recovery", "Windows recovery tools", SYSTEM, KEEP, "Files Windows uses to repair or reset the PC."),
    R("%ROOT%\\$WinREAgent", "Windows recovery update files", SYSTEM, KEEP,
      "Temporary files from an update of the Windows recovery environment. Windows removes them itself."),
    R("%ROOT%\\Windows.old", "Previous Windows installation", CACHE, OPTIONAL,
      "Your previous Windows, kept after a major update so you can go back.",
      SETTINGS_TEMP + " → tick 'Previous Windows installation(s)'. Afterwards you can't roll back that update."),
    R("%ROOT%\\$WINDOWS.~BT", "Windows upgrade files", CACHE, SAFE, "Temporary files from a Windows upgrade.",
      SETTINGS_TEMP + " → 'Temporary Windows installation files'."),
    R("%ROOT%\\$Windows.~WS", "Windows upgrade files", CACHE, SAFE, "Temporary files from a Windows upgrade.",
      SETTINGS_TEMP + " → 'Temporary Windows installation files'."),
    R("%ROOT%\\ESD", "Windows setup download", CACHE, SAFE, "Windows setup files downloaded for an upgrade.",
      SETTINGS_TEMP + " → 'Temporary Windows installation files'."),
    R("%ROOT%\\$SysReset", "Reset leftovers", CACHE, SAFE, "Logs left by 'Reset this PC'. Not needed afterwards.",
      "Can be deleted from an administrator account."),
    R("%ROOT%\\AMD", "AMD driver setup files", CACHE, SAFE,
      "AMD's driver installer unpacks its files here. The installed driver doesn't need them.", "Delete the folder."),
    R("%ROOT%\\NVIDIA", "NVIDIA driver setup files", CACHE, SAFE,
      "NVIDIA's driver installer unpacks its files here. The installed driver doesn't need them.", "Delete the folder."),
    R("%ROOT%\\Intel", "Intel setup files", CACHE, SAFE,
      "Intel's installers unpack files and write logs here. Installed drivers don't need them.", "Delete the folder."),
    R("%ROOT%\\Drivers", "Driver setup files (from the PC maker)", OTHER, REVIEW,
      "Driver installers your PC came with. The drivers are already installed.",
      "Keep if you may need to reinstall drivers without internet; otherwise it can go."),
    R("%ROOT%\\OEM", "PC maker files", SYSTEM, KEEP, "Files from your PC's maker (recovery, drivers, branding)."),
    R("%ROOT%\\inetpub", "IIS web server", SYSTEM, KEEP, "Created by Windows' built-in web server (IIS)."),
    R("%ROOT%\\PerfLogs", "Performance logs", SYSTEM, KEEP, "Windows performance logs (usually empty)."),
    R("%ROOT%\\Config.Msi", "Installer rollback files", SYSTEM, KEEP, "Used by Windows Installer while installing programs."),
    R("%ROOT%\\OneDriveTemp", "OneDrive temporary files", CACHE, SAFE, "Temporary files of OneDrive.", "Delete the folder."),
    R("%ROOT%\\XboxGames", "Xbox app games", APP, UNINSTALL, "Games installed with the Xbox app or Game Pass.",
      "Xbox app → My Library → ⋯ on the game → Uninstall."),
    R("%ROOT%\\Users\\Public", "Public folder", OTHER, REVIEW, "Shared folder all accounts on this PC can use.",
      "Delete what you no longer need."),
    R("%ROOT%\\Users\\Default", "Default profile template", SYSTEM, KEEP,
      "Template Windows copies when a new account is created."),
    R("%ROOT%\\laragon\\www", "Laragon websites", USER, REVIEW, "Your local web projects served by Laragon.",
      "Delete projects you no longer need.", "Laragon"),
    R("%ROOT%\\laragon\\data", "Laragon databases", APPDATA, KEEP, "MySQL/MariaDB databases you created in Laragon.",
      "Drop databases you no longer need from Laragon's database tool.", "Laragon"),
    R("%ROOT%\\laragon", "Laragon", APP, UNINSTALL, "Laragon local web server (PHP, MySQL, Node…).",
      "Settings → Apps → Laragon → Uninstall.", "Laragon"),

    # ------------------------------------------------------------------ Windows folder
    R("%WIN%\\WinSxS", "Windows component store (WinSxS)", SYSTEM, KEEP,
      "Windows' library of system components, used to install updates and features. Many files here are "
      "the same files as in System32, stored once; the size shown counts them once.",
      "Never delete files here by hand. To trim old update versions: " + SETTINGS_TEMP +
      " → 'Windows Update Cleanup', or in Terminal (Admin): `DISM /Online /Cleanup-Image /StartComponentCleanup`."),
    R("%WIN%\\System32\\DriverStore", "Driver store", SYSTEM, KEEP,
      "Every installed driver, plus older versions Windows keeps so it can roll back.",
      "Disk Cleanup (run as administrator) → 'Device driver packages' removes old versions when it offers them."),
    R("%WIN%\\System32", "System32", SYSTEM, KEEP, "Core Windows programs and libraries."),
    R("%WIN%\\SysWOW64", "SysWOW64", SYSTEM, KEEP, "32-bit versions of Windows' libraries."),
    R("%WIN%\\Installer", "Windows Installer cache", SYSTEM, KEEP,
      "Copies of installers Windows needs to update, repair or uninstall programs.",
      "Never delete it by hand: programs could become impossible to update or uninstall."),
    R("%WIN%\\assembly", ".NET native images", SYSTEM, KEEP,
      "Precompiled .NET Framework code that makes .NET apps start faster."),
    R("%WIN%\\Microsoft.NET", ".NET Framework", SYSTEM, KEEP, "The .NET Framework that many apps need."),
    R("%WIN%\\SoftwareDistribution\\Download", "Windows Update downloads", CACHE, SAFE,
      "Update files Windows downloaded (and has usually already installed).",
      SETTINGS_TEMP + " → 'Windows Update Cleanup'."),
    R("%WIN%\\SoftwareDistribution", "Windows Update data", SYSTEM, KEEP, "Windows Update's database and history."),
    R("%WIN%\\Temp", "Windows temporary files", CACHE, SAFE, "Temporary files created by Windows and installers.",
      SETTINGS_TEMP + " → 'Temporary files'."),
    R("%WIN%\\Logs", "Windows logs", SYSTEM, KEEP, "Logs from Windows Update and setup. Windows trims them itself."),
    R("%WIN%\\LiveKernelReports", "Crash reports (drivers, graphics)", CACHE, SAFE,
      "Diagnostic dumps written when a driver or the graphics card stopped responding.",
      SETTINGS_TEMP + " → 'System error memory dump files'."),
    R("%WIN%\\Minidump", "Crash dumps (blue screens)", CACHE, SAFE, "Small dumps saved after blue-screen crashes.",
      SETTINGS_TEMP + " → 'System error memory dump files'."),
    R("%WIN%\\MEMORY.DMP", "Crash dump (blue screen)", CACHE, SAFE, "Full memory dump saved after a blue-screen crash.",
      SETTINGS_TEMP + " → 'System error memory dump files'."),
    R("%WIN%\\ServiceProfiles\\NetworkService\\AppData\\Local\\Microsoft\\Windows\\DeliveryOptimization",
      "Delivery Optimization cache", CACHE, SAFE, "Pieces of updates Windows keeps to share with other PCs.",
      SETTINGS_TEMP + " → 'Delivery Optimization Files'."),
    R("%WIN%\\SystemApps", "Built-in Windows apps", SYSTEM, KEEP, "Start menu, Settings and other built-in apps."),
    R("%WIN%\\servicing", "Windows servicing", SYSTEM, KEEP, "Used by Windows to install updates."),
    R("%WIN%\\Fonts", "Fonts", SYSTEM, KEEP, "Installed fonts.",
      "Remove fonts you don't use in Settings → Personalization → Fonts."),

    # ------------------------------------------------------------------ ProgramData
    R("%PD%\\Package Cache", "Installer cache (Package Cache)", SYSTEM, KEEP,
      "Copies of installers (Visual C++, .NET, SDKs…) needed to repair or uninstall them.",
      "Don't delete it by hand. It shrinks when you uninstall those programs."),
    R("%PD%\\Microsoft\\Windows\\WER", "Windows error reports", CACHE, SAFE,
      "Problem reports waiting to be sent to Microsoft, or already archived.",
      SETTINGS_TEMP + " → 'Windows error reports and feedback diagnostics'."),
    R("%PD%\\Microsoft\\Windows Defender", "Microsoft Defender", SYSTEM, KEEP, "Antivirus definitions and scan data."),
    R("%PD%\\Microsoft\\Windows\\Virtual Hard Disks\\*", "Hyper-V disk: {0}", DEV, OPTIONAL,
      "Disk of a Hyper-V virtual machine.", "Delete the VM in Hyper-V Manager, then this disk."),
    R("%PD%\\Microsoft", "Windows & Microsoft shared data", SYSTEM, KEEP, "Shared data of Windows features.", split=True),
    R("%PD%\\Packages", "Store apps shared data", SYSTEM, KEEP, "Data Microsoft Store apps share between accounts."),
    R("%PD%\\OEM", "PC maker data", SYSTEM, KEEP, "Data from your PC's maker."),

    # ------------------------------------------------------------------ Windows data in your profile
    R("%LOCAL%\\Temp", "Temporary files", CACHE, SAFE,
      "Temporary files apps leave behind. Anything not in use is safe to delete.",
      SETTINGS_TEMP + " → 'Temporary files'. Or press Win+R, type %temp%, and delete everything "
      "(skip the files Windows says are in use)."),
    R("%LOCAL%\\CrashDumps", "App crash dumps", CACHE, SAFE, "Memory dumps saved when apps crashed.",
      "Delete the files inside."),
    R("%LOCAL%\\D3DSCache", "DirectX shader cache", CACHE, SAFE,
      "Compiled graphics shaders. Games rebuild them (the first launch may stutter a little).",
      SETTINGS_TEMP + " → 'DirectX Shader Cache'."),
    R("%LOCAL%\\Microsoft\\Windows\\Explorer", "Thumbnail cache", CACHE, SAFE,
      "Thumbnails File Explorer saves so folders of pictures and videos open faster.",
      SETTINGS_TEMP + " → 'Thumbnails'."),
    R("%LOCAL%\\Microsoft\\Windows\\INetCache", "Internet cache (Windows)", CACHE, SAFE,
      "Web cache used by Windows components and older apps.", SETTINGS_TEMP + " → 'Temporary Internet Files'."),
    R("%LOCAL%\\Microsoft\\Windows\\WER", "App error reports", CACHE, SAFE, "Problem reports about apps that crashed.",
      SETTINGS_TEMP + " → 'Windows error reports and feedback diagnostics'."),
    *browser(r"%LOCAL%\Microsoft\Edge\User Data", "Microsoft Edge", "edge-cache",
             "Edge → Settings → Privacy, search and services → Clear browsing data → 'Cached images and files' "
             "(you stay logged in)."),
    R("%LOCAL%\\Microsoft\\OneDrive", "OneDrive", APP, UNINSTALL, "The OneDrive app.",
      "Settings → Apps → Installed apps → Microsoft OneDrive → Uninstall.", "OneDrive"),
    R("%LOCAL%\\Microsoft", "Windows & Microsoft apps data", APPDATA, KEEP,
      "Settings and data of Windows features and Microsoft apps.", split=True),
    R("%ROAMING%\\Microsoft", "Windows & Microsoft apps settings", APPDATA, KEEP,
      "Your Windows settings: Start menu, recent files, saved credentials, templates…", split=True),

    # ------------------------------------------------------------------ browsers
    R(r"%LOCAL%\Google\Chrome\User Data\OptGuideOnDeviceModel", "Chrome's built-in AI model", CACHE, OPTIONAL,
      "Gemini Nano, the AI model Chrome keeps on your PC for features like writing help and scam detection.",
      "In Chrome: Settings → System → turn off 'On-device AI' (if your version has it) and Chrome deletes the "
      "model. Deleting the folder alone makes Chrome download it again.", "Google Chrome"),
    *browser(r"%LOCAL%\Google\Chrome\User Data", "Google Chrome", "chrome-cache",
             "Chrome → Settings → Privacy and security → Delete browsing data → 'Cached images and files' "
             "(you stay logged in)."),
    *browser(r"%LOCAL%\BraveSoftware\Brave-Browser\User Data", "Brave", "brave-cache",
             "Brave → Settings → Privacy and security → Delete browsing data → 'Cached images and files'."),
    R(r"%LOCAL%\Mozilla\Firefox\Profiles\*\cache2", "Firefox cache", CACHE, SAFE,
      "Copies of web pages, scripts and images Firefox saved to load sites faster.",
      "Firefox → Settings → Privacy & Security → Cookies and Site Data → Clear Data → 'Cached Web Content'.",
      "Mozilla Firefox", "firefox-cache"),
    R(r"%ROAMING%\Mozilla\Firefox", "Firefox profiles", APPDATA, KEEP,
      "Firefox's bookmarks, history, saved passwords and extensions.", None, "Mozilla Firefox"),
    R(r"%PF86%\Google\GoogleUpdater\crx_cache", "Google Updater download cache", CACHE, SAFE,
      "Update packages Google Updater downloaded for Chrome and other Google apps.",
      "Delete the files inside (needs administrator). They're only downloaded again with the next update.",
      "Google Chrome"),
    R(r"%LOCAL%\Google\DriveFS", "Google Drive cache", CACHE, OPTIONAL,
      "Local copies of Google Drive files you opened or made available offline.",
      "Google Drive → Settings → Preferences: make folders online-only, or move the cache to another drive.",
      "Google Drive"),

    # ------------------------------------------------------------------ graphics drivers
    *[R(p, "NVIDIA shader cache", CACHE, SAFE,
        "Compiled shaders the NVIDIA driver keeps so games load faster. Games rebuild what they need "
        "(the first launch can stutter a little).",
        SETTINGS_TEMP + " → 'DirectX Shader Cache', or delete the contents. To stop it growing this big: "
        "NVIDIA Control Panel → Manage 3D settings → Shader Cache Size → 5 GB or 10 GB.",
        "NVIDIA driver & apps", "nvidia-shader-cache")
      for p in [r"%LOCAL%\NVIDIA\DXCache", r"%LOCAL%\NVIDIA\GLCache", r"%LOCAL%\NVIDIA\OptixCache",
                r"%LOCALLOW%\NVIDIA\PerDriverVersion\DXCache", r"%LOCALLOW%\NVIDIA\PerDriverVersion\GLCache",
                r"%LOCALLOW%\NVIDIA\DXCache", r"%LOCALLOW%\NVIDIA\GLCache", r"%PD%\NVIDIA Corporation\NV_Cache"]],
    R(r"%PD%\NVIDIA Corporation\NVIDIA App\UpdateFramework\ota-artifacts", "NVIDIA driver installer downloads",
      CACHE, SAFE,
      "Driver and NVIDIA App installers that NVIDIA App downloaded, plus unpacked copies. "
      "The installed driver doesn't need them.",
      "Close NVIDIA App, then delete the contents of this folder (needs administrator). "
      "NVIDIA App downloads them again if you reinstall a driver.", "NVIDIA driver & apps"),
    R(r"%PD%\NVIDIA Corporation\Downloader", "NVIDIA driver downloads", CACHE, SAFE,
      "Driver installers downloaded by GeForce Experience. The installed driver doesn't need them.",
      "Delete the contents of this folder (needs administrator).", "NVIDIA driver & apps"),
    R(r"%PD%\NVIDIA\NGX", "NVIDIA DLSS models", APPDATA, KEEP, "AI models games use for DLSS upscaling.",
      None, "NVIDIA driver & apps"),
    R(r"%PD%\NVIDIA Corporation", "NVIDIA shared data", APPDATA, KEEP, "Data of the NVIDIA driver and NVIDIA App.",
      None, "NVIDIA driver & apps"),
    R(r"%PD%\NVIDIA", "NVIDIA shared data", APPDATA, KEEP, "Data of the NVIDIA driver.", None, "NVIDIA driver & apps"),
    R(r"%PF%\NVIDIA Corporation", "NVIDIA driver & apps", APP, KEEP,
      "Components of the NVIDIA graphics driver, NVIDIA App and NVIDIA tools.",
      "Keep the driver. Tools you don't use can be removed in Settings → Apps.", "NVIDIA driver & apps"),
    R(r"%PF86%\NVIDIA Corporation", "NVIDIA driver & apps (32-bit)", APP, KEEP,
      "32-bit components of the NVIDIA driver.", None, "NVIDIA driver & apps"),
    R(r"%LOCAL%\NVIDIA", "NVIDIA data", APPDATA, KEEP, "Data of the NVIDIA driver.", None, "NVIDIA driver & apps"),
    R(r"%LOCAL%\NVIDIA Corporation", "NVIDIA App data", APPDATA, KEEP, "Data of NVIDIA App.", None,
      "NVIDIA driver & apps"),
    R(r"%LOCALLOW%\NVIDIA", "NVIDIA data", APPDATA, KEEP, "Data of the NVIDIA driver.", None, "NVIDIA driver & apps"),
    R(r"%PF%\NVIDIA GPU Computing Toolkit", "NVIDIA CUDA Toolkit", DEV, OPTIONAL,
      "Compiler and libraries for writing CUDA (GPU) programs. Games and normal apps don't need it.",
      "If you don't build or run CUDA code on this PC, uninstall the 'NVIDIA CUDA …' entries in Settings → Apps.",
      "NVIDIA CUDA Toolkit"),
    *[R(p, "AMD shader cache", CACHE, SAFE,
        "Compiled shaders the AMD driver keeps so games load faster. Games rebuild what they need.",
        "AMD Software → Settings → Graphics → Advanced → Reset Shader Cache.", "AMD Software", "amd-shader-cache")
      for p in [r"%LOCAL%\AMD\DxCache", r"%LOCAL%\AMD\DxcCache", r"%LOCAL%\AMD\GLCache", r"%LOCAL%\AMD\VkCache",
                r"%LOCALLOW%\AMD\DxCache", r"%LOCALLOW%\AMD\VkCache"]],
    R(r"%LOCAL%\Intel\ShaderCache", "Intel shader cache", CACHE, SAFE,
      "Compiled shaders the Intel graphics driver keeps. Games rebuild what they need.", "Delete the contents."),

    # ------------------------------------------------------------------ JavaScript / Node.js
    R(r"%LOCAL%\npm-cache", "npm cache", CACHE, SAFE,
      "A copy of every npm package you've downloaded, kept so installs are faster.",
      "Run `npm cache clean --force`. Packages download again when a project needs them.", "Node.js", "npm-cache"),
    R(r"%ROAMING%\npm-cache", "npm cache", CACHE, SAFE,
      "A copy of every npm package you've downloaded, kept so installs are faster.",
      "Run `npm cache clean --force`.", "Node.js", "npm-cache"),
    R(r"%ROAMING%\npm", "Global npm packages", DEV, KEEP, "Command-line tools you installed with `npm install -g`.",
      "List them with `npm ls -g --depth=0` and remove unused ones with `npm uninstall -g <name>`.", "Node.js"),
    R(r"%LOCAL%\Yarn\Cache", "Yarn cache", CACHE, SAFE, "Packages downloaded by Yarn.", "Run `yarn cache clean`.",
      "Node.js"),
    R(r"%LOCAL%\pnpm\store", "pnpm store", CACHE, SAFE, "Packages downloaded by pnpm.",
      "Run `pnpm store prune` to remove packages no project uses.", "Node.js"),
    R(r"%LOCAL%\node-gyp", "node-gyp headers", CACHE, SAFE, "Node.js headers downloaded to compile native modules.",
      "Delete the folder.", "Node.js"),
    R(r"~\.bun\install\cache", "Bun cache", CACHE, SAFE, "Packages downloaded by Bun.", "Run `bun pm cache rm`."),
    R(r"~\.cache\puppeteer", "Puppeteer browsers", CACHE, SAFE,
      "Chrome builds Puppeteer downloaded for automated browsing and tests.",
      "Delete the folder. Puppeteer downloads a browser again when a project installs it.", "Node.js"),
    R(r"%LOCAL%\ms-playwright", "Playwright browsers", CACHE, SAFE,
      "Browser builds Playwright downloaded for automated tests.",
      "Run `npx playwright uninstall --all`, or delete the folder. `npx playwright install` brings them back.",
      "Node.js"),
    R(r"%LOCAL%\electron\Cache", "Electron downloads", CACHE, SAFE, "Electron builds downloaded for app projects.",
      "Delete the folder.", "Node.js"),
    R(r"%LOCAL%\electron-builder\Cache", "electron-builder downloads", CACHE, SAFE,
      "Tools downloaded by electron-builder.", "Delete the folder.", "Node.js"),

    # ------------------------------------------------------------------ editors
    *[R(r"%ROAMING%\Code" + "\\" + p, "VS Code caches, logs & crash reports", CACHE, SAFE,
        "Downloaded extension packages, compiled code caches, logs and crash reports.",
        "Close VS Code and delete these folders. VS Code recreates what it needs.", "Visual Studio Code", "vscode-cache")
      for p in CHROMIUM_CACHES + ["CachedExtensionVSIXs", "CachedData", "CachedProfilesData", "logs", "Crashpad"]],
    R(r"%ROAMING%\Code\User\workspaceStorage", "VS Code per-project data", APPDATA, REVIEW,
      "One folder per project you opened in VS Code: editor state and data extensions keep per project.",
      "Folders of projects you no longer open can be deleted. VS Code recreates them if you open the project again.",
      "Visual Studio Code"),
    R(r"%ROAMING%\Code", "VS Code settings & data", APPDATA, KEEP,
      "Your VS Code settings, keybindings, snippets and the data extensions store.", None, "Visual Studio Code"),
    R(r"~\.vscode\extensions", "VS Code extensions", DEV, KEEP, "Extensions installed in VS Code.",
      "Uninstall extensions you don't use in VS Code → Extensions.", "Visual Studio Code"),
    R(r"~\.vscode", "VS Code (other)", DEV, KEEP, "VS Code's extension folder and command-line data.", None,
      "Visual Studio Code"),
    R(r"~\.cursor", "Cursor editor data", DEV, KEEP, "Extensions and data of the Cursor editor.",
      "Delete it if you no longer use Cursor.", "Cursor", needs="cursor"),
    R(r"%ROAMING%\Cursor", "Cursor editor settings", APPDATA, KEEP, "Settings and data of the Cursor editor.",
      "Delete it if you no longer use Cursor.", "Cursor", needs="cursor"),

    # ------------------------------------------------------------------ Claude
    R(r"%LOCAL%\Packages\Claude_*\LocalCache\Roaming\Claude\vm_bundles", "Claude's sandbox virtual machine", CACHE,
      OPTIONAL,
      "A small Linux virtual machine the Claude desktop app uses to run tasks and code in a sandbox on your PC.",
      "Quit Claude, then delete this folder. Claude downloads it again (a few GB) the next time a feature needs it.",
      "Claude", "claude-vm"),
    R(r"%ROAMING%\Claude\vm_bundles", "Claude's sandbox virtual machine", CACHE, OPTIONAL,
      "A small Linux virtual machine the Claude desktop app uses to run tasks and code in a sandbox on your PC.",
      "Quit Claude, then delete this folder. Claude downloads it again (a few GB) the next time a feature needs it.",
      "Claude", "claude-vm"),
    R(r"~\.claude\projects", "Claude Code conversations & memory", APPDATA, REVIEW,
      "Saved Claude Code sessions for each project (so you can resume them), plus each project's memory.",
      "Folders of projects you won't go back to can be deleted. Inside the others, keep the 'memory' folders.",
      "Claude Code"),
    *[R(r"~\.claude" + "\\" + p, "Claude Code temporary data", CACHE, SAFE,
        "Debug logs, shell snapshots, file snapshots used to undo edits, and other temporary data.",
        "Safe to delete while Claude Code isn't running.", "Claude Code", "claude-code-temp")
      for p in ["debug", "shell-snapshots", "file-history", "todos", "statsig", "paste-cache", "image-cache",
                "telemetry"]],
    R(r"~\.claude\plugins", "Claude Code plugins", APPDATA, KEEP, "Plugins installed in Claude Code.",
      "Remove plugins you don't use with /plugin in Claude Code.", "Claude Code"),
    R(r"~\.claude", "Claude Code settings & data", APPDATA, KEEP, "Settings and other data of Claude Code.", None,
      "Claude Code"),
    R(r"~\.local\share\claude", "Claude Code (program)", APP, KEEP,
      "The Claude Code program. A few recent versions are kept for updates.", None, "Claude Code"),

    # ------------------------------------------------------------------ Android
    R(r"%LOCAL%\Android\Sdk\system-images\*\*\*", "Emulator image: {0}", DEV, OPTIONAL,
      "An Android system image ({0}, {1}, {2}) used to run an emulator.",
      "Android Studio → Tools → SDK Manager → SDK Platforms → tick 'Show Package Details' → untick this image → Apply.",
      "Android SDK", special="android_image"),
    R(r"%LOCAL%\Android\Sdk\ndk\*", "Android NDK {0}", DEV, OPTIONAL,
      "Tools to compile C/C++ code into Android apps. Flutter plugins with native code need the exact version "
      "their project asks for.",
      "Android Studio → SDK Manager → SDK Tools → tick 'Show Package Details' → NDK → untick versions your "
      "projects don't use → Apply.", "Android SDK"),
    R(r"%LOCAL%\Android\Sdk\sources\*", "Android source code ({0})", DEV, OPTIONAL,
      "Android's own source code, only used to read or debug Android framework code.",
      "SDK Manager → SDK Platforms → tick 'Show Package Details' → untick 'Sources for Android …'.", "Android SDK"),
    R(r"%LOCAL%\Android\Sdk\.temp", "SDK Manager temporary downloads", CACHE, SAFE,
      "Leftovers from Android SDK downloads.", "Close Android Studio and delete the folder.", "Android SDK"),
    R(r"%LOCAL%\Android\Sdk", "Android SDK (tools & platforms)", DEV, KEEP,
      "Build tools, platforms, platform-tools and the emulator needed to build Android apps "
      "(Flutter's Android builds use it too).",
      "Old platform and build-tools versions can be removed in Android Studio → SDK Manager.", "Android SDK"),
    R(r"%LOCAL%\Android", "Android (other)", DEV, KEEP, "Other Android development files.", None, "Android SDK"),
    R(r"~\.android\avd\*.avd\snapshots", "Quick Boot snapshot: {0}", CACHE, SAFE,
      "The saved state of an emulator, so it starts instantly.",
      "Close the emulator and delete this folder (or Device Manager → ⋮ → Delete snapshots). "
      "The next start is a normal cold boot.", "Android SDK", special="avd"),
    R(r"~\.android\avd\*.avd", "Android emulator: {0}", DEV, OPTIONAL,
      "The disk of an Android emulator: the apps and data inside it.",
      "Android Studio → Device Manager → ⋮ next to the device → Delete. 'Wipe Data' shrinks it instead.",
      "Android SDK", special="avd"),
    R(r"~\.android\cache", "Android tools cache", CACHE, SAFE, "Cache of the Android SDK tools.", "Delete the folder.",
      "Android SDK"),
    R(r"~\.android", "Android tools settings", DEV, KEEP, "Settings, debug keys and emulators of the Android tools.",
      None, "Android SDK"),
    R(r"%LOCAL%\Google\AndroidStudio*", "Android Studio caches & indexes ({0})", CACHE, SAFE,
      "Code indexes, caches and logs of Android Studio. Rebuilt automatically (the first project open is slower).",
      "Close Android Studio and delete it.", "Android Studio", special="ide_versions"),
    R(r"%ROAMING%\Google\AndroidStudio*", "Android Studio settings ({0})", APPDATA, KEEP,
      "Settings and plugins of Android Studio.", None, "Android Studio", special="ide_versions"),
    R(r"%LOCAL%\JetBrains\*20??.*", "JetBrains IDE caches ({0})", CACHE, SAFE,
      "Code indexes and caches of a JetBrains IDE. Rebuilt automatically.",
      "Close the IDE and delete it (or File → Invalidate Caches).", "JetBrains IDEs", special="ide_versions"),
    R(r"%ROAMING%\JetBrains\*20??.*", "JetBrains IDE settings ({0})", APPDATA, KEEP,
      "Settings and plugins of a JetBrains IDE.", None, "JetBrains IDEs", special="ide_versions"),
    R(r"~\.gradle\caches", "Gradle cache", CACHE, SAFE,
      "Libraries and build results Gradle downloaded or produced for Android and Java builds.",
      "Close Android Studio and delete the folder. The next build downloads what it needs again "
      "(slower the first time).", "Gradle"),
    R(r"~\.gradle\wrapper\dists", "Downloaded Gradle versions", CACHE, SAFE,
      "Each Gradle version your projects used. Projects download the version they need again.",
      "Delete versions your projects no longer use, or all of them.", "Gradle"),
    R(r"~\.gradle\daemon", "Gradle daemon logs", CACHE, SAFE, "Logs of Gradle's background process.",
      "Delete the folder.", "Gradle"),
    R(r"~\.gradle\jdks", "JDKs downloaded by Gradle", CACHE, SAFE, "Java versions Gradle downloaded for builds.",
      "Delete the folder; Gradle downloads a JDK again if a build needs it.", "Gradle"),
    R(r"~\.gradle", "Gradle settings", DEV, KEEP, "Gradle's settings and helper files.", None, "Gradle"),
    R(r"~\.m2\repository", "Maven cache", CACHE, SAFE, "Java libraries downloaded by Maven.",
      "Delete the folder; Maven downloads what builds need again."),

    # ------------------------------------------------------------------ Flutter / Dart
    R(r"%LOCAL%\Pub\Cache", "Dart & Flutter package cache", CACHE, SAFE, "Packages downloaded by `flutter pub get`.",
      "Run `flutter pub cache clean`. Packages download again on the next `flutter pub get`.", "Flutter"),
    R(r"%LOCAL%\.dartServer", "Dart analysis cache", CACHE, SAFE,
      "Cache of the Dart analysis server that powers code hints in VS Code and Android Studio.",
      "Close your editor and delete the folder. It's rebuilt automatically.", "Flutter", "dart-server"),
    R(r"~\.dartServer", "Dart analysis cache", CACHE, SAFE,
      "Cache of the Dart analysis server that powers code hints in VS Code and Android Studio.",
      "Close your editor and delete the folder. It's rebuilt automatically.", "Flutter", "dart-server"),
    R(r"%FLUTTER%", "Flutter SDK", DEV, KEEP,
      "The Flutter SDK. Its bin\\cache folder holds the Dart SDK and engine files it downloaded.",
      "Keep it while you develop with Flutter.", "Flutter"),

    # ------------------------------------------------------------------ Python
    R(r"%LOCAL%\Programs\Python\Python*", "Python {0}", DEV, KEEP,
      "A Python installation and the packages installed into it.",
      "Remove packages you don't use with `pip uninstall`, or uninstall this Python in Settings → Apps.",
      "Python", special="python"),
    R(r"%LOCAL%\Programs\Python", "Python installations", DEV, KEEP, "Python versions installed for your account.",
      None, "Python"),
    R(r"%LOCAL%\pip\cache", "pip cache", CACHE, SAFE, "Copies of the Python packages pip downloaded.",
      "Run `py -m pip cache purge`.", "Python"),
    R(r"%LOCAL%\uv\cache", "uv cache", CACHE, SAFE, "Packages downloaded by uv.", "Run `uv cache clean`.", "Python"),
    R(r"%ROAMING%\uv\python", "Pythons installed by uv", DEV, KEEP, "Python versions uv downloaded.",
      "List them with `uv python list --only-installed`; remove with `uv python uninstall <version>`.", "Python"),
    R(r"%LOCAL%\pypoetry\Cache", "Poetry cache", CACHE, SAFE, "Packages downloaded by Poetry.",
      "Run `poetry cache clear --all pypi`.", "Python"),
    R(r"~\.cache\huggingface", "Hugging Face AI models", CACHE, OPTIONAL,
      "AI models and datasets downloaded by Python libraries (transformers, diffusers…).",
      "Delete models you no longer use (`huggingface-cli delete-cache` lets you pick). They download again if needed.",
      "Python"),
    R(r"~\.cache\torch", "PyTorch model cache", CACHE, OPTIONAL, "AI models downloaded by PyTorch.",
      "Delete models you no longer use; they download again if needed.", "Python"),
    R(r"%LOCAL%\tts", "Coqui TTS voice models", CACHE, OPTIONAL,
      "AI voice models downloaded by the Coqui TTS library (for example XTTS v2).",
      "Delete models you no longer use. TTS downloads a model again the first time it's needed.", "Python"),
    R(r"~\.ollama\models", "Ollama AI models", CACHE, OPTIONAL, "Language models downloaded with Ollama.",
      "List them with `ollama list` and remove with `ollama rm <model>`.", "Ollama"),
    R(r"~\.lmstudio\models", "LM Studio models", CACHE, OPTIONAL, "Language models downloaded in LM Studio.",
      "Delete models in LM Studio → My Models.", "LM Studio"),
    R(r"~\.cache\lm-studio\models", "LM Studio models", CACHE, OPTIONAL, "Language models downloaded in LM Studio.",
      "Delete models in LM Studio → My Models.", "LM Studio"),
    R(r"~\anaconda3\pkgs", "Conda package cache", CACHE, SAFE, "Packages conda downloaded.", "Run `conda clean --all`.",
      "Anaconda", "conda-pkgs"),
    R(r"~\miniconda3\pkgs", "Conda package cache", CACHE, SAFE, "Packages conda downloaded.", "Run `conda clean --all`.",
      "Anaconda", "conda-pkgs"),
    R(r"~\.conda\pkgs", "Conda package cache", CACHE, SAFE, "Packages conda downloaded.", "Run `conda clean --all`.",
      "Anaconda", "conda-pkgs"),
    R(r"~\.cache", "Tool caches (.cache)", CACHE, SAFE,
      "A shared cache folder used by command-line tools (models, downloads, browser builds…).",
      "Delete the subfolders of tools you no longer use; tools re-create what they need."),

    # ------------------------------------------------------------------ Go, Rust, .NET
    R(r"~\go\pkg\mod", "Go module cache", CACHE, SAFE, "Go modules downloaded for your projects.",
      "Run `go clean -modcache`.", "Go"),
    R(r"%LOCAL%\go-build", "Go build cache", CACHE, SAFE, "Compiled Go packages kept to speed up builds.",
      "Run `go clean -cache`.", "Go"),
    R(r"~\go", "Go workspace", DEV, KEEP, "Go tools you installed (go\\bin) and Go's module cache.", None, "Go"),
    R(r"~\.cargo\registry", "Cargo cache (Rust)", CACHE, SAFE, "Rust crates downloaded by Cargo.",
      "Delete the cache and src folders inside; Cargo downloads crates again when needed.", "Rust"),
    R(r"~\.cargo", "Rust tools (Cargo)", DEV, KEEP, "Cargo and tools you installed with `cargo install`.", None, "Rust"),
    R(r"~\.rustup", "Rust toolchains", DEV, KEEP, "Rust compiler versions.",
      "List with `rustup toolchain list`; remove with `rustup toolchain uninstall <name>`.", "Rust"),
    R(r"~\.nuget\packages", "NuGet package cache (.NET)", CACHE, SAFE, "Packages downloaded for .NET projects.",
      "Run `dotnet nuget locals all --clear`.", ".NET"),
    R(r"%LOCAL%\NuGet", "NuGet cache (.NET)", CACHE, SAFE, "HTTP cache of NuGet.",
      "Run `dotnet nuget locals all --clear`.", ".NET"),

    # ------------------------------------------------------------------ Docker, WSL
    R(r"%LOCAL%\Docker\wsl", "Docker images, containers & volumes", DEV, OPTIONAL,
      "Docker's virtual disk: every image, container, volume and build cache.",
      "`docker system df` shows what's inside. `docker system prune` removes stopped containers and dangling "
      "images (add `-a` for every unused image; volumes only with `--volumes`). The disk file may not shrink by "
      "itself afterwards: Docker Desktop → Troubleshoot → 'Clean / Purge data' empties it completely.",
      "Docker Desktop"),
    R(r"%LOCAL%\Docker", "Docker Desktop data", APPDATA, KEEP, "Settings and logs of Docker Desktop.", None,
      "Docker Desktop"),
    R(r"%LOCAL%\Packages\CanonicalGroupLimited.*\LocalState", "Linux (WSL) disk", DEV, KEEP,
      "The virtual disk of a WSL Linux distribution: everything inside that Linux.",
      "`wsl --list -v` shows your distributions. `wsl --unregister <name>` removes one with all its files.",
      "WSL"),
    R(r"%LOCAL%\wsl\*", "Linux (WSL) disk", DEV, KEEP,
      "The virtual disk of a WSL Linux distribution: everything inside that Linux.",
      "`wsl --list -v` shows your distributions. `wsl --unregister <name>` removes one with all its files.",
      "WSL"),

    # ------------------------------------------------------------------ games and launchers
    R(r"%PF86%\Steam\steamapps\common\*", "{0}", APP, UNINSTALL, "A game installed with Steam.",
      "Steam → Library → right-click the game → Manage → Uninstall.", "{0}"),
    R(r"%PF86%\Steam\steamapps\shadercache", "Steam shader cache", CACHE, SAFE,
      "Pre-compiled shaders Steam downloads for your games.",
      "Delete the contents; Steam downloads them again for games you play "
      "(or turn off Steam → Settings → Downloads → Shader Pre-Caching).", "Steam"),
    R(r"%PF86%\Steam\steamapps\downloading", "Steam unfinished downloads", CACHE, SAFE,
      "Leftovers of game downloads or updates.", "Delete the contents while Steam isn't downloading.", "Steam"),
    R(r"%PF86%\Steam\steamapps\temp", "Steam temporary files", CACHE, SAFE, "Temporary files of Steam.",
      "Delete the contents while Steam isn't downloading.", "Steam"),
    R(r"%PF86%\Steam\steamapps\workshop", "Steam Workshop content", APPDATA, REVIEW, "Mods and maps you subscribed to.",
      "Unsubscribe from items you don't use on the game's Workshop page.", "Steam"),
    R(r"%PF86%\Steam", "Steam", APP, UNINSTALL, "The Steam app (games are listed separately).",
      "Settings → Apps → Steam → Uninstall (this removes the games too).", "Steam"),
    R(r"%PF%\Epic Games\Launcher", "Epic Games Launcher", APP, UNINSTALL, "The Epic Games Launcher app.",
      "Settings → Apps → Epic Games Launcher → Uninstall.", "Epic Games Launcher"),
    R(r"%PF86%\Epic Games\Launcher", "Epic Games Launcher", APP, UNINSTALL, "The Epic Games Launcher app.",
      "Settings → Apps → Epic Games Launcher → Uninstall.", "Epic Games Launcher"),
    R(r"%PF86%\Epic Games\Epic Online Services", "Epic Online Services", APP, KEEP,
      "Online features some games use (friends, achievements).", None, "Epic Games Launcher"),
    R(r"%PF%\Epic Games\*", "{0}", APP, UNINSTALL, "A game installed with the Epic Games Launcher.",
      "Epic Games Launcher → Library → ⋯ on the game → Uninstall.", "{0}"),
    R(r"%LOCAL%\EpicGamesLauncher\Saved\webcache*", "Epic Games Launcher web cache", CACHE, SAFE,
      "Web pages and images the launcher saved.", "Close the launcher and delete these folders.",
      "Epic Games Launcher", "epic-cache"),
    R(r"%LOCAL%\EpicGamesLauncher", "Epic Games Launcher data", APPDATA, KEEP, "Settings and logs of the launcher.",
      None, "Epic Games Launcher"),

    # ------------------------------------------------------------------ specific apps
    R(r"%PD%\BlueStacks_nxt", "BlueStacks: Android system & your apps", APPDATA, UNINSTALL,
      "The Android system BlueStacks runs, with the Android apps and games you installed inside it.",
      "Delete instances you don't use in BlueStacks → Multi-Instance Manager, or uninstall BlueStacks in "
      "Settings → Apps to remove all of it.", "BlueStacks"),
    R(r"%ROAMING%\MetaQuotes\Terminal\*\bases\*\history", "MetaTrader price history", CACHE, OPTIONAL,
      "Price history MetaTrader downloaded from your broker's server, for charts and the Strategy Tester.",
      "Close MetaTrader, then delete the folders of symbols you don't use (or all of them): MetaTrader downloads "
      "the history it needs again. Lower Tools → Options → Charts → 'Max bars in chart' to keep it smaller.",
      "MetaTrader", "mt-history"),
    R(r"%ROAMING%\MetaQuotes\Terminal\*\bases\*\ticks", "MetaTrader tick data", CACHE, OPTIONAL,
      "Tick-by-tick price data MetaTrader downloaded, mostly for the Strategy Tester.",
      "Close MetaTrader and delete the folders of symbols you don't test. They download again when needed.",
      "MetaTrader", "mt-ticks"),
    R(r"%ROAMING%\MetaQuotes\Tester", "MetaTrader Strategy Tester cache", CACHE, SAFE,
      "Data the Strategy Tester's agents cached during backtests.",
      "Close MetaTrader and delete the folder; it's rebuilt on the next test.", "MetaTrader"),
    R(r"%ROAMING%\MetaQuotes", "MetaTrader data", APPDATA, KEEP,
      "Settings and logs of your MetaTrader terminals, and your MQL5 programs.",
      "Keep the MQL5 folders: they hold your experts, indicators and scripts.", "MetaTrader"),
    R(r"%PD%\MetaQuotes", "MetaTrader shared data", APPDATA, KEEP, "Data shared by MetaTrader terminals.", None,
      "MetaTrader"),
    R(r"%ROAMING%\Maxon\*\Redshift\Cache", "Redshift render cache", CACHE, SAFE,
      "Textures and shaders cached by the Redshift renderer (used by Cinebench and Cinema 4D).",
      "Delete the folder. It's rebuilt the next time something renders with Redshift.", "Maxon / Cinebench",
      "redshift-cache"),
    R(r"%ROAMING%\Maxon", "Maxon / Cinebench data", APPDATA, KEEP, "Settings and data of Maxon apps (Cinebench, Cinema 4D).",
      "Delete it if you no longer use Cinebench or Cinema 4D.", "Maxon / Cinebench"),
    R(r"%CF%\Adobe\*\AddOnModules\sensei_model_cache", "{0}: AI models (all users)", CACHE, OPTIONAL,
      "AI models Adobe downloaded for features like Generative Fill and the Remove tool.",
      "Close the app first. The models download again (several GB) the next time you use those AI features.",
      "@match:{0}"),
    R(r"%ROAMING%\Adobe\*\AddOnModules\sensei_model_cache", "{0}: AI models (your account)", CACHE, OPTIONAL,
      "AI models Adobe downloaded for features like Generative Fill and the Remove tool.",
      "Close the app first. The models download again (several GB) the next time you use those AI features.",
      "@match:{0}"),
    R(r"%CF%\Adobe\Acrobat\Setup Files", "Acrobat installer cache", SYSTEM, KEEP,
      "Installer files Acrobat needs to repair and update itself.", None, "Adobe Acrobat"),
    R(r"%CF%\Adobe", "Adobe shared components", APP, UNINSTALL,
      "Components shared by Adobe apps (Photoshop, Acrobat…): Camera Raw, AI engines, fonts, installers.",
      "Removed when you uninstall the Adobe apps that use them.", "Adobe (shared)"),
    R(r"%PD%\Adobe\CameraRaw", "Adobe Camera Raw profiles & models", APPDATA, KEEP,
      "Camera and lens profiles plus AI models used by Camera Raw in Photoshop.", None, "Adobe (shared)"),
    R(r"%PD%\Adobe", "Adobe shared data", APPDATA, KEEP, "Data shared by Adobe apps.", None, "Adobe (shared)"),
    R(r"%ROAMING%\Adobe", "Adobe apps settings", APPDATA, KEEP, "Settings and presets of your Adobe apps.", None,
      "Adobe (shared)"),
    R(r"%LOCAL%\Adobe", "Adobe apps data", APPDATA, KEEP, "Data of your Adobe apps.", None, "Adobe (shared)"),
    R(r"%ROAMING%\Telegram Desktop\tdata\user_data", "Telegram media cache", CACHE, SAFE,
      "Photos, videos and files Telegram cached from your chats.",
      "Telegram → Settings → Advanced → Manage local storage → Clear all.", "Telegram"),
    R(r"%ROAMING%\Telegram Desktop", "Telegram data", APPDATA, KEEP, "Telegram's settings and login.", None, "Telegram"),
    R(r"%LOCAL%\Packages\SpotifyAB.SpotifyMusic_*\LocalCache\Spotify\Data", "Spotify cache", CACHE, SAFE,
      "Songs Spotify cached while streaming.", "Spotify → Settings → Storage → Clear cache.", "Spotify", "spotify-cache"),
    R(r"%LOCAL%\Spotify\Data", "Spotify cache", CACHE, SAFE, "Songs Spotify cached while streaming.",
      "Spotify → Settings → Storage → Clear cache.", "Spotify", "spotify-cache"),

    # ------------------------------------------------------------------ Microsoft programs
    R(r"%PF86%\Microsoft\EdgeWebView", "WebView2 Runtime", APP, KEEP,
      "Lets apps (Teams, Office, Claude and many installers) show web content. Don't remove it.", None,
      "WebView2 Runtime"),
    *[R(r"%PF86%\Microsoft" + "\\" + p, "Microsoft Edge", APP, KEEP,
        "The Edge browser. It's part of Windows and normally can't be uninstalled.", None, "Microsoft Edge",
        "edge-program") for p in ["Edge", "EdgeCore", "EdgeUpdate"]],
    R(r"%PF86%\Microsoft\Copilot", "Copilot", APP, UNINSTALL, "The Microsoft Copilot app.",
      "Settings → Apps → Installed apps → Copilot → Uninstall.", "Copilot"),
    R(r"%PF86%\Microsoft", "Microsoft programs (other)", APP, KEEP, "Other Microsoft components.", None, split=True),
    R(r"%CF%\microsoft shared", "Microsoft shared components", APP, KEEP,
      "Components shared by Microsoft programs (Office, Visual C++, ink…).", None),
    R(r"%PF%\dotnet", ".NET runtime", SYSTEM, KEEP, "Runs apps built with .NET. Many apps need it.", None, ".NET"),
    R(r"%PF86%\dotnet", ".NET runtime (32-bit)", SYSTEM, KEEP, "Runs 32-bit apps built with .NET.", None, ".NET"),
    *[R(p, "Windows SDK", DEV, OPTIONAL,
        "Headers and tools to build Windows desktop apps (Visual Studio and Flutter's Windows builds use it).",
        "Needed only if you build Windows apps. Uninstall 'Windows Software Development Kit' in Settings → Apps.",
        "Windows SDK", "windows-sdk") for p in [r"%PF%\Windows Kits", r"%PF86%\Windows Kits"]],
    *[R(p, "Visual Studio files", APP, UNINSTALL, "Files of Visual Studio or its build tools.",
        "Visual Studio Installer → ⋯ → Uninstall.", "Visual Studio", "visual-studio")
      for p in [r"%PF%\Microsoft Visual Studio", r"%PF86%\Microsoft Visual Studio"]],

    # ------------------------------------------------------------------ more tools and apps
    R(r"%PF%\NVIDIA Corporation\Nsight*", "{0}", DEV, OPTIONAL,
      "NVIDIA's profiling tool for CUDA and graphics development. Games and normal apps don't need it.",
      "If you don't develop CUDA or graphics code on this PC, uninstall 'NVIDIA Nsight …' in Settings → Apps.",
      "NVIDIA CUDA Toolkit"),
    R(r"%PF%\WSL", "Windows Subsystem for Linux", APP, KEEP,
      "Runs Linux distributions (WSL) and Docker Desktop's engine.", None, "WSL"),
    R(r"%PF%\Go", "Go", DEV, KEEP, "The Go programming language.", "Uninstall Go in Settings → Apps if you no longer use it.",
      "Go"),
    R(r"%PF%\nodejs", "Node.js", DEV, KEEP, "The Node.js runtime and npm.", None, "Node.js"),
    R(r"%LOCAL%\Package Cache", "Installer cache (your account)", SYSTEM, KEEP,
      "Copies of installers needed to repair or uninstall programs installed for your account.",
      "Don't delete it by hand. It shrinks when you uninstall those programs."),
    R(r"~\.local", ".local (command-line tools)", DEV, KEEP,
      "Programs and data of command-line tools installed for your account (uv, pipx, Claude Code…)."),
    R(r"~\.config", ".config (tool settings)", APPDATA, KEEP, "Settings of command-line tools."),
    R(r"~\.EasyOCR", "EasyOCR models", CACHE, OPTIONAL, "Text-recognition models downloaded by the EasyOCR Python library.",
      "Delete it if you no longer use EasyOCR; it downloads the models again when needed.", "Python"),
    R(r"~\.wakatime", "WakaTime data", DEV, KEEP, "The WakaTime command-line tool and its offline queue (coding time tracking)."),
    R(r"~\.npm", "npm cache", CACHE, SAFE, "A copy of every npm package you've downloaded, kept so installs are faster.",
      "Run `npm cache clean --force`.", "Node.js", "npm-cache"),
    R(r"%ROAMING%\Python", "Python packages installed with --user", DEV, KEEP,
      "Python packages installed with `pip install --user`.",
      "List them with `py -m pip list --user`; remove unused ones with `py -m pip uninstall <name>`.", "Python"),
    R(r"%LOCAL%\Composer", "Composer cache (PHP)", CACHE, SAFE, "PHP packages downloaded by Composer.",
      "Run `composer clear-cache`.", "PHP"),
    R(r"%PD%\USOShared", "Windows Update orchestrator", SYSTEM, KEEP, "Logs and data of Windows Update's scheduler."),
    R(r"%PD%\USOPrivate", "Windows Update orchestrator", SYSTEM, KEEP, "Data of Windows Update's scheduler."),
    R(r"%LOCAL%\AnkiProgramFiles", "Anki", APP, UNINSTALL, "The Anki flashcards program, set up by the Anki launcher.",
      "Settings → Apps → Anki Launcher → Uninstall.", "Anki"),
    R(r"%ROAMING%\Anki2", "Anki collections", APPDATA, KEEP, "Your Anki decks, review history and media.", None, "Anki"),
    R(r"%LOCAL%\Steam\htmlcache", "Steam web cache", CACHE, SAFE, "Web pages and images Steam's store and overlay saved.",
      "Close Steam and delete the folder.", "Steam"),
    R(r"%LOCAL%\*-updater", "{app} update downloads", CACHE, SAFE,
      "Installers this app downloaded to update itself. Once the update is installed they're not needed.",
      "Close the app and delete the folder.", "@match:{0}", special="updater"),

    # ------------------------------------------------------------------ generic: caches of Electron apps
    *electron_caches(r"%ROAMING%\*", "electron-{0}", "@match:{0}"),
    *electron_caches(r"%LOCAL%\Packages\*\LocalCache\Roaming\*", "store-electron-{0}", "@pkg:{0}"),
]

# Folders in Program Files that come with Windows
BUILTIN_PROGRAM_FOLDERS = [
    "Common Files", "Internet Explorer", "Microsoft Update Health Tools", "ModifiableWindowsApps", "MSBuild",
    "Reference Assemblies", "Uninstall Information", "Windows Defender", "Windows Defender Advanced Threat Protection",
    "Windows Mail", "Windows Media Player", "Windows Multimedia Platform", "Windows NT", "Windows Photo Viewer",
    "Windows Portable Devices", "Windows Security", "Windows Sidebar", "WindowsPowerShell", "Microsoft.NET",
    "Microsoft SDKs", "Package Cache",
]
for _folder in BUILTIN_PROGRAM_FOLDERS:
    for _pf in ("%PF%", "%PF86%"):
        RULES.append(R(_pf + "\\" + _folder, _folder, SYSTEM, KEEP, "Comes with Windows.", None))


# Containers: folders whose subfolders each become an item of their own. Their own loose files
# and small subfolders form one "remainder" item. (path, kind, remainder name, category, safety, text)
CONTAINERS = [
    ("%ROOT%", "root", "Files at the top of C:", OTHER, KEEP, "Loose files in the root of the drive."),
    ("%ROOT%\\Users", "users", "Users folder: loose files", OTHER, KEEP, "Loose files in C:\\Users."),
    ("~", "profile", "Your profile: loose files", OTHER, KEEP,
     "Loose files in your profile folder, including NTUSER.DAT (your part of the registry)."),
    ("~\\AppData", "appdata", "AppData: other", APPDATA, KEEP, "Loose files in AppData."),
    ("%LOCAL%", "local", "AppData\\Local: small folders", APPDATA, KEEP,
     "Hundreds of small app data folders (each under 2 MB) and loose files."),
    ("%ROAMING%", "roaming", "AppData\\Roaming: small folders", APPDATA, KEEP,
     "Many small app settings folders (each under 2 MB) and loose files."),
    ("%LOCALLOW%", "locallow", "AppData\\LocalLow: small folders", APPDATA, KEEP, "Small app data folders."),
    ("%LOCAL%\\Packages", "packages", "Store apps data: small folders", APPDATA, KEEP,
     "Data of Microsoft Store apps, each under 2 MB."),
    ("%LOCAL%\\Programs", "programs", "Programs (your account): small folders", APP, KEEP,
     "Small programs installed for your account only."),
    ("%PD%", "programdata", "ProgramData: small folders", APPDATA, KEEP, "Small data folders shared by all users."),
    ("%PF%", "pf", "Program Files: small folders", APP, KEEP, "Small program folders."),
    ("%PF86%", "pf", "Program Files (x86): small folders", APP, KEEP, "Small 32-bit program folders."),
    ("%CF%", "shared", "Common Files: small folders", APP, KEEP, "Small shared components."),
    ("%CF86%", "shared", "Common Files (x86): small folders", APP, KEEP, "Small shared 32-bit components."),
    ("%PF%\\WindowsApps", "windowsapps", "Microsoft Store apps: other files", APP, KEEP,
     "Store app files Windows doesn't let normal programs list."),
    ("%WIN%", "windows", "Windows: other files", SYSTEM, KEEP, "Other parts of Windows."),
]

# How folder names map to app groups. (regex on the cleaned app name, group, category, safety, description)
# Category/safety/description are only used for programs found through Windows' list of installed apps.
GROUP_ALIASES = [
    (r"^nvidia (cuda|nsight)", "NVIDIA CUDA Toolkit", DEV, OPTIONAL,
     "NVIDIA's CUDA development tools. Games and normal apps don't need them."),
    (r"^nvidia", "NVIDIA driver & apps", None, None, None),
    (r"^(microsoft )?visual studio code", "Visual Studio Code", None, None, None),
    (r"^(microsoft )?visual studio", "Visual Studio", None, None, None),
    (r"^windows software development kit", "Windows SDK", DEV, OPTIONAL,
     "Headers and tools to build Windows desktop apps."),
    (r"^microsoft edge webview2", "WebView2 Runtime", None, KEEP, None),
    (r"^microsoft edge", "Microsoft Edge", None, KEEP, None),
    (r"^(oracle )?virtualbox", "VirtualBox", None, None, None),
    (r"^bluestacks", "BlueStacks", None, None, None),
    (r"^docker", "Docker Desktop", None, None, None),
    (r"^android studio", "Android Studio", None, None, None),
    (r"^adobe photoshop", "Adobe Photoshop", None, None, None),
    (r"^adobe acrobat", "Adobe Acrobat", None, None, None),
    (r"^python", "Python", DEV, None, None),
    (r"^(google )?chrome", "Google Chrome", None, None, None),
    (r"^claude code", "Claude Code", None, None, None),
    (r"^claude", "Claude", None, None, None),
    (r"^metatrader|mt5|mt4", "MetaTrader", None, None, None),
    (r"^cinebench|^maxon", "Maxon / Cinebench", None, None, None),
    (r"^epic games", "Epic Games Launcher", None, None, None),
    (r"^(microsoft )?onedrive", "OneDrive", None, None, None),
    (r"^synology drive", "Synology Drive", None, None, None),
]

# Folders inside projects that can be rebuilt. name -> (kind, condition)
# Condition: None (the name is enough), ("parent", marker bits) or ("self", marker bits).
ARTIFACT_KINDS = {
    "node": ("node_modules folders", DEV, SAFE,
             "JavaScript dependencies downloaded into each project.",
             "Delete node_modules in projects you're not working on. Run `npm install` (or yarn/pnpm) when you go "
             "back to a project: it downloads them again in a minute or two."),
    "venv": ("Python virtual environments", DEV, OPTIONAL,
             "Per-project Python environments with the packages installed into them.",
             "Delete the environments of projects you've finished. To rebuild one later: `python -m venv .venv`, "
             "then `pip install -r requirements.txt` (if the project has one)."),
    "build": ("Build outputs & caches", DEV, SAFE,
              "Files produced when building or running projects (build, .next, .dart_tool, target, __pycache__…).",
              "Delete them, or run the project's clean command (`flutter clean`, `cargo clean`, `gradlew clean`). "
              "They're recreated on the next build."),
}
MARK_PACKAGE_JSON, MARK_PUBSPEC, MARK_PYVENV, MARK_CARGO = 1, 2, 4, 8
MARK_GRADLE, MARK_PYPROJECT, MARK_CMAKE, MARK_POM, MARK_DOTNET = 16, 32, 64, 128, 256
ARTIFACTS = {
    "node_modules": ("node", None),
    ".venv": ("venv", ("self", MARK_PYVENV)),
    "venv": ("venv", ("self", MARK_PYVENV)),
    "env": ("venv", ("self", MARK_PYVENV)),
    ".next": ("build", None), ".nuxt": ("build", None), ".svelte-kit": ("build", None),
    ".turbo": ("build", None), ".parcel-cache": ("build", None), ".angular": ("build", None),
    ".expo": ("build", None), ".dart_tool": ("build", None),
    "build": ("build", ("parent", MARK_PUBSPEC | MARK_GRADLE | MARK_CMAKE | MARK_PYPROJECT)),
    "target": ("build", ("parent", MARK_CARGO | MARK_POM)),
    "bin": ("build", ("parent", MARK_DOTNET)), "obj": ("build", ("parent", MARK_DOTNET)),
    "dist": ("build", ("parent", MARK_PACKAGE_JSON | MARK_PYPROJECT)),
    ".gradle": ("build", ("parent", MARK_GRADLE)), ".cxx": ("build", ("parent", MARK_GRADLE)),
    "__pycache__": ("build", None), ".pytest_cache": ("build", None), ".mypy_cache": ("build", None),
    ".ruff_cache": ("build", None), ".tox": ("build", None),
}
