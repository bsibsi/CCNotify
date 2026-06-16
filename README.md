![Notification Screenshot](alert.jpg)

# CCNotify

> **This is a fork of [dazuiba/CCNotify](https://github.com/dazuiba/CCNotify)** with several behaviour fixes and quality-of-life improvements described in [Fork enhancements](#fork-enhancements).

CCNotify provides desktop notifications for Claude Code, alerting you when Claude needs your input or completes tasks.
## Important Notes
Starting from claude-code v1.0.95 (2025-08-31), any invalid settings in `~/.claude/settings.json` will disable hooks. See [Why not working](#why-not-working) for solutions.

## Features

- 🔔 **Get notified** when Claude requires your input or completes a task.
- 🔗 **Click to jump back** when notifications are clicked, automatically taking you to the corresponding project in VS Code.
- ⏱️ **Task Duration**: Displays started time, and how long the task took to complete
- 🔕 **Focus-aware**: Suppresses notifications when VS Code is already focused on the same workspace

**Note**: Currently compatible with macOS only.


## Installation Guide

### 1. Install CCNotify
```bash
# Create the directory if it doesn't exist
mkdir -p ~/.claude/ccnotify

# soft link ccnotify.py to the directory
ln -f ccnotify.py ~/.claude/ccnotify/

chmod a+x ~/.claude/ccnotify/ccnotify.py

# run this script, should print: ok
~/.claude/ccnotify/ccnotify.py

ok

```
### 2. Install terminal-notifier
ccnotify uses `terminal-notifier` for macOS notifications. Install it using Homebrew:

```bash
brew install terminal-notifier
```

For alternative installation methods and more information, visit: https://github.com/julienXX/terminal-notifier

### 3. Configure Claude Hooks
Add the following hooks to your Claude configuration to enable ccnotify:

 ~/.claude/settings.json 
 
```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/ccnotify/ccnotify.py UserPromptSubmit"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/ccnotify/ccnotify.py Stop"
          }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/ccnotify/ccnotify.py Notification"
          }
        ]
      }
    ],
    "PermissionRequest": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/ccnotify/ccnotify.py PermissionRequest"
          }
        ]
      }
    ]
  }
}
```

## Try It Out

To verify the notification system works, start a new Claude Code session and run:
```
after 1 second, echo 'hello'
```
You should see a macOS notification appear.

## Why not working
1. Ensure hooks configuration is active. Here's an example where other configurations prevent hooks from working:

`claude -p --model haiku -d hooks --verbose "hi"`

Expected output:

```
[DEBUG] Found 1 hook commands to execute
[DEBUG] Executing hook command: ~/.claude/ccnotify/ccnotify.py UserPromptSubmit with timeout 60000ms
[DEBUG] Hook command completed with status 0: ~/.claude/ccnotify/ccnotify.py UserPromptSubmit
```

Actual output:

```
[DEBUG] Invalid settings in userSettings source - key: permissions.allow.0, error:.....
[DEBUG] Found 0 hook commands to execute
```
Reason: In September 2025, claude-code strengthened validation rules for settings.json. Any invalid configuration will disable hooks.
You need to modify the relevant configurations in `~/.claude/settings.json` until the `Invalid settings` error stops appearing.



## How It Works

ccnotify tracks Claude sessions and provides notifications at key moments:

- **When you submit a prompt**: Records the start time and project context
- **When Claude completes**: Calculates duration and sends a completion notification
- **When Claude waits for input**: Immediately alerts you that input is needed

All activity is logged to `~/.claude/ccnotify/ccnotify.log` and session data is stored in `~/.claude/ccnotify/ccnotify.db` locally. No data is uploaded or shared externally.


## Fork enhancements

The following improvements are present in this fork but not in the upstream repository.

### Click notification opens the correct VS Code workspace

**Problem:** Claude Code sometimes navigates into a subfolder while working (e.g. to read a nested file). When a notification fires with that subfolder as `cwd`, clicking it runs `code "<subfolder>"` which opens a new VS Code workspace for the subfolder instead of focusing the already-open project window.

**Fix:** Before opening VS Code, CCNotify now walks up the directory tree from `cwd` to find the nearest `.git` root and opens that path instead. If no git root exists the original path is used as a fallback. VS Code is opened via macOS `open -a "Visual Studio Code"` rather than a hard-coded `code` binary path, which works regardless of how VS Code was installed.

### Suppress notifications for temporary directories

**Problem:** When Claude Code operates on files in system or macOS temp directories (`/tmp`, `/private/tmp`, `/var/folders/…/T/…`), spurious notifications are fired for work the user never cares about.

**Fix:** Notifications are silently suppressed when `cwd` resolves to a recognised temp location:
- `/tmp` and `/private/tmp` (standard Unix temp and its macOS `realpath`)
- `/var/folders/*/*/T/…` (macOS per-user sandbox temp, used by `NSTemporaryDirectory()`)
- Any path whose components include a directory named `tmp` (case-insensitive)

### Enhanced notification type detection

Contributed via [PR #11](https://github.com/dazuiba/CCNotify/pull/11): the `Notification` hook handler now recognises a wider range of Claude messages — permission requests, approval prompts, and generic notifications — and labels each one with an appropriate subtitle ("Permission Required", "Action Required", etc.).

### Log rotation

`ccnotify.log` is rotated daily and the last 7 days of logs are kept, preventing unbounded disk growth.

### Current time in notifications

Each notification subtitle includes a human-readable timestamp (`June 16, 2026 at 14:35`) so you can tell at a glance how long ago an event fired.

### Suppress notifications when VS Code is already focused on the workspace

**Problem:** When you are actively working in the VS Code window where Claude is running, desktop notifications are noise — you can see Claude's output directly.

**Fix:** Before firing a notification, CCNotify checks whether VS Code is the frontmost application *and* its active window is the same workspace the notification targets. If so, the notification is silently suppressed. The check is done entirely without AppleScript:

1. `lsappinfo list` identifies the frontmost application by its bundle ID (`com.microsoft.VSCode`).
2. `~/Library/Application Support/Code/User/workspaceStorage/*/workspace.json` maps each storage directory to a folder URI, locating the storage entry for the current workspace.
3. The `state.vscdb` modification time of that entry is compared against all other open workspace storage directories — the frontmost VS Code window writes to its database most recently.

To disable this behaviour, set `SUPPRESS_IF_VSCODE_FOCUSED = False` near the top of `ccnotify.py`.

---

## Uninstall

Edit `~/.claude/settings.json` and remove all hook commands related to `ccnotify`.

Remove all files with a single command:
```bash
rm -rf ~/.claude/ccnotify
```

