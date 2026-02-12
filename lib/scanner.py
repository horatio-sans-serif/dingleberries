"""Dingleberries scanner - detect project types, fetch templates, find ignored-file violations.

Technology detection is driven by GitHub Linguist's languages.yml, which maps
800+ languages to their marker filenames and extensions. These language names
are then mapped to gitignore.io template slugs (either automatically via
lowercase matching or via an explicit override table).
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

import pathspec
import yaml

PROJECTS_ROOT = Path.home() / "projects"
TEMPLATE_CACHE_DIR = PROJECTS_ROOT / "etc" / "gitignore"
AGGREGATE_GITIGNORE = PROJECTS_ROOT / "etc" / "dot-files" / "gitignore"
GLOBAL_GIT_IGNORE = Path.home() / ".config" / "git" / "ignore"
LINGUIST_CACHE = TEMPLATE_CACHE_DIR / "_linguist_languages.yml"

TEMPLATE_TTL_SECONDS = 30 * 24 * 3600  # 30 days
NETWORK_TIMEOUT = 15  # seconds
SUBPROCESS_TIMEOUT = 10  # seconds

SKIP_DIRS = frozenset({
    "node_modules", ".venv", "venv", "__pycache__", ".git", ".hg",
    "vendor", "target", "dist", "build", ".next", ".nuxt", ".output",
    ".tox", ".nox", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "htmlcov", ".eggs", "egg-info",
})

# ── Linguist-to-gitignore.io mapping ──────────────────────────────────────
#
# Most Linguist language names map to gitignore.io slugs by lowercasing
# (e.g. "Python" -> "python", "Rust" -> "rust"). This table handles the
# exceptions where the names don't match, or where multiple languages
# should share a single gitignore template.

LINGUIST_NAME_OVERRIDES: dict[str, str] = {
    # Language name -> gitignore.io slug
    "JavaScript": "node",
    "TypeScript": "node",
    "TSX": "node",
    "JSX": "node",
    "CoffeeScript": "coffeescript",
    "C#": "csharp",
    "F#": "fsharp",
    "C++": "c++",
    "Objective-C": "objective-c",
    "Objective-C++": "objective-c",
    "Visual Basic .NET": "visualbasic",
    "HCL": "terraform",
    "TeX": "tex",
    "PHP": "composer",
    "Shell": "zsh",
    "Vim Script": "vim",
    "Vim Snippet": "vim",
    "Emacs Lisp": "emacs",
    "Common Lisp": "commonlisp",
}

# Non-language tools that Linguist doesn't cover but gitignore.io has
# templates for. These are checked as marker files.
TOOL_MARKERS: dict[str, str] = {
    "ansible.cfg": "ansible",
    "playbook.yml": "ansible",
    "Vagrantfile": "vagrant",
    "Gruntfile.js": "grunt",
    "gulpfile.js": "grunt",
    "webpack.config.js": "node",
    "vite.config.js": "node",
    "vite.config.ts": "node",
    "next.config.js": "node",
    "next.config.mjs": "node",
    "nuxt.config.js": "node",
    "nuxt.config.ts": "node",
    ".flake8": "python",
    "tox.ini": "python",
    "setup.cfg": "python",
    "Pipfile": "python",
    "meson.build": "meson",
    "Podfile": "cocoapods",
    "Cartfile": "carthage",
    ".terraform.lock.hcl": "terraform",
}

# Templates to always include regardless of detection
UNIVERSAL_TECHS = frozenset({"macos", "vim"})
ALWAYS_INCLUDE_TECHS = ["macos", "vim"]

# Gitignore.io slugs to skip even if auto-matched (too generic or useless)
SLUG_BLOCKLIST = frozenset({
    "text", "diff", "patch", "archive", "archives", "audio", "video",
    "images", "font", "database", "backup", "compressed", "certificate",
    "certificates", "diskimage", "executable", "spreadsheet",
})

TEMP_PATTERNS = {".DS_Store", "._.DS_Store", "Thumbs.db", "desktop.ini"}
TEMP_SUFFIXES = {".swp", ".swo", ".swn", ".tmp", ".bak", ".orig"}
TEMP_SUFFIX_TILDE = "~"

SECTION_START = "# START {name}"
SECTION_END = "# END {name}"
CUSTOM_SECTION = "custom-project-rules"

VERBOSE = False
DRY_RUN = False

# ── Linguist data loading ─────────────────────────────────────────────────

_linguist_data: dict | None = None
_filename_to_slug: dict[str, str] | None = None
_extension_to_slug: dict[str, str] | None = None
_gi_slugs: set[str] | None = None


def _fetch_url(url: str, timeout: int = NETWORK_TIMEOUT, retries: int = 2) -> str:
    """Fetch URL content with retries and exponential backoff."""
    last_error: Exception | None = None

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "dingleberries/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                return data.decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, UnicodeDecodeError) as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(1 << attempt)  # 1s, 2s backoff
                continue

    raise last_error or RuntimeError(f"Failed to fetch {url}")


def _is_valid_gitignore_content(content: str) -> bool:
    """Check if content looks like a gitignore file (not an HTML error page)."""
    if not content or len(content) < 10:
        return False
    stripped = content.strip()
    if stripped.startswith(("<", "<!DOCTYPE", "<html")):
        return False
    return True


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_gitignore_slugs() -> set[str]:
    """Fetch the list of all available gitignore.io template slugs."""
    global _gi_slugs
    if _gi_slugs is not None:
        return _gi_slugs

    slugs_cache = TEMPLATE_CACHE_DIR / "_gitignore_slugs.txt"
    if slugs_cache.exists():
        age = time.time() - slugs_cache.stat().st_mtime
        if age < TEMPLATE_TTL_SECONDS:
            _gi_slugs = set(slugs_cache.read_text(encoding="utf-8").split())
            return _gi_slugs

    try:
        text = _fetch_url("https://www.toptal.com/developers/gitignore/api/list")
        slugs = set()
        for line in text.splitlines():
            for slug in line.split(","):
                s = slug.strip()
                if s:
                    slugs.add(s)
        _atomic_write(slugs_cache, "\n".join(sorted(slugs)))
        _gi_slugs = slugs
        return slugs
    except (urllib.error.URLError, OSError) as e:
        log(f"  WARN: failed to fetch gitignore.io slug list: {e}")
        if slugs_cache.exists():
            _gi_slugs = set(slugs_cache.read_text(encoding="utf-8").split())
            return _gi_slugs
        _gi_slugs = set()
        return _gi_slugs


def _linguist_name_to_slug(name: str, gi_slugs: set[str]) -> str | None:
    """Map a Linguist language name to a gitignore.io slug.

    Returns None if no valid mapping exists.
    """
    # Check explicit overrides first (authoritative -- no fallthrough)
    if name in LINGUIST_NAME_OVERRIDES:
        slug = LINGUIST_NAME_OVERRIDES[name]
        return slug if slug in gi_slugs else None

    # Try direct lowercase
    slug = name.lower()
    if slug in gi_slugs and slug not in SLUG_BLOCKLIST:
        return slug

    # Try without spaces
    slug = name.lower().replace(" ", "")
    if slug in gi_slugs and slug not in SLUG_BLOCKLIST:
        return slug

    # Try # -> sharp
    slug = name.lower().replace("#", "sharp").replace(" ", "")
    if slug in gi_slugs and slug not in SLUG_BLOCKLIST:
        return slug

    # Try + -> plus
    slug = name.lower().replace("+", "plus").replace(" ", "")
    if slug in gi_slugs and slug not in SLUG_BLOCKLIST:
        return slug

    return None


def _load_linguist() -> tuple[dict[str, str], dict[str, str]]:
    """Load Linguist data and build filename->slug and extension->slug maps.

    Returns (filename_to_slug, extension_to_slug).
    """
    global _linguist_data, _filename_to_slug, _extension_to_slug

    if _filename_to_slug is not None and _extension_to_slug is not None:
        return _filename_to_slug, _extension_to_slug

    # Fetch/cache languages.yml
    if LINGUIST_CACHE.exists():
        age = time.time() - LINGUIST_CACHE.stat().st_mtime
        if age < TEMPLATE_TTL_SECONDS:
            log("  linguist: using cached languages.yml")
            _linguist_data = yaml.safe_load(
                LINGUIST_CACHE.read_text(encoding="utf-8")
            )
        else:
            _linguist_data = None

    if _linguist_data is None:
        url = "https://raw.githubusercontent.com/github-linguist/linguist/main/lib/linguist/languages.yml"
        log("  linguist: fetching languages.yml")
        try:
            text = _fetch_url(url, timeout=30)
            _atomic_write(LINGUIST_CACHE, text)
            _linguist_data = yaml.safe_load(text)
        except (urllib.error.URLError, OSError) as e:
            log(f"  WARN: failed to fetch linguist data: {e}")
            if LINGUIST_CACHE.exists():
                _linguist_data = yaml.safe_load(
                    LINGUIST_CACHE.read_text(encoding="utf-8")
                )
            else:
                _linguist_data = {}

    gi_slugs = _load_gitignore_slugs()

    fname_map: dict[str, str] = {}
    ext_map: dict[str, str] = {}

    lang_data = _linguist_data or {}

    for lang_name, info in lang_data.items():
        if not isinstance(info, dict):
            continue

        slug = _linguist_name_to_slug(lang_name, gi_slugs)
        if not slug:
            continue

        # Map filenames
        for fname in info.get("filenames", []):
            if fname not in fname_map:
                fname_map[fname] = slug

        # Map extensions
        for ext in info.get("extensions", []):
            if ext not in ext_map:
                ext_map[ext] = slug

    # Add tool markers (these override linguist for non-language files)
    # Validate that every marker slug actually exists on gitignore.io
    for fname, slug in TOOL_MARKERS.items():
        if slug not in gi_slugs:
            log(f"  WARN: TOOL_MARKERS[{fname!r}] = {slug!r} not in gitignore.io slugs, skipping")
            continue
        fname_map[fname] = slug

    _filename_to_slug = fname_map
    _extension_to_slug = ext_map

    log(f"  linguist: {len(lang_data)} languages -> {len(fname_map)} filenames, {len(ext_map)} extensions")
    return fname_map, ext_map


# ── Core functions ────────────────────────────────────────────────────────

def log(msg: str) -> None:
    if VERBOSE:
        print(msg, file=sys.stderr)


def is_temp_file(name: str) -> bool:
    if name in TEMP_PATTERNS:
        return True
    if name.endswith(TEMP_SUFFIX_TILDE):
        return True
    _, ext = os.path.splitext(name)
    return ext in TEMP_SUFFIXES


def safe_to_delete(filepath: str, scan_root: Path) -> bool:
    """Defense-in-depth check before any file deletion.

    Returns True ONLY if ALL of these hold:
      1. Path resolves to a regular file (not dir, not symlink)
      2. Path is contained within scan_root (no path traversal)
      3. Basename passes is_temp_file() (re-validated here)
      4. File is not inside a .git directory
    """
    try:
        original = Path(filepath)
        # Check symlink BEFORE resolving (resolve follows symlinks)
        if original.is_symlink():
            return False
        p = original.resolve()
        root = scan_root.resolve()
    except (OSError, ValueError):
        return False

    # Must be under the scan root
    try:
        p.relative_to(root)
    except ValueError:
        return False

    # Must be a regular file, not a directory
    if not p.is_file():
        return False

    # Must not be inside .git
    if ".git" in p.parts:
        return False

    # Basename must match temp file patterns (re-validate)
    return is_temp_file(p.name)


def _sanitize_tech_name(tech: str) -> str:
    """Validate and return a safe tech name for use in file paths."""
    if "/" in tech or "\\" in tech or "\0" in tech:
        raise ValueError(f"Invalid tech name: {tech!r}")
    if tech.startswith(".") and tech != ".net":
        raise ValueError(f"Invalid tech name: {tech!r}")
    return tech


def detect_techs(scan_root: Path) -> dict[str, list[str]]:
    """Walk scan_root detecting project technologies. Returns {tech: [paths]}."""
    fname_map, ext_map = _load_linguist()
    techs: dict[str, list[str]] = {}

    for dirpath, dirnames, filenames in os.walk(scan_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        dp = Path(dirpath)
        found_here: set[str] = set()

        for fname in filenames:
            # Check exact filename match
            if fname in fname_map:
                found_here.add(fname_map[fname])
                continue

            # Check extension match
            _, ext = os.path.splitext(fname)
            if ext in ext_map:
                found_here.add(ext_map[ext])

        for tech in found_here:
            techs.setdefault(tech, []).append(str(dp))

    for tech in ALWAYS_INCLUDE_TECHS:
        techs.setdefault(tech, [])

    return techs


def detect_repo_techs(repo: Path) -> set[str]:
    """Detect technologies used in a repo.

    Checks marker filenames first (high confidence), then falls back to
    file extensions in the repo root if nothing was found.
    Uses os.scandir with follow_symlinks=False to avoid false positives
    from symlinked files.
    """
    fname_map, ext_map = _load_linguist()
    techs: set[str] = set()
    try:
        entries = []
        for entry in os.scandir(repo):
            if entry.is_file(follow_symlinks=False):
                entries.append(entry.name)
    except OSError as e:
        log(f"  WARN: failed to scan {repo}: {e}")
        return techs

    # Tier 1: exact filename matches
    for fname in entries:
        if fname in fname_map:
            techs.add(fname_map[fname])

    # Tier 2: extension-based fallback (only if no language-specific tech found)
    if not (techs - UNIVERSAL_TECHS):
        for fname in entries:
            _, ext = os.path.splitext(fname)
            if ext and ext in ext_map:
                techs.add(ext_map[ext])

    return techs


def fetch_template(tech: str) -> tuple[str, bool]:
    """Fetch a gitignore template. Returns (content, was_fetched).

    Uses atomic writes to prevent cache corruption. Validates that
    fetched content is actually gitignore format (not HTML error page).
    """
    tech = _sanitize_tech_name(tech)
    cache_path = TEMPLATE_CACHE_DIR / f"{tech}.gitignore"

    if cache_path.exists():
        try:
            age = time.time() - cache_path.stat().st_mtime
            if age < TEMPLATE_TTL_SECONDS:
                content = cache_path.read_text(encoding="utf-8")
                if _is_valid_gitignore_content(content):
                    log(f"  cached: {tech} ({int(age / 86400)}d old)")
                    return content, False
                log(f"  WARN: cached template for {tech} appears corrupted, refetching")
        except (OSError, UnicodeDecodeError) as e:
            log(f"  WARN: failed to read cached template for {tech}: {e}")

    url = f"https://www.toptal.com/developers/gitignore/api/{tech}"
    log(f"  fetching: {url}")
    try:
        content = _fetch_url(url)

        if not _is_valid_gitignore_content(content):
            log(f"  WARN: fetched content for {tech} is not valid gitignore, skipping")
            return "", False

        _atomic_write(cache_path, content)
        return content, True
    except (urllib.error.URLError, OSError) as e:
        log(f"  WARN: failed to fetch {tech}: {e}")
        if cache_path.exists():
            try:
                return cache_path.read_text(encoding="utf-8"), False
            except (OSError, UnicodeDecodeError):
                pass
        return "", False


def build_pathspec(patterns_text: str) -> pathspec.PathSpec:
    """Build a PathSpec from gitignore-style pattern text."""
    lines = [
        line for line in patterns_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def find_git_repos(scan_root: Path) -> list[Path]:
    """Find all git repositories under scan_root."""
    repos = []
    for dirpath, dirnames, _ in os.walk(scan_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        dp = Path(dirpath)
        if (dp / ".git").exists():
            repos.append(dp)
    return repos


def git_tracked_files(repo: Path) -> list[str]:
    """Get tracked files in a repo. Returns empty list on error."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-files"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.splitlines() if f]
        log(f"  WARN: git ls-files failed in {repo}: {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        log(f"  WARN: git ls-files timed out in {repo}")
    except FileNotFoundError:
        log("  ERROR: git not found")
    except OSError as e:
        log(f"  WARN: git ls-files error in {repo}: {e}")
    return []


def git_untracked_files(repo: Path) -> list[str]:
    """Get untracked files in a repo. Returns empty list on error."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.splitlines() if f]
        log(f"  WARN: git ls-files --others failed in {repo}: {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        log(f"  WARN: git ls-files --others timed out in {repo}")
    except FileNotFoundError:
        log("  ERROR: git not found")
    except OSError as e:
        log(f"  WARN: git ls-files --others error in {repo}: {e}")
    return []


def file_size(repo: Path, relpath: str) -> int:
    try:
        return (repo / relpath).stat().st_size
    except OSError:
        return 0


# ── Per-project .gitignore management ─────────────────────────────────────

def parse_sectioned_gitignore(content: str) -> dict[str, str]:
    """Parse a sectioned .gitignore into {section_name: content}.

    Sections are delimited by:
      # START section-name
      ...content...
      # END section-name

    Any content outside sections is stored under the key "_preamble".
    Raises ValueError on malformed structure (nested/unclosed sections,
    mismatched END markers).
    """
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []
    preamble_lines: list[str] = []

    for line_num, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()

        if stripped.startswith("# START "):
            section_name = stripped[len("# START "):].strip()
            if not section_name:
                log(f"  WARN: line {line_num}: empty section name, ignoring")
                continue

            if current_section is not None:
                raise ValueError(
                    f"Line {line_num}: nested section '{section_name}' inside "
                    f"'{current_section}'. Missing '# END {current_section}'?"
                )

            current_section = section_name
            current_lines = []
            continue

        if stripped.startswith("# END ") and current_section is not None:
            end_name = stripped[len("# END "):].strip()
            if end_name != current_section:
                raise ValueError(
                    f"Line {line_num}: section name mismatch: "
                    f"'# END {end_name}' but current section is '{current_section}'"
                )
            sections[current_section] = "\n".join(current_lines)
            current_section = None
            current_lines = []
            continue

        if current_section is not None:
            current_lines.append(line)
        else:
            preamble_lines.append(line)

    if current_section is not None:
        raise ValueError(
            f"Unclosed section '{current_section}': "
            f"missing '# END {current_section}'"
        )

    if preamble_lines:
        preamble = "\n".join(preamble_lines).strip()
        if preamble:
            sections["_preamble"] = preamble

    return sections


def build_sectioned_gitignore(custom_rules: str, techs: set[str],
                              tech_templates: dict[str, str]) -> str:
    """Build a complete sectioned .gitignore file."""
    lines: list[str] = []

    start = SECTION_START.format(name=CUSTOM_SECTION)
    end = SECTION_END.format(name=CUSTOM_SECTION)
    lines.append(start)
    if custom_rules.strip():
        lines.append(custom_rules.strip())
    lines.append(end)
    lines.append("")

    all_techs = sorted(UNIVERSAL_TECHS | techs)
    for tech in all_techs:
        content = tech_templates.get(tech, "").strip()
        if content:
            start = SECTION_START.format(name=tech)
            end = SECTION_END.format(name=tech)
            lines.append(start)
            lines.append(content)
            lines.append(end)
            lines.append("")

    return "\n".join(lines) + "\n"


def update_project_gitignore(repo: Path, techs: set[str],
                             tech_templates: dict[str, str]) -> dict:
    """Create or update the .gitignore for a repo using sectioned format.

    Uses atomic writes (temp file + rename) to prevent corruption.
    Preserves the custom-project-rules section across updates.
    """
    gitignore_path = repo / ".gitignore"
    tech_list = sorted(UNIVERSAL_TECHS | techs)
    custom_rules = ""
    existing: str | None = None

    # Read existing content
    try:
        existing = gitignore_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError) as e:
        log(f"  WARN: failed to read {gitignore_path}: {e}")
        return {"action": "error", "repo": str(repo), "techs": tech_list}

    if existing is not None:
        custom_start = SECTION_START.format(name=CUSTOM_SECTION)
        if custom_start in existing:
            try:
                sections = parse_sectioned_gitignore(existing)
                custom_rules = sections.get(CUSTOM_SECTION, "")
            except ValueError as e:
                log(f"  WARN: malformed .gitignore in {repo}: {e}")
                log(f"  WARN: treating entire file as custom rules")
                custom_rules = existing.strip()
        else:
            custom_rules = existing.strip()

    new_content = build_sectioned_gitignore(custom_rules, techs, tech_templates)

    if existing is not None and new_content.rstrip() == existing.rstrip():
        return {"action": "unchanged", "repo": str(repo), "techs": tech_list}

    action = "would_create" if existing is None else "would_update"

    if DRY_RUN:
        return {"action": action, "repo": str(repo), "techs": tech_list}

    try:
        _atomic_write(gitignore_path, new_content)
    except OSError as e:
        log(f"  ERROR: failed to write {gitignore_path}: {e}")
        return {"action": "error", "repo": str(repo), "techs": tech_list}

    actual_action = "created" if existing is None else "updated"
    log(f"  {actual_action} .gitignore: {repo}")
    return {"action": actual_action, "repo": str(repo), "techs": tech_list}


# ── Scanning ──────────────────────────────────────────────────────────────

def build_repo_spec(repo_techs: set[str], tech_templates: dict[str, str]) -> pathspec.PathSpec:
    """Build a pathspec for a specific repo based on its detected technologies."""
    patterns_parts = []

    for tech in sorted(UNIVERSAL_TECHS):
        content = tech_templates.get(tech, "").strip()
        if content:
            patterns_parts.append(content)

    for tech in sorted(repo_techs - UNIVERSAL_TECHS):
        content = tech_templates.get(tech, "").strip()
        if content:
            patterns_parts.append(content)

    # Safety net for common directories not always in templates
    patterns_parts.append("node_modules/\n.venv/\nvenv/\n__pycache__/")

    combined = "\n".join(patterns_parts)
    return build_pathspec(combined)


def scan_repos(repos: list[Path], tech_templates: dict[str, str]) -> dict:
    """Scan repos for dingleberries and update per-project gitignores."""
    tracked_should_ignore = []
    untracked_no_coverage = []
    temporary = []
    waste = 0
    gitignore_actions = []

    for repo in repos:
        log(f"  scanning: {repo}")

        repo_techs = detect_repo_techs(repo)

        if repo_techs:
            result = update_project_gitignore(repo, repo_techs, tech_templates)
            if result["action"] not in ("unchanged", "error"):
                gitignore_actions.append(result)

        repo_spec = build_repo_spec(repo_techs, tech_templates)

        for f in git_tracked_files(repo):
            if repo_spec.match_file(f):
                sz = file_size(repo, f)
                tracked_should_ignore.append({
                    "file": f, "repo": str(repo), "size": sz
                })
                waste += sz
            elif is_temp_file(os.path.basename(f)):
                sz = file_size(repo, f)
                tracked_should_ignore.append({
                    "file": f, "repo": str(repo), "size": sz
                })
                waste += sz

        for f in git_untracked_files(repo):
            name = os.path.basename(f)
            sz = file_size(repo, f)
            if is_temp_file(name):
                temporary.append({"file": str(repo / f), "size": sz})
                waste += sz
            elif not repo_spec.match_file(f):
                untracked_no_coverage.append({
                    "file": f, "repo": str(repo), "size": sz
                })

    return {
        "tracked": tracked_should_ignore,
        "untracked_no_coverage": untracked_no_coverage,
        "temporary": temporary,
        "waste_bytes": waste,
        "gitignore_actions": gitignore_actions,
    }


def find_temp_files_outside_repos(scan_root: Path, repos: set[Path]) -> list[dict]:
    temps = []
    for dirpath, dirnames, filenames in os.walk(scan_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        dp = Path(dirpath)

        in_repo = any(dp == r or str(dp).startswith(str(r) + os.sep) for r in repos)
        if in_repo:
            continue

        for fname in filenames:
            if is_temp_file(fname):
                fp = dp / fname
                try:
                    sz = fp.stat().st_size
                except OSError:
                    sz = 0
                temps.append({"file": str(fp), "size": sz})
    return temps


# ── CLI commands ──────────────────────────────────────────────────────────

def cmd_scan(scan_path: str) -> None:
    scan_root = Path(scan_path).resolve()
    if not scan_root.is_dir():
        print(json.dumps({"error": f"Not a directory: {scan_root}"}))
        sys.exit(1)

    log(f"Detecting technologies in {scan_root}...")
    techs = detect_techs(scan_root)
    log(f"Found technologies: {', '.join(sorted(techs.keys()))}")

    templates_fetched = []
    templates_cached = []
    tech_templates: dict[str, str] = {}

    for tech in sorted(techs.keys()):
        content, was_fetched = fetch_template(tech)
        if content:
            tech_templates[tech] = content
            if was_fetched:
                templates_fetched.append(tech)
            else:
                templates_cached.append(tech)

    log("Finding git repositories...")
    repos = find_git_repos(scan_root)
    log(f"Found {len(repos)} repositories")

    log("Scanning for dingleberries...")
    results = scan_repos(repos, tech_templates)

    repo_set = set(repos)
    extra_temps = find_temp_files_outside_repos(scan_root, repo_set)
    results["temporary"].extend(extra_temps)
    results["waste_bytes"] += sum(t["size"] for t in extra_temps)

    output = {
        "techs_detected": {k: v for k, v in sorted(techs.items())},
        "templates_fetched": templates_fetched,
        "templates_cached": templates_cached,
        "repos_scanned": len(repos),
        "dingleberries": {
            "tracked": results["tracked"],
            "untracked_no_coverage": results["untracked_no_coverage"],
            "temporary": results["temporary"],
        },
        "gitignore_actions": results["gitignore_actions"],
        "waste_bytes": results["waste_bytes"],
    }

    print(json.dumps(output))


def cmd_update_global() -> None:
    TEMPLATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    templates = sorted(TEMPLATE_CACHE_DIR.glob("*.gitignore"))
    if not templates:
        log("No cached templates found. Run 'scan' first.")
        return

    claude_patterns = ""
    source = GLOBAL_GIT_IGNORE
    if source.is_symlink():
        bak = source.parent / "ignore.bak"
        if bak.exists():
            claude_patterns = bak.read_text(encoding="utf-8").strip()
    elif source.exists():
        claude_patterns = source.read_text(encoding="utf-8").strip()

    lines = []
    lines.append("# Global gitignore - auto-generated by dingleberries")
    lines.append("# DO NOT EDIT - regenerate with: dingleberries --update-global")
    lines.append(f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"# Templates: {', '.join(t.stem for t in templates)}")
    lines.append("")

    if claude_patterns:
        lines.append("# ─── Custom patterns ───────────────────────────────────")
        lines.append("")
        lines.append(claude_patterns)
        lines.append("")

    lines.append("# ─── Common directories ────────────────────────────────")
    lines.append("")
    lines.append("node_modules/")
    lines.append(".venv/")
    lines.append("venv/")
    lines.append("")

    for tmpl in templates:
        tech = tmpl.stem
        try:
            content = tmpl.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            log(f"  WARN: failed to read {tmpl}: {e}")
            continue
        lines.append(f"# ─── {tech} ────────────────────────────────────────────")
        lines.append("")
        lines.append(content)
        lines.append("")

    _atomic_write(AGGREGATE_GITIGNORE, "\n".join(lines) + "\n")
    log(f"Updated: {AGGREGATE_GITIGNORE}")
    log(f"  Templates: {len(templates)}")


def main() -> None:
    global VERBOSE, DRY_RUN

    args = sys.argv[1:]
    if not args:
        print("Usage: scanner.py <scan|update-global> [options]", file=sys.stderr)
        sys.exit(1)

    cmd = args[0]

    if "--verbose" in args:
        VERBOSE = True
        args.remove("--verbose")

    if "--dry-run" in args:
        DRY_RUN = True
        args.remove("--dry-run")

    if cmd == "scan":
        if len(args) < 2:
            print("Usage: scanner.py scan <path> [--verbose] [--dry-run]", file=sys.stderr)
            sys.exit(1)
        cmd_scan(args[1])
    elif cmd == "update-global":
        cmd_update_global()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
