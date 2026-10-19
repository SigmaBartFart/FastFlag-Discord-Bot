import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import bot


class HelperTests(unittest.TestCase):
    def test_slash_commands_are_bound_to_bot_instance(self):
        instance = bot.RobloxTrackerBot()
        self.assertIs(instance.setup_command.binding, instance)
        self.assertIs(instance.flagsearch_command.binding, instance)
        self.assertIs(instance.send_missed_updates_command.binding, instance)
        self.assertEqual(
            {command.name for command in instance.tree.get_commands()},
            {"setup", "flagsearch", "send-missed-updates"},
        )

    def test_env_bool_accepts_common_true_values_and_default(self):
        with patch.dict(os.environ, {"FEATURE": " YeS "}, clear=False):
            self.assertTrue(bot.env_bool("FEATURE"))
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(bot.env_bool("MISSING", default=True))
            self.assertFalse(bot.env_bool("MISSING"))

    def test_short_trims_and_marks_truncation(self):
        self.assertEqual(bot.short("  hello  "), "hello")
        self.assertEqual(bot.short("abcdefghijklmnopqrstuvwxyz", limit=25), "abcde\n... (truncated)")

    def test_code_block_escapes_nested_fences(self):
        self.assertEqual(bot.code_block("before ``` after", "diff"), "```diff\nbefore `\u200b`` after\n```")

    def test_fvariable_name_requires_expected_path_shape(self):
        self.assertEqual(
            bot.fvariable_name_from_path("FVariables/DFFlag/A/DFFlagExample.json"),
            "DFFlagExample",
        )
        self.assertIsNone(bot.fvariable_name_from_path("FVariables/DFFlag/DFFlagExample.json"))
        self.assertIsNone(bot.fvariable_name_from_path("Other/DFFlag/A/DFFlagExample.json"))

    def test_parse_fvariable_patch_reads_scalar_old_and_new_values(self):
        patch_text = """@@ -1 +1 @@
-  \"Value\": false,
+  \"Value\": true,
"""
        self.assertEqual(bot.parse_fvariable_patch(patch_text), ("false", "true"))

    def test_parse_fvariable_patch_ignores_headers_and_non_value_lines(self):
        patch_text = """--- a/file.json
+++ b/file.json
@@
 context only
"""
        self.assertEqual(bot.parse_fvariable_patch(patch_text), (None, None))

    def test_extract_application_settings_normalizes_roblox_payload(self):
        self.assertEqual(
            bot.extract_application_settings({"applicationSettings": {"FFlagPacket": "True"}}),
            {"FFlagPacket": "True"},
        )
        self.assertEqual(bot.extract_application_settings({"FFlagPacket": "False"}), {"FFlagPacket": "False"})

    def test_tracker_change_lines_reports_added_removed_and_modified_json_keys(self):
        tracker = bot.GithubFileTracker(unittest.mock.Mock(), unittest.mock.Mock(), unittest.mock.Mock())
        added, removed, modified = tracker.change_lines(
            '{"Keep": "old", "Removed": false}',
            '{"Keep": "new", "Added": true}',
        )
        self.assertEqual(added, ["Added: True"])
        self.assertEqual(removed, ["Removed: False"])
        self.assertEqual(modified, ["Keep: old -> new"])


class StateStoreTests(unittest.TestCase):
    def test_load_save_and_set_round_trip_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            store = bot.StateStore(path)
            with patch("bot.utcnow", return_value="2026-01-01T00:00:00+00:00"):
                store.load()
                self.assertEqual(store.get("created_at"), "2026-01-01T00:00:00+00:00")
                store.set("last_sha", "abc123")

            loaded = bot.StateStore(path)
            loaded.load()
            self.assertEqual(loaded.get("last_sha"), "abc123")
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_load_recovers_a_leftover_tmp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.with_suffix(".tmp").write_text(json.dumps({"last_sha": "recovered"}), encoding="utf-8")
            store = bot.StateStore(path)
            store.load()
            self.assertEqual(store.get("last_sha"), "recovered")
            self.assertTrue(path.exists())


class MockedBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_flag_export_combines_sources_and_filters_case_insensitively(self):
        http = unittest.mock.Mock()
        http.json = AsyncMock(side_effect=[
            {"applicationSettings": {"FFlagPacket": "True", "FFlagOther": "False"}},
            {"applicationSettings": {"DFIntPacketSize": "1200"}},
            {"applicationSettings": {"FFlagPacket": "False"}},
            {"applicationSettings": {"FFlagStudio": "True"}},
        ])
        result = await bot.fetch_flag_export(http, "client", "packet")
        self.assertEqual(result, {"DFIntPacketSize": "1200", "FFlagPacket": "False"})

    async def test_version_at_falls_back_to_ref_when_fetch_fails(self):
        github = unittest.mock.Mock()
        github.raw = AsyncMock(side_effect=OSError("offline"))
        self.assertEqual(await bot.version_at(github, "repo", "abcdef123456789"), "abcdef123456")

    async def test_github_commit_cache_avoids_duplicate_fetches(self):
        http = unittest.mock.Mock()
        http.json = AsyncMock(return_value={"sha": "abc"})
        github = bot.Github(http)

        first = await github.commit("repo", "abc")
        second = await github.commit("repo", "abc")

        self.assertIs(first, second)
        http.json.assert_awaited_once()

    async def test_changed_commits_returns_newest_history_oldest_first(self):
        state = bot.StateStore(Path(tempfile.gettempdir()) / "unused-state-for-test.json")
        state.data = {"last_sha": "old"}
        github = unittest.mock.Mock()
        github.commits = AsyncMock(return_value=[{"sha": "new"}, {"sha": "old"}])
        tracker = bot.CommitTracker(state, unittest.mock.Mock(), github)
        tracker.repo = "repo"
        tracker.branch = "main"
        tracker.key = "last_sha"

        commits = await tracker.changed_commits()

        self.assertEqual([item["sha"] for item in commits], ["new"])
        self.assertEqual(state.get("last_sha"), "new")


if __name__ == "__main__":
    unittest.main()
