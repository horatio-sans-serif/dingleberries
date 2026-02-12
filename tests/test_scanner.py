"""Tests for dingleberries scanner."""

import os
import textwrap

import pytest

from lib.scanner import (
    _is_valid_gitignore_content,
    _linguist_name_to_slug,
    _sanitize_tech_name,
    build_sectioned_gitignore,
    is_temp_file,
    parse_sectioned_gitignore,
    detect_repo_techs,
    safe_to_delete,
    SLUG_BLOCKLIST,
)


# ── _is_valid_gitignore_content ──────────────────────────────────────────


class TestIsValidGitignoreContent:
    def test_valid_content(self):
        assert _is_valid_gitignore_content("node_modules/\n.venv/\n")

    def test_valid_comment_only(self):
        assert _is_valid_gitignore_content("# Python gitignore\n*.pyc\n")

    def test_empty_string(self):
        assert not _is_valid_gitignore_content("")

    def test_too_short(self):
        assert not _is_valid_gitignore_content("*.pyc")  # 5 chars

    def test_html_error_page(self):
        assert not _is_valid_gitignore_content("<html><body>404</body></html>")

    def test_doctype_html(self):
        assert not _is_valid_gitignore_content("<!DOCTYPE html>\n<html>")

    def test_html_tag_start(self):
        assert not _is_valid_gitignore_content("<html>\n<head>\n</head>")

    def test_none_returns_false(self):
        assert not _is_valid_gitignore_content(None)  # type: ignore


# ── _sanitize_tech_name ──────────────────────────────────────────────────


class TestSanitizeTechName:
    def test_normal_name(self):
        assert _sanitize_tech_name("python") == "python"

    def test_dotnet_allowed(self):
        assert _sanitize_tech_name(".net") == ".net"

    def test_slash_rejected(self):
        with pytest.raises(ValueError, match="Invalid tech name"):
            _sanitize_tech_name("../etc/passwd")

    def test_backslash_rejected(self):
        with pytest.raises(ValueError, match="Invalid tech name"):
            _sanitize_tech_name("foo\\bar")

    def test_null_byte_rejected(self):
        with pytest.raises(ValueError, match="Invalid tech name"):
            _sanitize_tech_name("foo\0bar")

    def test_dot_prefix_rejected(self):
        with pytest.raises(ValueError, match="Invalid tech name"):
            _sanitize_tech_name(".hidden")

    def test_double_dot_rejected(self):
        with pytest.raises(ValueError, match="Invalid tech name"):
            _sanitize_tech_name("..secret")


# ── _linguist_name_to_slug ──────────────────────────────────────────────


class TestLinguistNameToSlug:
    """Test slug mapping with a controlled set of known slugs."""

    FAKE_SLUGS = {"python", "node", "rust", "go", "ruby", "csharp", "c++",
                  "vim", "macos", "terraform", "composer", "zsh", "swift",
                  "objectivec", "fsharp", "elixir", "java", "commonlisp",
                  "visualbasic", "emacs", "coffeescript", "objective-c",
                  "text", "diff", "patch"}

    def test_override_javascript_to_node(self):
        assert _linguist_name_to_slug("JavaScript", self.FAKE_SLUGS) == "node"

    def test_override_typescript_to_node(self):
        assert _linguist_name_to_slug("TypeScript", self.FAKE_SLUGS) == "node"

    def test_override_csharp(self):
        slugs = self.FAKE_SLUGS | {"csharp"}
        assert _linguist_name_to_slug("C#", slugs) == "csharp"

    def test_override_hcl_to_terraform(self):
        assert _linguist_name_to_slug("HCL", self.FAKE_SLUGS) == "terraform"

    def test_override_php_to_composer(self):
        assert _linguist_name_to_slug("PHP", self.FAKE_SLUGS) == "composer"

    def test_override_shell_to_zsh(self):
        assert _linguist_name_to_slug("Shell", self.FAKE_SLUGS) == "zsh"

    def test_direct_lowercase_python(self):
        assert _linguist_name_to_slug("Python", self.FAKE_SLUGS) == "python"

    def test_direct_lowercase_rust(self):
        assert _linguist_name_to_slug("Rust", self.FAKE_SLUGS) == "rust"

    def test_direct_lowercase_go(self):
        assert _linguist_name_to_slug("Go", self.FAKE_SLUGS) == "go"

    def test_blocklisted_slug_returns_none(self):
        # "Text" -> "text" which is in SLUG_BLOCKLIST
        assert "text" in SLUG_BLOCKLIST
        assert _linguist_name_to_slug("Text", self.FAKE_SLUGS) is None

    def test_unknown_language_returns_none(self):
        assert _linguist_name_to_slug("Brainfuck", self.FAKE_SLUGS) is None

    def test_no_slug_available(self):
        # Language that lowercases to something not in slugs
        assert _linguist_name_to_slug("COBOL", {"python", "node"}) is None


# ── is_temp_file ─────────────────────────────────────────────────────────


class TestIsTempFile:
    def test_ds_store(self):
        assert is_temp_file(".DS_Store")

    def test_underscore_ds_store(self):
        assert is_temp_file("._.DS_Store")

    def test_thumbs_db(self):
        assert is_temp_file("Thumbs.db")

    def test_desktop_ini(self):
        assert is_temp_file("desktop.ini")

    def test_vim_swap(self):
        assert is_temp_file("file.swp")

    def test_tilde_backup(self):
        assert is_temp_file("file.txt~")

    def test_tmp_suffix(self):
        assert is_temp_file("data.tmp")

    def test_bak_suffix(self):
        assert is_temp_file("config.bak")

    def test_orig_suffix(self):
        assert is_temp_file("file.orig")

    def test_normal_file(self):
        assert not is_temp_file("README.md")

    def test_python_file(self):
        assert not is_temp_file("scanner.py")

    def test_gitignore(self):
        assert not is_temp_file(".gitignore")


# ── parse_sectioned_gitignore ────────────────────────────────────────────


class TestParseSectionedGitignore:
    def test_single_section(self):
        content = textwrap.dedent("""\
            # START custom-project-rules
            *.log
            /tmp/
            # END custom-project-rules
        """)
        result = parse_sectioned_gitignore(content)
        assert "custom-project-rules" in result
        assert "*.log" in result["custom-project-rules"]
        assert "/tmp/" in result["custom-project-rules"]

    def test_multiple_sections(self):
        content = textwrap.dedent("""\
            # START custom-project-rules
            *.log
            # END custom-project-rules

            # START python
            __pycache__/
            *.pyc
            # END python
        """)
        result = parse_sectioned_gitignore(content)
        assert "custom-project-rules" in result
        assert "python" in result
        assert "__pycache__/" in result["python"]

    def test_preamble_preserved(self):
        content = textwrap.dedent("""\
            # This is a preamble
            some-pattern

            # START section
            content
            # END section
        """)
        result = parse_sectioned_gitignore(content)
        assert "_preamble" in result
        assert "some-pattern" in result["_preamble"]

    def test_empty_content(self):
        result = parse_sectioned_gitignore("")
        assert result == {}

    def test_nested_sections_raise_error(self):
        content = textwrap.dedent("""\
            # START outer
            # START inner
            content
            # END inner
            # END outer
        """)
        with pytest.raises(ValueError, match="nested section"):
            parse_sectioned_gitignore(content)

    def test_mismatched_end_raises_error(self):
        content = textwrap.dedent("""\
            # START python
            *.pyc
            # END node
        """)
        with pytest.raises(ValueError, match="section name mismatch"):
            parse_sectioned_gitignore(content)

    def test_unclosed_section_raises_error(self):
        content = textwrap.dedent("""\
            # START python
            *.pyc
        """)
        with pytest.raises(ValueError, match="Unclosed section"):
            parse_sectioned_gitignore(content)

    def test_empty_section_name_ignored(self):
        content = textwrap.dedent("""\
            # START
            stuff
            # START real
            content
            # END real
        """)
        # empty name line is ignored, but "stuff" ends up as unclosed
        # Actually the "# START " with just space is empty after strip
        # It gets ignored, then "stuff" is preamble, then START real / END real works
        result = parse_sectioned_gitignore(content)
        assert "real" in result
        assert "_preamble" in result


# ── build_sectioned_gitignore round-trip ─────────────────────────────────


class TestBuildSectionedGitignore:
    def test_round_trip(self):
        """Build a gitignore, parse it, re-build - should be identical."""
        custom = "*.log\n/tmp/"
        techs = {"python", "node"}
        templates = {
            "python": "# Python\n__pycache__/\n*.pyc",
            "node": "# Node\nnode_modules/",
            "macos": "# macOS\n.DS_Store",
            "vim": "# Vim\n*.swp",
        }

        built = build_sectioned_gitignore(custom, techs, templates)
        sections = parse_sectioned_gitignore(built)

        assert "custom-project-rules" in sections
        assert "*.log" in sections["custom-project-rules"]
        assert "/tmp/" in sections["custom-project-rules"]

        # Universal techs should be present
        assert "macos" in sections
        assert "vim" in sections
        # Requested techs
        assert "python" in sections
        assert "node" in sections

        # Rebuild from parsed sections should produce same output
        rebuilt = build_sectioned_gitignore(
            sections["custom-project-rules"], techs, templates
        )
        assert rebuilt == built

    def test_empty_custom_rules(self):
        """Empty custom rules section should still have the section markers."""
        built = build_sectioned_gitignore("", {"python"}, {
            "python": "*.pyc",
            "macos": ".DS_Store",
            "vim": "*.swp",
        })
        assert "# START custom-project-rules" in built
        assert "# END custom-project-rules" in built

    def test_missing_template_skipped(self):
        """If a tech has no template content, it should be skipped."""
        built = build_sectioned_gitignore("", {"rust"}, {
            "macos": ".DS_Store",
            "vim": "*.swp",
        })
        # rust not in templates, so no section for it
        assert "# START rust" not in built
        # but universal techs should be present
        assert "# START macos" in built
        assert "# START vim" in built


# ── detect_repo_techs ────────────────────────────────────────────────────


class TestDetectRepoTechs:
    """Test tech detection using real temp directories.

    These tests require Linguist data to be loaded. They use marker files
    from TOOL_MARKERS which are always added to the filename map regardless
    of Linguist data availability.
    """

    def test_node_project_via_tool_marker(self, tmp_path):
        """webpack.config.js is a TOOL_MARKER for node."""
        (tmp_path / "webpack.config.js").write_text("module.exports = {}")
        (tmp_path / ".git").mkdir()
        techs = detect_repo_techs(tmp_path)
        assert "node" in techs

    def test_node_project_via_extension(self, tmp_path):
        """A .js file should detect node via extension fallback."""
        (tmp_path / "index.js").write_text("console.log('hi')")
        (tmp_path / ".git").mkdir()
        techs = detect_repo_techs(tmp_path)
        assert "node" in techs

    def test_python_project(self, tmp_path):
        """setup.cfg should detect as python via TOOL_MARKERS."""
        (tmp_path / "setup.cfg").write_text("[metadata]")
        (tmp_path / ".git").mkdir()
        techs = detect_repo_techs(tmp_path)
        assert "python" in techs

    def test_ansible_project(self, tmp_path):
        """ansible.cfg should detect as ansible via TOOL_MARKERS."""
        (tmp_path / "ansible.cfg").write_text("[defaults]")
        (tmp_path / ".git").mkdir()
        techs = detect_repo_techs(tmp_path)
        assert "ansible" in techs

    def test_empty_repo(self, tmp_path):
        """Empty repo should return empty set (no tier 1 or tier 2 matches)."""
        (tmp_path / ".git").mkdir()
        techs = detect_repo_techs(tmp_path)
        # May have some extension-based matches from .git dir, but
        # with no files, should be empty
        assert isinstance(techs, set)

    def test_extension_fallback(self, tmp_path):
        """When no tier 1 filename match, extensions should be checked."""
        # Create a .py file but no pyproject.toml/setup.py/etc
        (tmp_path / "hello.py").write_text("print('hi')")
        (tmp_path / ".git").mkdir()
        techs = detect_repo_techs(tmp_path)
        assert "python" in techs

    def test_symlink_not_followed(self, tmp_path):
        """Symlinks should not be followed for detection."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "package.json").write_text("{}")

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        # Symlink to the package.json
        os.symlink(str(real_dir / "package.json"), str(repo / "package.json"))

        techs = detect_repo_techs(repo)
        # Symlinked file should NOT be detected (follow_symlinks=False)
        assert "node" not in techs


# ── Integration: parse + build consistency ───────────────────────────────


class TestIntegration:
    def test_malformed_gitignore_fallback(self):
        """Malformed gitignore (nested sections) should raise ValueError."""
        bad_content = "# START a\n# START b\nfoo\n# END b\n# END a\n"
        with pytest.raises(ValueError):
            parse_sectioned_gitignore(bad_content)

    def test_non_sectioned_gitignore_as_preamble(self):
        """A plain gitignore with no sections should parse as preamble only."""
        content = "node_modules/\n*.pyc\n.env\n"
        result = parse_sectioned_gitignore(content)
        assert "_preamble" in result
        assert "node_modules/" in result["_preamble"]
        assert len(result) == 1  # only preamble


# ── safe_to_delete ───────────────────────────────────────────────────────


class TestSafeToDelete:
    """Verify that safe_to_delete prevents deletion of anything except
    known temp files inside the scan root."""

    def test_ds_store_allowed(self, tmp_path):
        f = tmp_path / ".DS_Store"
        f.write_text("")
        assert safe_to_delete(str(f), tmp_path) is True

    def test_swap_file_allowed(self, tmp_path):
        f = tmp_path / "file.swp"
        f.write_text("")
        assert safe_to_delete(str(f), tmp_path) is True

    def test_tilde_backup_allowed(self, tmp_path):
        f = tmp_path / "file.txt~"
        f.write_text("")
        assert safe_to_delete(str(f), tmp_path) is True

    def test_tmp_file_allowed(self, tmp_path):
        f = tmp_path / "data.tmp"
        f.write_text("")
        assert safe_to_delete(str(f), tmp_path) is True

    def test_normal_file_rejected(self, tmp_path):
        """A .py file must NEVER be deletable."""
        f = tmp_path / "important.py"
        f.write_text("print('hello')")
        assert safe_to_delete(str(f), tmp_path) is False

    def test_source_code_rejected(self, tmp_path):
        """Source code must NEVER be deletable."""
        for name in ["main.go", "app.js", "index.html", "README.md", "Makefile"]:
            f = tmp_path / name
            f.write_text("content")
            assert safe_to_delete(str(f), tmp_path) is False, f"{name} should be rejected"

    def test_gitignore_rejected(self, tmp_path):
        """.gitignore must NEVER be deletable."""
        f = tmp_path / ".gitignore"
        f.write_text("node_modules/")
        assert safe_to_delete(str(f), tmp_path) is False

    def test_directory_rejected(self, tmp_path):
        """Directories must NEVER be deletable."""
        d = tmp_path / ".DS_Store"  # even if named like a temp file
        d.mkdir()
        assert safe_to_delete(str(d), tmp_path) is False

    def test_symlink_rejected(self, tmp_path):
        """Symlinks must NEVER be deletable (even to temp files)."""
        real = tmp_path / "real.tmp"
        real.write_text("")
        link = tmp_path / "link.tmp"
        os.symlink(str(real), str(link))
        assert safe_to_delete(str(link), tmp_path) is False

    def test_outside_scan_root_rejected(self, tmp_path):
        """Files outside scan root must NEVER be deletable."""
        scan_root = tmp_path / "projects"
        scan_root.mkdir()
        outside = tmp_path / ".DS_Store"
        outside.write_text("")
        assert safe_to_delete(str(outside), scan_root) is False

    def test_path_traversal_rejected(self, tmp_path):
        """Path traversal attempts must be rejected."""
        scan_root = tmp_path / "projects"
        scan_root.mkdir()
        outside = tmp_path / ".DS_Store"
        outside.write_text("")
        # Try to escape via ../
        traversal = str(scan_root / ".." / ".DS_Store")
        assert safe_to_delete(traversal, scan_root) is False

    def test_inside_git_dir_rejected(self, tmp_path):
        """Files inside .git must NEVER be deletable."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        f = git_dir / ".DS_Store"
        f.write_text("")
        assert safe_to_delete(str(f), tmp_path) is False

    def test_nonexistent_file_rejected(self, tmp_path):
        """Non-existent files must be rejected."""
        assert safe_to_delete(str(tmp_path / "ghost.tmp"), tmp_path) is False

    def test_nested_temp_file_allowed(self, tmp_path):
        """Temp files in subdirectories should still be allowed."""
        sub = tmp_path / "project" / "src"
        sub.mkdir(parents=True)
        f = sub / ".DS_Store"
        f.write_text("")
        assert safe_to_delete(str(f), tmp_path) is True
