"""Bounded post-command nested Git detection, including job-lifetime recovery."""

import os
from pathlib import Path
from unittest.mock import patch

from helpers import TempHome
from cli_subagents.sandbox import launcher
from cli_subagents.sandbox.model import Policy, SandboxError


class NestedRepositoryTests(TempHome):
    def setUp(self):
        super().setUp()
        self.policy = Policy([str(self.project)])

    def check_quarantine(self, baseline, marker):
        with self.assertRaisesRegex(SandboxError, "quarantine"):
            launcher.check_snapshot(self.policy, baseline)
        self.assertFalse(os.path.lexists(marker))
        [saved] = marker.parent.glob(".git.csa-quarantine-*/item")
        return saved

    def test_new_git_directory_in_existing_nested_folder_is_preserved(self):
        folder = self.project / "vendor" / "library"
        folder.mkdir(parents=True)
        baseline = launcher.snapshot(self.policy)
        marker = folder / ".git"
        marker.mkdir()
        (marker / "payload").write_text("fixture", encoding="utf-8")
        self.assertEqual((self.check_quarantine(baseline, marker) / "payload").read_text(), "fixture")

    def test_entire_new_nested_repository_is_detected(self):
        baseline = launcher.snapshot(self.policy)
        marker = self.project / "new" / "library" / ".git"
        marker.mkdir(parents=True)
        (marker / "payload").touch()
        self.assertTrue((self.check_quarantine(baseline, marker) / "payload").exists())
        launcher.check_snapshot(self.policy, baseline)  # quarantine folders are skipped

    def test_git_file_is_moved_without_following_its_gitdir(self):
        baseline = launcher.snapshot(self.policy)
        folder = self.project / "new"
        folder.mkdir()
        marker = folder / ".git"
        marker.write_text("gitdir: ../../outside", encoding="utf-8")
        self.assertEqual(self.check_quarantine(baseline, marker).read_text(), "gitdir: ../../outside")

    def test_existing_repositories_are_left_alone(self):
        marker = self.project / "library" / ".git"
        marker.mkdir(parents=True)
        (marker / "payload").write_text("existing")
        baseline = launcher.snapshot(self.policy)
        launcher.check_snapshot(self.policy, baseline)
        self.assertEqual((marker / "payload").read_text(), "existing")

    def test_additional_roots_are_checked(self):
        additional = self.root / "extra"
        additional.mkdir()
        self.policy = Policy([str(self.project), str(additional)])
        baseline = launcher.snapshot(self.policy)
        marker = additional / "new" / ".git"
        marker.mkdir(parents=True)
        self.check_quarantine(baseline, marker)

    def test_scan_is_bounded_and_does_not_mistake_unvisited_old_repo_for_new(self):
        marker = self.project / "old" / ".git"
        marker.mkdir(parents=True)
        with patch.object(launcher, "NESTED_DIRS", 1):
            baseline = launcher.snapshot(self.policy)
        # A later, larger scan must not quarantine an old, unvisited subtree.
        launcher.check_snapshot(self.policy, baseline)
        self.assertTrue(marker.exists())

    def test_partial_listing_is_not_evidence_of_a_new_child(self):
        marker = self.project / "old" / ".git"
        marker.mkdir(parents=True)
        (self.project / "file").touch()
        with patch.object(launcher, "NESTED_ENTRIES", 1):
            baseline = launcher.snapshot(self.policy)
        launcher.check_snapshot(self.policy, baseline)
        self.assertTrue(marker.exists())

    def test_snapshot_byte_limit_leaves_room_for_gate_context(self):
        for number in range(30):
            (self.project / (str(number) + "x" * 80)).mkdir()
        with patch.object(launcher, "NESTED_BYTES", 1024):
            baseline = launcher.snapshot(self.policy)
        self.assertEqual(baseline["nested"], [])
        launcher.check_snapshot(self.policy, baseline)

    def test_deep_and_dependency_subtrees_are_outside_scan(self):
        baseline = launcher.snapshot(self.policy)
        markers = [self.project / "a" / "b" / "c" / "d" / ".git",
                   self.project / "node_modules" / "library" / ".git"]
        for marker in markers:
            marker.mkdir(parents=True)
        launcher.check_snapshot(self.policy, baseline)
        self.assertTrue(all(marker.exists() for marker in markers))

    def test_directory_reparse_points_are_not_followed(self):
        # Mock the native attribute: no junction privilege needed inside CI.
        folder = self.project / "linked"
        folder.mkdir()
        marker = folder / ".git"
        marker.mkdir()
        real_lstat = Path.lstat

        def lstat(path):
            if path == folder:
                class Reparse:
                    st_file_attributes = 0x400
                return Reparse()
            return real_lstat(path)

        with patch.object(Path, "lstat", lstat):
            rows = launcher._nested_folders(self.policy)
        self.assertNotIn(str(folder), [row["folder"] for row in rows])

    def test_malformed_nested_snapshot_fails_closed(self):
        baseline = launcher.snapshot(self.policy)
        baseline["nested"][0]["folder"] = str(self.root)
        with self.assertRaisesRegex(SandboxError, "invalid nested"):
            launcher.check_snapshot(self.policy, baseline)
