import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
from ccnotify import ClaudePromptTracker


def make(known):
    t = ClaudePromptTracker.__new__(ClaudePromptTracker)
    t.known_vscode_workspaces = lambda: known
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

    def test_fallback_to_git_root(self):
        t = make([])
        with patch.object(ClaudePromptTracker, "find_workspace_root", return_value="/g"):
            self.assertEqual(t.resolve_workspace("/g/sub"), "/g")


if __name__ == "__main__":
    unittest.main()
