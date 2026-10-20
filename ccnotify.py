#!/usr/bin/env python3
"""
Claude Code Notify
https://github.com/bsibsi/CCNotify
"""

import os
import re
import sys
import json
import shutil
import sqlite3
import subprocess
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime


# Set to True to suppress notifications when VS Code is already focused on the same workspace.
SUPPRESS_IF_VSCODE_FOCUSED = True

# Which CLI tool delivers notifications: "terminal-notifier" (default) or "alerter"
# (https://github.com/vjeantet/alerter). alerter is code-signed and notarized, unlike
# terminal-notifier, and supports a real -app-icon override instead of the -contentImage
# workaround below — but it has no equivalent to -execute, so switching to it drops
# click-notification-to-reopen-VS-Code, and it blocks until the notification is
# dismissed/clicked, so we fire it detached and never confirm delivery synchronously.
NOTIFICATION_BACKEND = "terminal-notifier"

# How long a detached alerter notification stays on screen waiting for a
# dismiss/click before it auto-closes. 0 (the default, matching alerter's
# own default) means it lingers indefinitely until you interact with it or
# it's cleared some other way. Since CCNotify never waits on the process
# either way, raising this only bounds how long the idle background process
# sticks around — it has no effect on hook latency.
ALERTER_TIMEOUT_SECONDS = 0

# Common install locations, used when this hook's inherited PATH doesn't include
# terminal-notifier (e.g. when VS Code fails to resolve the user's shell environment
# and falls back to launchd's bare PATH for the extension host and its subprocesses).
_TERMINAL_NOTIFIER_FALLBACK_PATHS = [
    "/opt/homebrew/bin/terminal-notifier",
    "/usr/local/bin/terminal-notifier",
    "/opt/local/bin/terminal-notifier",
]

_ALERTER_FALLBACK_PATHS = [
    "/opt/homebrew/bin/alerter",
    "/usr/local/bin/alerter",
    "/opt/local/bin/alerter",
]


def resolve_terminal_notifier():
    """Find terminal-notifier via PATH, falling back to common install locations."""
    found = shutil.which("terminal-notifier")
    if found:
        return found
    for candidate in _TERMINAL_NOTIFIER_FALLBACK_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def resolve_alerter():
    """Find alerter via PATH, falling back to common install locations."""
    found = shutil.which("alerter")
    if found:
        return found
    for candidate in _ALERTER_FALLBACK_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


# terminal-notifier 3.x has no way to override a notification's own icon badge
# (macOS removed -appIcon; the icon always comes from the sending app's bundle).
# The closest approximation is -contentImage, which attaches a picture inside
# the notification body. We convert each source app's .icns to PNG on first
# use (contentImage only accepts formats macOS can attach) and cache it.
_ICON_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")

_SOURCE_APP_ICNS = {
    "vscode": "/Applications/Visual Studio Code.app/Contents/Resources/Code.icns",
}


def detect_source_app():
    """Best-effort guess at what launched Claude Code, from inherited env vars."""
    if (
        os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "claude-vscode"
        or os.environ.get("__CFBundleIdentifier") == "com.microsoft.VSCode"
    ):
        return "vscode"
    return None


def resolve_source_app_icon(source_app):
    """Return a cached PNG path for the source app's icon, converting its .icns on first use."""
    icns_path = _SOURCE_APP_ICNS.get(source_app)
    if not icns_path or not os.path.isfile(icns_path):
        return None

    cached_png = os.path.join(_ICON_CACHE_DIR, f"{source_app}.png")
    if os.path.isfile(cached_png):
        return cached_png

    try:
        os.makedirs(_ICON_CACHE_DIR, exist_ok=True)
        subprocess.run(
            ["sips", "-s", "format", "png", icns_path, "--out", cached_png],
            check=True,
            capture_output=True,
        )
        return cached_png if os.path.isfile(cached_png) else None
    except Exception as e:
        logging.debug(f"Icon conversion failed for {source_app}: {e}")
        return None


class ClaudePromptTracker:
    def __init__(self):
        """Initialize the prompt tracker with database setup"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.db_path = os.path.join(script_dir, "ccnotify.db")
        self.setup_logging()
        self.init_database()

    def setup_logging(self):
        """Setup logging to file with daily rotation"""

        script_dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(script_dir, "ccnotify.log")

        # Create a timed rotating file handler
        handler = TimedRotatingFileHandler(
            log_path,
            when="midnight",  # Rotate at midnight
            interval=1,  # Every 1 day
            backupCount=1,  # Keep 1 days of logs
            encoding="utf-8",
        )

        # Set the log format
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        handler.setFormatter(formatter)

        # Configure the root logger
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)

    def init_database(self):
        """Create tables and triggers if they don't exist"""
        with sqlite3.connect(self.db_path) as conn:
            # Create main table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prompt (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    prompt TEXT,
                    cwd TEXT,
                    seq INTEGER,
                    stoped_at DATETIME,
                    lastWaitUserAt DATETIME
                )
            """)

            # Create trigger for auto-incrementing seq
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS auto_increment_seq
                AFTER INSERT ON prompt
                FOR EACH ROW
                BEGIN
                    UPDATE prompt
                    SET seq = (
                        SELECT COALESCE(MAX(seq), 0) + 1
                        FROM prompt
                        WHERE session_id = NEW.session_id
                    )
                    WHERE id = NEW.id;
                END
            """)

            conn.commit()

    def handle_user_prompt_submit(self, data):
        """Handle UserPromptSubmit event - insert new prompt record"""
        session_id = data.get("session_id")
        prompt = data.get("prompt", "")
        cwd = data.get("cwd", "")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO prompt (session_id, prompt, cwd)
                VALUES (?, ?, ?)
            """,
                (session_id, prompt, cwd),
            )
            conn.commit()

        logging.info(f"Recorded prompt for session {session_id}")

    def handle_stop(self, data):
        """Handle Stop event - update completion time and send notification"""
        session_id = data.get("session_id")

        with sqlite3.connect(self.db_path) as conn:
            # Find the latest unfinished record for this session
            cursor = conn.execute(
                """
                SELECT id, created_at, cwd
                FROM prompt
                WHERE session_id = ? AND stoped_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
            """,
                (session_id,),
            )

            row = cursor.fetchone()
            if row:
                record_id, created_at, cwd = row

                # Update completion time
                conn.execute(
                    """
                    UPDATE prompt
                    SET stoped_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """,
                    (record_id,),
                )
                conn.commit()

                # Get seq number and calculate duration
                cursor = conn.execute(
                    "SELECT seq FROM prompt WHERE id = ?", (record_id,)
                )
                seq_row = cursor.fetchone()
                seq = seq_row[0] if seq_row else 1

                duration = self.calculate_duration_from_db(record_id)
                transcript_path = data.get("transcript_path")
                workspace = self.resolve_workspace(cwd, transcript_path)
                self.send_notification(
                    title=os.path.basename(workspace) if workspace else "Claude Task",
                    subtitle=f"job#{seq} done, duration: {duration}",
                    cwd=cwd,
                    transcript_path=transcript_path,
                )

                logging.info(
                    f"Task completed for session {session_id}, job#{seq}, duration: {duration}"
                )

    def handle_notification(self, data):
        """Handle Notification event - check for various notification types and send notifications"""
        session_id = data.get("session_id")
        message = data.get("message", "")
        cwd = data.get("cwd", "")

        # Log all notifications for debugging
        logging.info(f"[NOTIFICATION] session={session_id}, message='{message}'")

        # Determine notification type and subtitle
        message_lower = message.lower()
        subtitle = None
        should_update_db = False
        should_notify = True

        if (
            "waiting for your input" in message_lower
            or "waiting for input" in message_lower
        ):
            subtitle = "Waiting for input"
            should_update_db = True
            should_notify = (
                False  # Suppress notification - Stop handler will send "job done"
            )
        elif "permission" in message_lower:
            subtitle = "Permission Required"
        elif "approval" in message_lower or "choose an option" in message_lower:
            subtitle = "Action Required"
        else:
            # For other notifications, use a generic subtitle
            subtitle = "Notification"

        # Update database for waiting notifications
        if should_update_db:
            with sqlite3.connect(self.db_path) as conn:
                # Fix: Use subquery instead of ORDER BY/LIMIT in UPDATE
                conn.execute(
                    """
                    UPDATE prompt
                    SET lastWaitUserAt = CURRENT_TIMESTAMP
                    WHERE id = (
                        SELECT id FROM prompt
                        WHERE session_id = ?
                        ORDER BY created_at DESC
                        LIMIT 1
                    )
                """,
                    (session_id,),
                )
                conn.commit()
            logging.info(f"Updated lastWaitUserAt for session {session_id}")

        # Send notification only if should_notify is True
        if should_notify:
            workspace = self.resolve_workspace(cwd, data.get("transcript_path"))
            self.send_notification(
                title=os.path.basename(workspace) if workspace else "Claude Task",
                subtitle=subtitle,
                cwd=cwd,
                transcript_path=data.get("transcript_path"),
            )
            logging.info(f"Notification sent for session {session_id}: {subtitle}")
        else:
            logging.info(
                f"Notification suppressed for session {session_id}: {subtitle}"
            )

    def handle_permission_request(self, data):
        """Handle PermissionRequest event - notify user that Claude is asking for permission"""
        session_id = data.get("session_id")
        tool_name = data.get("tool_name", "Unknown tool")
        tool_input = data.get("tool_input", {})
        cwd = data.get("cwd", "")

        # Build a brief description of what the tool wants to do
        detail = None
        if tool_name == "Bash" and "command" in tool_input:
            cmd = tool_input["command"]
            detail = cmd[:80] + ("…" if len(cmd) > 80 else "")
        elif tool_name in ("Write", "Edit", "Read") and "file_path" in tool_input:
            detail = tool_input["file_path"]
        elif "path" in tool_input:
            detail = tool_input["path"]

        subtitle = f"Permission Required: {tool_name}"
        logging.info(
            f"[PERMISSION_REQUEST] session={session_id}, tool={tool_name}, detail={detail}"
        )

        workspace = self.resolve_workspace(cwd, data.get("transcript_path"))
        self.send_notification(
            title=os.path.basename(workspace) if workspace else "Claude Task",
            subtitle=subtitle,
            cwd=cwd,
            transcript_path=data.get("transcript_path"),
        )
        logging.info(f"Permission request handled for {tool_name}")

    def calculate_duration_from_db(self, record_id):
        """Calculate duration for a completed record"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT created_at, stoped_at
                FROM prompt
                WHERE id = ?
            """,
                (record_id,),
            )

            row = cursor.fetchone()
            if row and row[1]:
                return self.calculate_duration(row[0], row[1])

        return "Unknown"

    def calculate_duration(self, start_time, end_time):
        """Calculate human-readable duration between two timestamps"""
        try:
            if isinstance(start_time, str):
                start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            else:
                start_dt = datetime.fromisoformat(start_time)

            if isinstance(end_time, str):
                end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            else:
                end_dt = datetime.fromisoformat(end_time)

            duration = end_dt - start_dt
            total_seconds = int(duration.total_seconds())

            if total_seconds < 60:
                return f"{total_seconds}s"
            elif total_seconds < 3600:
                minutes = total_seconds // 60
                seconds = total_seconds % 60
                if seconds > 0:
                    return f"{minutes}m{seconds}s"
                else:
                    return f"{minutes}m"
            else:
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                if minutes > 0:
                    return f"{hours}h{minutes}m"
                else:
                    return f"{hours}h"
        except Exception as e:
            logging.error(f"Error calculating duration: {e}")
            return "Unknown"

    def is_temp_path(self, path):
        """Return True if path is a system or macOS temp directory."""
        if not path:
            return False
        resolved = os.path.realpath(os.path.abspath(path))
        # /tmp and /private/tmp (macOS symlink pair)
        if resolved.startswith("/tmp") or resolved.startswith("/private/tmp"):
            return True
        # macOS per-user temp: /var/folders/*/*/T/... (realpath resolves /var -> /private/var)
        if resolved.startswith("/var/folders/") or resolved.startswith("/private/var/folders/"):
            parts = resolved.split(os.sep)
            if "T" in parts:
                return True
        # Any path component literally named "tmp" (case-insensitive)
        parts = resolved.split(os.sep)
        if any(p.lower() == "tmp" for p in parts):
            return True
        return False

    def is_vscode_focused_on_workspace(self, workspace_path):
        """Return True if VS Code is the frontmost app and showing this workspace.

        Uses lsappinfo (no osascript) for the frontmost-app check, then reads
        ~/Library/.../workspaceStorage/*/workspace.json to find the storage dir
        for our path, and compares state.vscdb mod-times to identify the active window.
        """
        # Step 1: is VS Code the frontmost app?
        try:
            out = subprocess.run(
                ["lsappinfo", "list"],
                capture_output=True, text=True, timeout=3,
            ).stdout
            if not any('"Code"' in line and "(in front)" in line for line in out.splitlines()):
                return False
        except Exception as e:
            logging.debug(f"lsappinfo error: {e}")
            return False

        # Step 2: find storage dirs for this folder (possibly several spellings,
        # e.g. a symlink and its target) and compare against all other windows.
        target_real = os.path.realpath(workspace_path)
        mtimes = {"ours": [], "others": []}
        for path, mtime in self.known_vscode_workspaces():
            key = "ours" if os.path.realpath(path) == target_real else "others"
            mtimes[key].append(mtime)

        if not mtimes["ours"]:
            # Workspace not found in VS Code's storage — can't confirm it's active
            return False

        # Step 3: is our state DB the most recently modified?
        # The active window writes its DB most recently; background windows write less often.
        if mtimes["others"] and max(mtimes["others"]) > max(mtimes["ours"]):
            return False  # A different workspace was more recently active

        return True

    def find_workspace_root(self, path):
        """Walk up from path to find the git root; fall back to path itself."""
        current = os.path.abspath(path)
        while True:
            if os.path.isdir(os.path.join(current, ".git")):
                return current
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return path

    def known_vscode_workspaces(self):
        """Local folders VS Code has opened, as (path, state_mtime) tuples.

        Read from workspaceStorage/*/workspace.json. The same folder can appear
        under several spellings (e.g. a symlink and its target); the mtime of
        the entry's state.vscdb tells which one is the most recently active.
        """
        from urllib.parse import unquote, urlparse

        ws_storage = os.path.expanduser(
            "~/Library/Application Support/Code/User/workspaceStorage"
        )
        folders = []
        try:
            for entry in os.scandir(ws_storage):
                wj = os.path.join(entry.path, "workspace.json")
                try:
                    with open(wj) as f:
                        uri = json.load(f).get("folder", "")
                    mtime = os.path.getmtime(os.path.join(entry.path, "state.vscdb"))
                except Exception:
                    continue
                if uri.startswith("file://"):
                    folders.append((unquote(urlparse(uri).path).rstrip("/"), mtime))
        except Exception as e:
            logging.debug(f"Workspace scan error: {e}")
        return folders

    def open_vscode_windows(self):
        """Folders of currently open VS Code windows, in the order they were opened.

        Read from globalStorage/storage.json (windowsState.openedWindows).
        """
        from urllib.parse import unquote, urlparse

        path = os.path.expanduser(
            "~/Library/Application Support/Code/User/globalStorage/storage.json"
        )
        try:
            with open(path) as f:
                windows = json.load(f).get("windowsState", {}).get("openedWindows", [])
        except Exception as e:
            logging.debug(f"storage.json read error: {e}")
            return []
        out = []
        for w in windows:
            uri = w.get("folder", "")
            if uri.startswith("file://"):
                out.append(unquote(urlparse(uri).path).rstrip("/"))
        return out

    def resolve_workspace(self, cwd, transcript_path=None):
        """Resolve the VS Code workspace folder that contains cwd.

        Claude's cwd drifts into subfolders when it runs `cd`, so rather than
        guessing from .git, match against the folders VS Code actually has open:
        1. candidates = known VS Code workspaces that are cwd or an ancestor of it
        2. prefer those that also contain the session's launch directory (encoded
           in transcript_path's project dir, which does not change on `cd`)
        3. pick the deepest; fall back to the git-root walk if none match.
        """
        if not cwd:
            return cwd
        real_cwd = os.path.realpath(cwd)

        def contains(path):
            rw = os.path.realpath(path)
            return real_cwd == rw or real_cwd.startswith(rw + os.sep)

        # Prefer folders in windows that are open right now. Among several
        # spellings of one folder (symlink vs target), VS Code treats each as a
        # different workspace, so we must return the spelling of the window that
        # is already open: the earliest-opened one (a later duplicate may have
        # been created by an earlier click-through with the wrong spelling).
        # (path, order): lower order = opened earlier.
        candidates = [
            (p, i) for i, p in enumerate(self.open_vscode_windows()) if contains(p)
        ]
        if not candidates:
            # Not open now: fall back to history, most recently active first
            history = sorted(self.known_vscode_workspaces(), key=lambda x: -x[1])
            candidates = [(p, i) for i, (p, _) in enumerate(history) if p and contains(p)]
        if not candidates:
            return self.find_workspace_root(cwd)

        if transcript_path:
            launch = os.path.basename(os.path.dirname(transcript_path))
            anchored = [
                c for c in candidates
                if any(
                    launch == enc or launch.startswith(enc + "-")
                    for enc in (
                        re.sub(r"[^A-Za-z0-9]", "-", p)
                        for p in (c[0], os.path.realpath(c[0]))
                    )
                )
            ]
            candidates = anchored or candidates

        # Deepest real folder wins; ties go to the earliest-opened spelling.
        return max(candidates, key=lambda c: (len(os.path.realpath(c[0])), -c[1]))[0]

    def send_notification(self, title, subtitle, cwd=None, force=False, transcript_path=None):
        """Send a macOS notification via the configured NOTIFICATION_BACKEND.

        Returns True if the notification was (or is believed to have been)
        delivered, False otherwise (including when suppressed or skipped).
        `force=True` bypasses the temp-path and VS-Code-focus suppression
        checks, used by the `test` subcommand to exercise the real send path.
        """
        from datetime import datetime

        if not force:
            if self.is_temp_path(cwd):
                logging.info(f"Notification suppressed for temp path: {cwd}")
                return False

            if SUPPRESS_IF_VSCODE_FOCUSED and cwd:
                workspace = self.resolve_workspace(cwd, transcript_path)
                if self.is_vscode_focused_on_workspace(workspace):
                    logging.info(
                        f"Notification suppressed: VS Code focused on {workspace}"
                    )
                    return False

        current_time = datetime.now().strftime("%B %d, %Y at %H:%M")
        icon_path = resolve_source_app_icon(detect_source_app())

        if NOTIFICATION_BACKEND == "alerter":
            return self._send_via_alerter(title, subtitle, current_time, icon_path)
        return self._send_via_terminal_notifier(
            title, subtitle, current_time, icon_path,
            self.resolve_workspace(cwd, transcript_path) if cwd else None,
        )

    def vscode_open_command(self, workspace):
        """Shell command that focuses the VS Code window showing `workspace`.

        `open -a "Visual Studio Code" <path>` makes VS Code resolve symlinks, so a
        window opened under a symlinked spelling (e.g. ~/Dropbox/... vs the real
        ~/Library/CloudStorage/Dropbox/...) is never matched and a duplicate window
        opens. The CLI's --folder-uri takes the URI verbatim, so it focuses exactly
        the window opened with this spelling.
        """
        import shlex
        from urllib.parse import quote

        code_cli = shutil.which("code") or (
            "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
        )
        if os.path.exists(code_cli):
            uri = "file://" + quote(workspace)
            return f"{shlex.quote(code_cli)} --folder-uri {shlex.quote(uri)}"
        return f"open -a 'Visual Studio Code' {shlex.quote(workspace)}"

    def _send_via_terminal_notifier(self, title, subtitle, current_time, icon_path, workspace):
        """Send via terminal-notifier: fire-and-forget, with click-to-reopen-VS-Code."""
        notifier = resolve_terminal_notifier()
        if not notifier:
            logging.warning("terminal-notifier not found, notification skipped")
            return False

        try:
            cmd = [
                notifier,
                "-sound",
                "default",
                "-title",
                title,
                "-subtitle",
                subtitle,
                "-message",
                current_time,
            ]

            if icon_path:
                cmd.extend(["-contentImage", icon_path])

            if workspace:
                open_cmd = self.vscode_open_command(workspace)
                cmd.extend(["-execute", open_cmd])
                logging.info(f"Click-through: {open_cmd}")

            result = subprocess.run(cmd, check=False, capture_output=True, text=True)
            if result.returncode != 0:
                logging.error(
                    f"terminal-notifier exited {result.returncode}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
                return False

            logging.info(f"Notification sent: {title} - {subtitle}")
            return True
        except FileNotFoundError:
            logging.warning("terminal-notifier not found, notification skipped")
            return False
        except Exception as e:
            logging.error(f"Error sending notification: {e}")
            return False

    def _send_via_alerter(self, title, subtitle, current_time, icon_path):
        """Send via alerter (https://github.com/vjeantet/alerter).

        alerter blocks until the notification is dismissed or clicked (even
        for a plain message, with no -actions), which does not fit a hook
        that must return quickly. So we launch it detached and never wait on
        it — we only know it was launched, not that it was actually
        delivered, and there is no click-to-reopen-VS-Code support here since
        alerter has no -execute equivalent. ALERTER_TIMEOUT_SECONDS bounds
        how long each detached process lingers if never interacted with.
        """
        alerter = resolve_alerter()
        if not alerter:
            logging.warning("alerter not found, notification skipped")
            return False

        try:
            cmd = [
                alerter,
                "--sound",
                "default",
                "--title",
                title,
                "--subtitle",
                subtitle,
                "--message",
                current_time,
                "--timeout",
                str(ALERTER_TIMEOUT_SECONDS),
            ]

            if icon_path:
                cmd.extend(["--app-icon", icon_path])

            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logging.info(f"Notification launched via alerter: {title} - {subtitle}")
            return True
        except FileNotFoundError:
            logging.warning("alerter not found, notification skipped")
            return False
        except Exception as e:
            logging.error(f"Error sending notification via alerter: {e}")
            return False


def validate_input_data(data, expected_event_name):
    """Validate input data matches design specification"""
    required_fields = {
        "UserPromptSubmit": ["session_id", "prompt", "cwd", "hook_event_name"],
        "Stop": ["session_id", "hook_event_name"],
        "Notification": ["session_id", "message", "hook_event_name"],
        "PermissionRequest": ["session_id", "tool_name", "hook_event_name"],
    }

    if expected_event_name not in required_fields:
        raise ValueError(f"Unknown event type: {expected_event_name}")

    # Check hook_event_name matches expected
    if data.get("hook_event_name") != expected_event_name:
        raise ValueError(
            f"Event name mismatch: expected {expected_event_name}, got {data.get('hook_event_name')}"
        )

    # Check required fields
    missing_fields = []
    for field in required_fields[expected_event_name]:
        if field not in data or data[field] is None:
            missing_fields.append(field)

    if missing_fields:
        raise ValueError(
            f"Missing required fields for {expected_event_name}: {missing_fields}"
        )

    return True


def run_test_notification():
    """Fire a real notification straight through the configured backend,
    bypassing suppression rules, so the user can quickly confirm the whole
    pipeline (binary found, permission granted, icon attached) actually works."""
    print(f"Notification backend: {NOTIFICATION_BACKEND}")

    if NOTIFICATION_BACKEND == "alerter":
        binary = resolve_alerter()
        binary_name = "alerter"
        install_hint = "brew install vjeantet/tap/alerter"
        version_flag = "--version"
    else:
        binary = resolve_terminal_notifier()
        binary_name = "terminal-notifier"
        install_hint = "brew install terminal-notifier"
        version_flag = "-version"

    if not binary:
        print(f"FAILED: {binary_name} not found on PATH or common install locations.")
        print(f"Install it with: {install_hint}")
        sys.exit(1)

    version = subprocess.run(
        [binary, version_flag], capture_output=True, text=True, check=False
    ).stdout.strip()
    print(f"Using {binary_name}: {binary} ({version or 'version unknown'})")

    source_app = detect_source_app()
    print(f"Detected source app: {source_app or 'none (generic)'}")

    if NOTIFICATION_BACKEND == "alerter":
        print(
            "Note: the alerter backend is fire-and-forget — this only confirms the "
            "process launched, not that the notification was actually delivered, "
            "and it does not support click-to-reopen-VS-Code."
        )

    test_cwd = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()

    tracker = ClaudePromptTracker()
    ok = tracker.send_notification(
        title="CCNotify Test",
        subtitle="If you can see this, notifications are working",
        cwd=test_cwd,
        force=True,
    )

    if ok:
        print("Notification sent — check your screen (and Notification Center if you missed it).")
    else:
        print("FAILED to send notification. Check ccnotify.log for details", end="")
        if NOTIFICATION_BACKEND == "alerter":
            print(".")
        else:
            print(", or run:")
            print("  terminal-notifier -diagnose")
        sys.exit(1)


def main():
    """Main entry point - read JSON from stdin and process event"""
    try:
        # Check if hook type is provided as command line argument
        if len(sys.argv) < 2:
            print("ok")
            return

        expected_event_name = sys.argv[1]

        if expected_event_name == "test":
            run_test_notification()
            return

        valid_events = ["UserPromptSubmit", "Stop", "Notification", "PermissionRequest"]

        if expected_event_name not in valid_events:
            logging.error(f"Invalid hook type: {expected_event_name}")
            logging.error(f"Valid hook types: {', '.join(valid_events)}")
            sys.exit(1)

        # Read JSON data from stdin
        input_data = sys.stdin.read().strip()
        if not input_data:
            logging.warning("No input data received")
            return

        data = json.loads(input_data)

        # Validate input data
        validate_input_data(data, expected_event_name)

        tracker = ClaudePromptTracker()

        if expected_event_name == "UserPromptSubmit":
            tracker.handle_user_prompt_submit(data)
        elif expected_event_name == "Stop":
            tracker.handle_stop(data)
        elif expected_event_name == "Notification":
            tracker.handle_notification(data)
        elif expected_event_name == "PermissionRequest":
            tracker.handle_permission_request(data)

    except json.JSONDecodeError as e:
        logging.error(f"JSON decode error: {e}")
        sys.exit(1)
    except ValueError as e:
        logging.error(f"Validation error: {e}")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
