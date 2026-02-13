"""Tests for dingleberries report server."""

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from lib.report_server import (
    generate_done_html,
    generate_html,
    execute_actions,
    human_size,
)


# ── human_size ──────────────────────────────────────────────────────────


class TestHumanSize:
    def test_bytes(self):
        assert human_size(42) == "42 B"

    def test_zero(self):
        assert human_size(0) == "0 B"

    def test_kilobytes(self):
        assert human_size(2048) == "2.0 KB"

    def test_megabytes(self):
        assert human_size(5 * 1024 * 1024) == "5.0 MB"

    def test_gigabytes(self):
        assert human_size(3 * 1024 * 1024 * 1024) == "3.0 GB"

    def test_fractional_kb(self):
        assert human_size(1536) == "1.5 KB"


# ── generate_html ───────────────────────────────────────────────────────


SAMPLE_DATA = {
    "techs_detected": {"python": [], "node": []},
    "repos_scanned": 5,
    "waste_bytes": 12345,
    "gitignore_actions": [
        {"action": "would_create", "repo": "/tmp/repo1", "techs": ["python", "vim"]},
    ],
    "dingleberries": {
        "tracked": [
            {"file": ".DS_Store", "repo": "/tmp/repo1", "size": 6148},
            {"file": "dist/bundle.js", "repo": "/tmp/repo2", "size": 50000},
        ],
        "untracked_no_coverage": [],
        "temporary": [
            {"file": "/tmp/repo1/.DS_Store", "size": 6148},
            {"file": "/tmp/repo2/file.swp", "size": 1024},
        ],
    },
}


class TestGenerateHtml:
    def test_contains_title(self):
        page = generate_html(SAMPLE_DATA, "/apply")
        assert "<title>Dingleberries Report</title>" in page

    def test_self_contained(self):
        """Page should have inline CSS and JS, no external refs."""
        page = generate_html(SAMPLE_DATA, "/apply")
        assert "<style>" in page
        assert "<script>" in page
        assert 'rel="stylesheet"' not in page
        assert 'src="' not in page

    def test_gitignore_checkboxes(self):
        page = generate_html(SAMPLE_DATA, "/apply")
        assert "name='gi:/tmp/repo1'" in page
        assert "Gitignore Updates" in page

    def test_tracked_checkboxes(self):
        page = generate_html(SAMPLE_DATA, "/apply")
        assert "name='tracked:/tmp/repo1:.DS_Store'" in page
        assert "name='tracked:/tmp/repo2:dist/bundle.js'" in page

    def test_temp_checkboxes(self):
        page = generate_html(SAMPLE_DATA, "/apply")
        assert "name='temp:/tmp/repo1/.DS_Store'" in page
        assert "name='temp:/tmp/repo2/file.swp'" in page

    def test_html_escaping(self):
        """Special characters in paths should be escaped."""
        data = {
            "techs_detected": {},
            "repos_scanned": 1,
            "waste_bytes": 0,
            "gitignore_actions": [],
            "dingleberries": {
                "tracked": [],
                "untracked_no_coverage": [],
                "temporary": [
                    {"file": "/tmp/<script>alert(1)</script>", "size": 0},
                ],
            },
        }
        page = generate_html(data, "/apply")
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page

    def test_empty_data_no_actions(self):
        data = {
            "techs_detected": {},
            "repos_scanned": 0,
            "waste_bytes": 0,
            "gitignore_actions": [],
            "dingleberries": {
                "tracked": [], "untracked_no_coverage": [], "temporary": [],
            },
        }
        page = generate_html(data, "/apply")
        assert "No actions needed" in page

    def test_stats_displayed(self):
        page = generate_html(SAMPLE_DATA, "/apply")
        assert "5" in page  # repos scanned
        assert "2" in page  # technologies


# ── generate_done_html ──────────────────────────────────────────────────


class TestGenerateDoneHtml:
    def test_success_counts(self):
        results = {
            "gitignore_updated": 3,
            "files_untracked": 5,
            "files_deleted": 2,
            "errors": [],
        }
        page = generate_done_html(results)
        assert "3" in page
        assert "gitignore" in page
        assert "5" in page
        assert "untracked" in page
        assert "2" in page
        assert "deleted" in page

    def test_auto_close_script(self):
        results = {"gitignore_updated": 0, "files_untracked": 0,
                    "files_deleted": 0, "errors": []}
        page = generate_done_html(results)
        assert "window.close()" in page
        assert "setTimeout" in page

    def test_errors_displayed(self):
        results = {"gitignore_updated": 0, "files_untracked": 0,
                    "files_deleted": 0, "errors": ["Something went wrong"]}
        page = generate_done_html(results)
        assert "Something went wrong" in page

    def test_no_actions_message(self):
        results = {"gitignore_updated": 0, "files_untracked": 0,
                    "files_deleted": 0, "errors": []}
        page = generate_done_html(results)
        assert "No actions were taken" in page


# ── execute_actions ─────────────────────────────────────────────────────


class TestExecuteActions:
    def test_empty_actions_noop(self, tmp_path):
        results = execute_actions({}, tmp_path)
        assert results["gitignore_updated"] == 0
        assert results["files_untracked"] == 0
        assert results["files_deleted"] == 0
        assert results["errors"] == []

    def test_temp_deletion_enforces_safe_to_delete(self, tmp_path):
        """Only files passing safe_to_delete should be removed."""
        # Create a legit temp file
        ds = tmp_path / ".DS_Store"
        ds.write_text("")
        # Create a non-temp file with temp: prefix in action key
        py_file = tmp_path / "important.py"
        py_file.write_text("print('hi')")

        actions = {
            f"temp:{ds}": "1",
            f"temp:{py_file}": "1",
        }
        results = execute_actions(actions, tmp_path)
        assert results["files_deleted"] == 1  # only .DS_Store
        assert not ds.exists()
        assert py_file.exists()  # should NOT be deleted
        assert len(results["errors"]) == 1  # refused important.py

    @patch("lib.report_server.subprocess.run")
    def test_tracked_calls_git_rm_cached(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        actions = {"tracked:/tmp/repo:file.log": "1"}
        results = execute_actions(actions, tmp_path)
        assert results["files_untracked"] == 1
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert "rm" in call_args[0][0]
        assert "--cached" in call_args[0][0]

    @patch("lib.report_server.subprocess.run")
    def test_tracked_git_failure(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=1, stderr="fatal: error")
        actions = {"tracked:/tmp/repo:file.log": "1"}
        results = execute_actions(actions, tmp_path)
        assert results["files_untracked"] == 0
        assert len(results["errors"]) == 1
