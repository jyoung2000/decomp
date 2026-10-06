# Installing Rebuild Studio on Windows

This is for anyone who wants to use Rebuild Studio. You do not need a terminal, PowerShell, Python, Node or any developer tools.

## What you download

| File | What it is |
|------|------------|
| `RebuildStudio-<version>-x64-setup-UNSIGNED.exe` | **The installer (recommended).** A normal Windows setup program (NSIS `.exe`, not an MSI). Installs for your user account only, so it does not ask for administrator rights. |
| `RebuildStudio-<version>-win-x64-portable-UNSIGNED.zip` | Portable copy for testing. Unzip and run `rebuild-studio.exe`. No shortcuts, no uninstall entry. |

"UNSIGNED" means the files are not signed with a code-signing certificate. Windows SmartScreen may show **"Windows protected your PC"**
the first time. If you trust where you got the file, click **More info → Run anyway**. Check the file against `SHA256SUMS.txt`
if it came with the download.

Requirements: Windows 10 (22H2) or Windows 11, 64-bit. The Microsoft Edge WebView2 runtime is already part of Windows 11; on
Windows 10 the installer downloads it automatically if it is missing (this needs an internet connection once).

## Install

1. Double-click the setup file.
2. **Welcome** → **Next**.
3. **Choose install location**: keep the default (`%LOCALAPPDATA%\Rebuild Studio`) unless you have a reason to change it → **Next**.
4. Wait for **Installation Complete** → **Next**.
5. On the last page:
   * **Create desktop shortcut** — ticked by default. Leave it ticked if you want an icon on the desktop.
   * **Run Rebuild Studio** — starts the app when you click **Finish**.
6. Click **Finish**.

You now have: a **Start menu** entry "Rebuild Studio", a **desktop icon** (if you left the box ticked), and an entry in
**Settings → Apps → Installed apps**.

## Start it and pin it to the taskbar

* Double-click the desktop icon, or open the Start menu and click **Rebuild Studio**.
* The first start can take a few seconds longer (Windows Defender scans the new program). A small "Starting Rebuild Studio…"
  window appears, then the main window. No black console window should ever appear.
* To pin: while the app is open, right-click its icon in the taskbar → **Pin to taskbar**. Close the app and click the pinned
  icon to start it again.

## First steps inside the app

1. **Tools** (left sidebar): install the analysis tools you need. Each card says what the tool is for (for example
   ".NET programs" or "native Windows programs"), its size and license. Click **Install**; you see real download progress. Every
   download is checked against a pinned checksum before anything is unpacked. If you are offline, the card tells you which file to
   download on another computer; then use **Install from file…**.
2. **New project**: choose the folder where the program you want to rebuild is installed, and an output folder. The app explains
   before it starts what it can and cannot produce for that kind of program with your current settings.
3. Running the original program (to record how it behaves, which is what makes a meaningful comparison possible) only happens
   if you allow it for that project. It then runs with reduced rights; see `docs/ISOLATION.md` for exactly what that protects
   and what it does not.
4. **Connections** (optional): add an AI provider key to let the app write and repair the rebuilt program. Without AI you still
   get recovered evidence and a starting project, clearly marked as not implemented yet.

## Where your data lives

* Projects, evidence, logs and downloaded tools: `%LOCALAPPDATA%\RebuildStudio`
* AI keys: Windows Credential Manager (entries named `RebuildStudio:…`), never in plain files.
* Rebuilt programs: the output folder you chose for each project.

## Update

Run the newer setup file. It replaces the program and keeps your projects, tools, keys and settings.

## Uninstall

**Settings → Apps → Installed apps → Rebuild Studio → Uninstall** (or Start menu → right-click → Uninstall).

* By default your projects, tools, logs and AI keys are **kept**, so a reinstall picks up where you left off.
* To remove everything, tick **Delete the application data** on the uninstall confirmation page. That also deletes
  `%LOCALAPPDATA%\RebuildStudio` and the `RebuildStudio:…` Credential Manager entries. Output folders you chose are never deleted.

## If it does not start

A window titled **"Rebuild Studio could not start"** explains what failed and offers **Try again**, **Open log folder** and
**Quit**. Try again first; if it keeps failing, reinstall (your data is kept) and send `controller.log` from the log folder with
your report.
