import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
from ccnotify import ClaudePromptTracker


def make(known):
    t = ClaudePromptTracker.__new__(ClaudePromptTracker)
    t.open_vscode_windows = lambda: []
    t.known_vscode_workspaces = lambda: [(k, 0) if isinstance(k, str) else k for k in known]
    return t


class TestResolveWorkspace(unittest.TestCase):
    def test_subfolder_resolves_to_open_workspace(self):
        t = make(["/p/app"])
        self.assertEqual(t.resolve_workspace("/p/app/sub/deep"), "/p/app")

    def test_exact_match(self):
        self.assertEqual(make(["/p/app"]).resolve_workspace("/p/app"), "/p/app")

    def test_deepest_wins_without_anchor(self):
        t = make(["/p", "/p/app"])
        self.assertEqual(t.resolve_workspace("/p/app/x"), "/p/app")

    def test_transcript_anchor_prefers_launch_workspace(self):
        t = make(["/p", "/p/app"])
        tp = "/home/u/.claude/projects/-p/abc.jsonl"  # launched in /p
        self.assertEqual(t.resolve_workspace("/p/app/x", tp), "/p")

    def test_prefix_lookalike_not_matched(self):
        t = make(["/p/app"])
        with patch.object(ClaudePromptTracker, "find_workspace_root", return_value="FALLBACK"):
            self.assertEqual(t.resolve_workspace("/p/app-other/x"), "FALLBACK")

    def test_symlink_alias_prefers_earliest_open_window(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        real = os.path.join(d, "real dir")
        os.makedirs(os.path.join(real, "sub"))
        link = os.path.join(d, "link")
        os.symlink(real, link)
        t = make([])
        # user's window (link) opened first; duplicate (real) opened later by a click
        t.open_vscode_windows = lambda: [link, real]
        self.assertEqual(t.resolve_workspace(os.path.join(real, "sub")), link)
        t.open_vscode_windows = lambda: [real, link]
        self.assertEqual(t.resolve_workspace(os.path.join(link, "sub")), real)

    def test_open_windows_beat_history(self):
        t = make(["/p/old"])
        t.open_vscode_windows = lambda: ["/p"]
        self.assertEqual(t.resolve_workspace("/p/old/x"), "/p")

    def test_fallback_to_git_root(self):
        t = make([])
        with patch.object(ClaudePromptTracker, "find_workspace_root", return_value="/g"):
            self.assertEqual(t.resolve_workspace("/g/sub"), "/g")


if __name__ == "__main__":
    unittest.main()
