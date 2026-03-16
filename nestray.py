#!/usr/bin/env python3
"""
Nestray - A systray application for Thunderbird
Shows unread mail status and allows click-to-raise Thunderbird.
"""

import argparse
import configparser
import re
import shutil
import subprocess
import sys
import threading
import time
import os
from pathlib import Path

qt5_forced = os.getenv("FORCE_QT5")
force_qt5 = qt5_forced is not None and qt5_forced != "0"

try:
    if force_qt5:
        raise Exception("qt5 forced")
    from PyQt6.QtCore import QThread, pyqtSignal
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QMessageBox
    print("nestray: using qt6")
except:
    try:
        from PyQt5.QtCore import QThread, pyqtSignal
        from PyQt5.QtGui import QIcon
        from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QMessageBox
        print("nestray: using qt5")
    except:
        print("you need pyqt6 or pyqt5 installed to run nestray")
        sys.exit(1)


# --- Constants ---

CONFIG_PATH = Path.home() / ".config" / "nestray.ini"
TB_PROFILE_BASE = Path.home() / ".thunderbird"

DEFAULTS = {
    "General": {
        "PollInterval": "10",
        "RaiseTimeout": "5",
        "DesktopNotifications": "1",
        "RemindInterval": "30",
        "UseBundledKdoTool": "0",
        "Debug": "0",
    }
}

have_unread_mail_icon = "mail-mark-read"
have_no_unread_mail_icon = "mail-mark-unread"


# --- Logger ---

class Logger:
    """Simple debug logger. Only prints when debug mode is enabled."""

    def __init__(self, debug: bool = True):
        self.debug = debug

    def log(self, msg: str) -> None:
        if self.debug:
            print(f"nestray: {msg}")


# Global instance — initialised with debug=False, reconfigured after config is loaded.
logger = Logger()


# --- Config ---

def load_config() -> configparser.ConfigParser:
    """Load or create the config file, filling in any missing defaults."""
    config = configparser.ConfigParser(allow_no_value=False)

    if CONFIG_PATH.exists():
        config.read(CONFIG_PATH)

    changed = False
    for section, keys in DEFAULTS.items():
        if not config.has_section(section):
            config.add_section(section)
            changed = True
        for key, value in keys.items():
            if not config.has_option(section, key):
                config.set(section, key, value)
                changed = True

    if changed:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            # Write with comments if creating fresh
            if not CONFIG_PATH.exists() or CONFIG_PATH.stat().st_size == 0:
                f.write("[General]\n")
                f.write("# the period between polls of the IMAP folder, in seconds\n")
                f.write(f"PollInterval={config.get('General', 'PollInterval')}\n")
                f.write("# the maximum time to poll for the thunderbird window when attempting to raise it, in seconds\n")
                f.write(f"RaiseTimeout={config.get('General', 'RaiseTimeout')}\n")
                f.write("# whether to raise desktop notifications for unread mail (1 = enabled, 0 = disabled)\n")
                f.write(f"DesktopNotifications={config.get('General', 'DesktopNotifications')}\n")
                f.write("# minutes to wait before re-notifying about the same unread count\n")
                f.write(f"RemindInterval={config.get('General', 'RemindInterval')}\n")
                f.write("# use the kdotool binary bundled alongside nestray.py (1 = enabled, 0 = disabled)\n")
                f.write(f"UseBundledKdoTool={config.get('General', 'UseBundledKdoTool')}\n")
                f.write("# enable debug logging (1 = enabled, 0 = disabled)\n")
                f.write(f"Debug={config.get('General', 'Debug')}\n")
            else:
                config.write(f)

    return config


# --- kdotool resolution ---

# Resolved path to kdotool — set once by resolve_kdotool().
_kdotool: str | None = None


def resolve_kdotool(config: configparser.ConfigParser) -> str | None:
    """
    Determine which kdotool binary to use.
    If UseBundledKdoTool is enabled, use the binary alongside nestray.py.
    Otherwise, look for kdotool in PATH and warn if not found.
    """
    use_bundled = config.getboolean("General", "UseBundledKdoTool", fallback=False)

    if use_bundled:
        bundled = Path(__file__).resolve().parent / "kdotool"
        if bundled.exists() and os.access(bundled, os.X_OK):
            logger.log(f"using bundled kdotool: {bundled}")
            return str(bundled)
        print(
            f"nestray: UseBundledKdoTool is enabled but bundled binary not found at {bundled}",
            file=sys.stderr,
        )
        return None

    system_kdotool = shutil.which("kdotool")
    if system_kdotool is not None:
        logger.log(f"using system kdotool: {system_kdotool}")
        return system_kdotool

    print(
        "nestray: kdotool not found in PATH. Window management will not work.\n"
        "  You can set UseBundledKdoTool=1 in ~/.config/nestray.ini to use the\n"
        "  version bundled in the nestray repository.",
        file=sys.stderr,
    )
    return None


# --- Thunderbird profile detection ---

def get_thunderbird_profile_path() -> Path | None:
    """
    Determine the active Thunderbird profile directory.
    Prefers install record (modern TB), falls back to Default=1.
    """
    ini_path = TB_PROFILE_BASE / "profiles.ini"
    if not ini_path.exists():
        return None

    ini = configparser.ConfigParser()
    ini.read(ini_path)

    # Prefer install record
    for section in ini.sections():
        if section.startswith("Install") and ini.has_option(section, "Default"):
            rel_path = ini.get(section, "Default")
            return TB_PROFILE_BASE / rel_path

    # Fall back to Default=1
    for section in ini.sections():
        if ini.get(section, "Default", fallback="0") == "1":
            path = ini.get(section, "Path", fallback=None)
            if path is None:
                continue
            if ini.get(section, "IsRelative", fallback="0") == "1":
                return TB_PROFILE_BASE / path
            return Path(path)

    return None


# --- Unread mail counting via .msf (Mork index) files ---

# Pattern to parse the column dictionary at the top of a Mork file.
# Entries look like (A2=numNewMsgs) — the column ID is hex, the name is a string.
_COLUMN_DICT_PATTERN = re.compile(r"\(([0-9a-fA-F]+)=([^)]+)\)")


def _find_num_new_msgs_column(data: str) -> str | None:
    """
    Parse the Mork column dictionary to find which column ID maps to
    numNewMsgs. Column IDs are not fixed across Thunderbird versions/profiles.
    """
    for col_id, col_name in _COLUMN_DICT_PATTERN.findall(data):
        if col_name == "numNewMsgs":
            return col_id
    return None


def find_inbox_msf_files(profile_path: Path) -> list[Path]:
    """
    Find INBOX.msf index files in the profile.
    If a smart mailbox (unified inbox) exists and has content, return only that
    to avoid double-counting. Otherwise aggregate from individual accounts.
    """
    smart = profile_path / "Mail" / "smart mailboxes" / "Inbox.msf"
    if smart.exists() and smart.stat().st_size > 0:
        print(f"found unified inbox: {smart}")
        return [smart]

    msf_files: list[Path] = []
    for search_root in [profile_path / "Mail", profile_path / "ImapMail"]:
        print(f"search for msf files under {search_root}")
        if not search_root.exists():
            print("not found")
            continue
        for account_dir in search_root.iterdir():
            if not account_dir.is_dir():
                continue
            # Try exact name first, then case-insensitive fallback
            candidate = account_dir / "INBOX.msf"
            print(f"candidate: {candidate}")
            if not candidate.exists():
                for item in account_dir.iterdir():
                    if item.name.lower() == "inbox.msf" and item.is_file():
                        candidate = item
                        break
                else:
                    continue
            print(f"candidate: {candidate}")
            if candidate.exists() and candidate.stat().st_size > 0:
                msf_files.append(candidate)

    return msf_files


def get_unread_from_msf(msf_path: Path) -> int:
    """
    Extract the unread count from a .msf file by:
    1. Parsing the Mork column dictionary to find the ID for numNewMsgs.
    2. Finding the last occurrence of that field in the data.
    Mork appends updates, so the last match is the most current value.
    """
    try:
        data = msf_path.read_text(errors="replace")
    except OSError as e:
        print(f"nestray: could not read {msf_path}: {e}", file=sys.stderr)
        return 0

    col_id = _find_num_new_msgs_column(data)
    if col_id is None:
        logger.log(f"numNewMsgs column not found in {msf_path}")
        return 0

    logger.log(f"numNewMsgs column is {col_id} in {msf_path}")
    # The ^ prefix is literal Mork syntax (column namespace), not a regex anchor.
    pattern = re.compile(r"\(\^" + col_id + r"=([0-9a-fA-F]+)\)")
    matches = pattern.findall(data)
    if not matches:
        return 0

    return int(matches[-1], 16)


def get_total_unread(profile_path: Path) -> int:
    """
    Count total unread messages across Inbox .msf files in the profile.
    Uses the smart mailbox (unified inbox) if available, otherwise
    aggregates from individual IMAP/local/POP3 account inboxes.
    """
    total = 0
    print(f"looking for msf files under {profile_path}")
    for msf_path in find_inbox_msf_files(profile_path):
        total += get_unread_from_msf(msf_path)
    return total


# --- Window raising ---

def is_thunderbird_running() -> bool:
    """Check if a Thunderbird process is currently running."""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "thunderbird"],
            capture_output=True, timeout=2
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def launch_thunderbird_minimized(timeout: float) -> None:
    """
    Launch Thunderbird and minimize its window so it syncs mail in the
    background without appearing on screen. No-op if already running.
    This function blocks until the window is minimized or the timeout expires,
    so it should only be called from a background thread.
    """
    if is_thunderbird_running():
        return

    logger.log("thunderbird not running, launching minimized")
    try:
        subprocess.Popen(
            ["thunderbird"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        print("nestray: thunderbird not found in your PATH", file=sys.stderr)
        return

    # Wait for the window to appear, then minimize it
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        wid = find_thunderbird_window()
        if wid is not None:
            try:
                subprocess.run(
                    [_kdotool, "windowminimize", wid],
                    capture_output=True, timeout=2
                )
                logger.log("thunderbird launched and minimized")
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            return
        time.sleep(0.05)

    print("nestray: thunderbird launched but window not found to minimize", file=sys.stderr)


class EnsureThunderbirdThread(QThread):
    """Ensures Thunderbird is running (and minimized) in the background."""

    def __init__(self, raise_timeout: float):
        super().__init__()
        self.raise_timeout = raise_timeout

    def run(self) -> None:
        launch_thunderbird_minimized(self.raise_timeout)


def find_thunderbird_window() -> str | None:
    """Find the Thunderbird window ID via kdotool, or None if not found."""
    if _kdotool is None:
        return None
    try:
        result = subprocess.run(
            [_kdotool, "search", "--name", "Thunderbird"],
            capture_output=True, text=True, timeout=2
        )
        window_ids = result.stdout.strip().splitlines()
        if window_ids:
            logger.log(f"found thunderbird window(s): {window_ids}")
            return window_ids[0]
        logger.log("no thunderbird window found")
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"nestray: kdotool search failed: {e}", file=sys.stderr)
        return None


def toggle_thunderbird_window() -> None:
    """
    Toggle the Thunderbird window:
      - If a window is found and is the active window, minimize it.
      - If a window is found but is not active (including minimized), activate it.
      - If no window is found, run the thunderbird executable. This will
        either raise an existing instance or start a new one.
    """
    logger.log("toggle requested")
    wid = find_thunderbird_window()

    if wid is not None:
        # Check if TB is the currently active (focused) window
        is_active = False
        try:
            active = subprocess.run(
                [_kdotool, "getactivewindow"],
                capture_output=True, text=True, timeout=2
            )
            active_wid = active.stdout.strip()
            is_active = active_wid == wid
            logger.log(f"active window: {active_wid}, thunderbird window: {wid}, is_active: {is_active}")
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.log(f"getactivewindow failed: {e}, assuming not active")

        if is_active:
            logger.log(f"minimizing window {wid}")
            try:
                subprocess.run(
                    [_kdotool, "windowminimize", wid],
                    capture_output=True, timeout=2
                )
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                print(f"nestray: windowminimize failed: {e}", file=sys.stderr)
        else:
            logger.log(f"activating window {wid}")
            try:
                subprocess.run(
                    [_kdotool, "windowactivate", wid],
                    capture_output=True, timeout=2
                )
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                print(f"nestray: windowactivate failed: {e}", file=sys.stderr)
        return

    # No window found — run thunderbird (raises existing or starts new)
    logger.log("launching thunderbird executable")
    try:
        subprocess.Popen(
            ["thunderbird"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        print("nestray: thunderbird not found in your PATH", file=sys.stderr)


def raise_thunderbird_window() -> None:
    """
    Raise (activate) the Thunderbird window without toggling.
    If already active, this is a no-op. If no window is found,
    launch Thunderbird.
    """
    logger.log("raise requested")
    wid = find_thunderbird_window()

    if wid is not None:
        logger.log(f"activating window {wid}")
        try:
            subprocess.run(
                [_kdotool, "windowactivate", wid],
                capture_output=True, timeout=2
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"nestray: windowactivate failed: {e}", file=sys.stderr)
        return

    # No window found — run thunderbird (raises existing or starts new)
    logger.log("launching thunderbird executable")
    try:
        subprocess.Popen(
            ["thunderbird"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        print("nestray: thunderbird not found in your PATH", file=sys.stderr)


class ToggleThread(QThread):
    """
    Toggles the Thunderbird window in a background thread so the tray
    stays responsive.
    """

    def run(self) -> None:
        toggle_thunderbird_window()


# --- Poll worker thread ---

class MailPoller(QThread):
    """Background thread that polls for unread mail on a timer."""
    poll_started = pyqtSignal()
    poll_finished = pyqtSignal(int)  # unread count
    ensure_thunderbird = pyqtSignal()  # request TB launch from main thread

    def __init__(self, profile_path: Path, interval: int):
        super().__init__()
        self.profile_path = profile_path
        self.interval = interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            # Ask the main thread to ensure TB is running (non-blocking)
            if not is_thunderbird_running():
                self.ensure_thunderbird.emit()
            self.poll_started.emit()
            try:
                unread = get_total_unread(self.profile_path)
                logger.log(f"there are {unread} unread mails")
            except Exception as e:
                print(f"nestray: error polling mail: {e}", file=sys.stderr)
                unread = 0
            self.poll_finished.emit(unread)
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()
        self.wait()


# --- Main tray application ---

class NestrayApp:
    def __init__(self, app: QApplication, raise_on_start: bool = False):
        self.app = app
        self.config = load_config()
        logger.debug = self.config.getboolean("General", "Debug", fallback=False)
        self.poll_interval = self.config.getint("General", "PollInterval", fallback=30)
        self.raise_timeout = self.config.getfloat("General", "RaiseTimeout", fallback=5.0)
        self.desktop_notifications = self.config.getboolean("General", "DesktopNotifications", fallback=True)
        self.remind_interval = self.config.getint("General", "RemindInterval", fallback=30) * 60  # stored as minutes, used as seconds

        # When --raise is passed on first startup, skip the initial minimize
        self._skip_first_minimize = raise_on_start

        # Notification state
        self._last_unread = 0
        self._last_notify_time: float = 0.0

        # Validate profile
        self.profile_path = get_thunderbird_profile_path()
        if self.profile_path is None or not self.profile_path.exists():
            QMessageBox.critical(
                None,
                "Nestray",
                "No Thunderbird profile found.\n\n"
                "Please ensure Thunderbird has been run at least once.",
            )
            sys.exit(1)

        # Build tray icon
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon.fromTheme(have_no_unread_mail_icon))
        self.tray.setToolTip("Open Thunderbird")

        # Right-click menu
        menu = QMenu()
        exit_action = menu.addAction("Exit")
        exit_action.triggered.connect(self._on_exit)
        self.tray.setContextMenu(menu)

        # Left-click to launch/raise
        self.tray.activated.connect(self._on_tray_activated)

        # Keep references to background threads so they aren't GC'd
        self._launch_thread: ToggleThread | None = None
        self._ensure_thread: EnsureThunderbirdThread | None = None

        self.tray.show()

        # Start mail poller
        self.poller = MailPoller(self.profile_path, self.poll_interval)
        self.poller.poll_started.connect(self._on_poll_started)
        self.poller.poll_finished.connect(self._on_poll_finished)
        self.poller.ensure_thunderbird.connect(self._ensure_thunderbird_running)
        self.poller.start()

    def _on_poll_started(self) -> None:
        self.tray.setIcon(QIcon.fromTheme("folder-sync"))
        self.tray.setToolTip("Checking mail...")

    def _on_poll_finished(self, unread: int) -> None:
        if unread > 0:
            self.tray.setIcon(QIcon.fromTheme(have_unread_mail_icon))
            self.tray.setToolTip(f"{unread} unread")
            self._maybe_notify(unread)
        else:
            self.tray.setIcon(QIcon.fromTheme(have_no_unread_mail_icon))
            self.tray.setToolTip("Open Thunderbird")
        self._last_unread = unread

    def _maybe_notify(self, unread: int) -> None:
        """Raise a desktop notification if the count increased or the remind interval has elapsed."""
        if not self.desktop_notifications:
            return

        now = time.monotonic()
        count_increased = unread > self._last_unread
        remind_elapsed = (now - self._last_notify_time) >= self.remind_interval

        if count_increased or remind_elapsed:
            emails = "emails" if unread > 1 else "email"
            self.tray.showMessage(
                "Nestray",
                f"You have {unread} unread {emails}",
                QSystemTrayIcon.MessageIcon.Information,
                5000,
            )
            self._last_notify_time = now

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        logger.log(f"tray activated, reason={reason}")
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_thunderbird()

    def _toggle_thunderbird(self) -> None:
        """Toggle Thunderbird window visibility in a background thread."""
        if self._launch_thread is not None and self._launch_thread.isRunning():
            logger.log("toggle thread still running, skipping")
            return
        logger.log("starting toggle thread")
        self._launch_thread = ToggleThread()
        self._launch_thread.start()

    def _ensure_thunderbird_running(self) -> None:
        """Launch Thunderbird minimized in a background thread if not already running."""
        if self._skip_first_minimize:
            self._skip_first_minimize = False
            logger.log("--raise: skipping minimize on first poll")
            if not is_thunderbird_running():
                # Launch TB but don't minimize — let it appear normally
                logger.log("thunderbird not running, launching (no minimize)")
                try:
                    subprocess.Popen(
                        ["thunderbird"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except FileNotFoundError:
                    print("nestray: thunderbird not found in your PATH", file=sys.stderr)
            return
        if self._ensure_thread is not None and self._ensure_thread.isRunning():
            return
        self._ensure_thread = EnsureThunderbirdThread(self.raise_timeout)
        self._ensure_thread.start()

    def _on_exit(self) -> None:
        self.poller.stop()
        if self._launch_thread is not None and self._launch_thread.isRunning():
            self._launch_thread.wait()
        if self._ensure_thread is not None and self._ensure_thread.isRunning():
            self._ensure_thread.wait()
        self.tray.hide()
        self.app.quit()

def write_pid_file(pidfile):
    with open(pidfile, "w") as fp:
        pid = str(os.getpid())
        logger.log(f"writing pidfile with pid {pid}")
        fp.write(pid)

def get_running_pid() -> int | None:
    """
    Check if another nestray instance is running.
    Returns the PID of the running instance, or None if not running.
    Writes our own PID file if no other instance is found.
    """
    logger.log(f"my pid: {os.getpid()}")
    pidfile = "/tmp/nestray.pid"
    if not os.path.exists(pidfile):
        logger.log("pidfile not found")
        write_pid_file(pidfile)
        return None
    with open(pidfile, "r") as fp:
        existing_pid = int(fp.read().strip())
        logger.log(f"pidfile found with pid {existing_pid}")
        try:
            os.kill(existing_pid, 0)
            logger.log("kill-0 success")
            logger.log("nestray is already running")
            return existing_pid
        except ProcessLookupError as e:
            logger.log(f"kill-0 fails: {e}")
            write_pid_file(pidfile)
            return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Nestray - Thunderbird systray app")
    parser.add_argument(
        "--raise", dest="raise_window", action="store_true",
        help="Raise the Thunderbird window. If nestray is already running, "
             "raises the window and exits. If starting fresh, "
             "skips the initial window minimize.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config = load_config()
    logger.debug = config.getboolean("General", "Debug", fallback=False)

    global _kdotool
    _kdotool = resolve_kdotool(config)
    if _kdotool is None:
        sys.exit(1)

    existing_pid = get_running_pid()

    if existing_pid is not None:
        if args.raise_window:
            logger.log(f"raising thunderbird window")
            raise_thunderbird_window()
        else:
            print(f"nestray is already running with pid {existing_pid}")
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("nestray: system tray not available", file=sys.stderr)
        sys.exit(1)

    nestray = NestrayApp(app, raise_on_start=args.raise_window)  # noqa: F841 — keep reference alive
    sys.exit(app.exec())

def install_application_menu_item_if_necessary():
    desktop_file = "nestray.desktop";
    if sys.platform == "win32" or sys.platform == "darwin":
        logger.log("warning: no menu shortcut will be created - only supported on linux for now")
        return
    home = Path.home()
    target = os.path.join(home, ".local", "share", "applications", desktop_file)
    if os.path.isfile(target):
        logger.log(f".desktop file already found at: {target}")
        return
    my_dir = str(Path(__file__).resolve().parent)
    source = os.path.join(my_dir, desktop_file)
    if not os.path.isfile(source):
        logger.log(f"warning: unable to install desktop file: not found at '{source}'")
        return
    with open(source, "r", encoding="utf-8") as fp:
        source_lines = fp.read().splitlines()

    with open(target, "w", encoding="utf-8", newline=None) as fp:
        for line in source_lines:
            to_write = line.replace("$INSTALL_PATH$", my_dir)
            fp.write(f"{to_write}\n")
    logger.log(f"installed desktop file at: {target}")

if __name__ == "__main__":
    install_application_menu_item_if_necessary()
    main()
