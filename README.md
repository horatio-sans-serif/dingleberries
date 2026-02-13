# Dingleberries

https://github.com/horatio-sans-serif/dingleberries

Gitignore hygiene tool. Scans project directories, auto-detects technologies,
fetches appropriate gitignore templates, and maintains both per-project
`.gitignore` files and a global aggregate gitignore.

## The Problem

Projects accumulate files that should be ignored: `node_modules/` committed by
accident, `.DS_Store` scattered everywhere, `__pycache__/` checked in, build
artifacts bloating repos. Many projects lack a `.gitignore` entirely, and when
they have one it's often incomplete for the technologies in use.

## How It Works

### Core Algorithm

```
for each git repo in <scan-path>:

    top_level_files = ls <repo>
    gitignore_templates = detect_templates(top_level_files, MAPPING)
    existing_rules = read .gitignore (if exists)

    write .gitignore:
        # START custom-project-rules
        <existing_rules>       # preserved across re-runs
        # END custom-project-rules

        for template in gitignore_templates:
            # START <template-name>
            <template content>   # replaced on each re-run
            # END <template-name>
```

On the first run, existing `.gitignore` content is wrapped in a
`custom-project-rules` section. On subsequent runs, only the template sections
are regenerated -- the custom section is preserved untouched.

### Technology Detection

Detection is driven by [GitHub Linguist](https://github.com/github-linguist/linguist)'s
`languages.yml`, which defines 800+ programming languages along with their
marker filenames and file extensions. These language names are automatically
mapped to [gitignore.io](https://www.toptal.com/developers/gitignore) template
slugs.

**How the mapping works:**

1. Linguist's `languages.yml` is fetched and cached (30-day TTL)
2. gitignore.io's full slug list is fetched and cached
3. Each Linguist language name is mapped to a gitignore.io slug by:
   - Checking an explicit override table (e.g. `JavaScript` -> `node`,
     `C#` -> `csharp`, `HCL` -> `terraform`)
   - Trying direct lowercase match (e.g. `Python` -> `python`)
   - Trying without spaces (e.g. `Visual Basic` -> `visualbasic`)
   - Trying `#` -> `sharp` and `+` -> `plus` substitutions

4. A blocklist filters out generic/useless matches (`text`, `diff`,
   `archive`, `audio`, `video`, `images`, etc.)
5. Non-language tool markers are added separately (`Dockerfile` -> `docker`,
   `ansible.cfg` -> `ansible`, `Vagrantfile` -> `vagrant`, etc.)

This produces two lookup maps: **filename -> slug** (~153 entries) and
**extension -> slug** (~347 entries).

**Per-repo detection uses two tiers:**

**Tier 1: Filename matches** (high confidence) -- checked first. Files in
the repo root are matched against the filename map. Examples:

- `package.json` -> node
- `Cargo.toml` -> rust
- `go.mod` -> go
- `Gemfile` -> ruby
- `Dockerfile` -> docker

**Tier 2: Extension fallback** -- only used if tier 1 found no
language-specific technologies. File extensions in the repo root are
matched against the extension map. Examples:

- `.py` -> python
- `.rs` -> rust
- `.go` -> go
- `.swift` -> swift

**Always included:** `macos` and `vim` templates are applied to every repo
regardless of detection.

**Override table** (`LINGUIST_NAME_OVERRIDES` in `scanner.py`):

| Linguist name        | gitignore.io slug |
| -------------------- | ----------------- |
| JavaScript           | node              |
| TypeScript           | node              |
| TSX / JSX            | node              |
| C#                   | csharp            |
| F#                   | fsharp            |
| C++                  | c++               |
| Objective-C / C++    | objective-c       |
| Visual Basic .NET    | visualbasic       |
| HCL                  | terraform         |
| TeX                  | tex               |
| PHP                  | composer          |
| Shell                | zsh               |
| Vim Script / Snippet | vim               |
| Emacs Lisp           | emacs             |
| Common Lisp          | commonlisp        |

**Tool markers** (`TOOL_MARKERS` in `scanner.py`) -- non-language files
that Linguist doesn't cover:

| Marker file           | Template  |
| --------------------- | --------- |
| `ansible.cfg`         | ansible   |
| `Vagrantfile`         | vagrant   |
| `Podfile`             | cocoapods |
| `Cartfile`            | carthage  |
| `meson.build`         | meson     |
| `tox.ini`, `Pipfile`  | python    |
| `webpack.config.js`   | node      |
| `vite.config.ts`      | node      |
| `.terraform.lock.hcl` | terraform |

### Template Fetching and Caching

Templates are fetched from [gitignore.io](https://www.toptal.com/developers/gitignore):

```
https://www.toptal.com/developers/gitignore/api/{template-name}
```

The detected slug maps directly to the API path. Fetched templates are
cached at `~/projects/etc/gitignore/{name}.gitignore` with a 30-day TTL.
Subsequent runs use the cache.

Linguist's `languages.yml` and the gitignore.io slug list are also cached
in the same directory with the same TTL (prefixed with `_`).

### Per-Project .gitignore Format

After running, a `.gitignore` looks like this:

```gitignore
# START custom-project-rules
# These are your hand-written rules. Edit freely.
node_modules/
dist/
.env.local
# END custom-project-rules

# START macos
.DS_Store
.AppleDouble
._*
...
# END macos

# START node
logs
*.log
npm-debug.log*
node_modules/
...
# END node

# START vim
[._]*.s[a-v][a-z]
[._]*.sw[a-p]
*~
...
# END vim
```

On re-run, everything between `# START <name>` and `# END <name>` markers
for template sections is replaced with the current cached template. The
`custom-project-rules` section is never modified by the tool.

### Global Aggregate Gitignore

All cached templates are also concatenated into a single aggregate file at
`~/projects/etc/dot-files/gitignore`. This file is what git uses as the
global excludes file via the symlink:

```
~/.config/git/ignore -> ~/projects/etc/dot-files/gitignore
```

The aggregate serves as a safety net: even if a project's `.gitignore` is
missing or incomplete, common patterns (`.DS_Store`, `__pycache__/`,
`node_modules/`, etc.) are caught globally.

## Modes of Operation

### Dry Run (report only)

```bash
dingleberries ~/projects --dry-run
```

Scans everything, shows what `.gitignore` files would be created/updated,
reports dingleberries. Does not modify any project files.

### Normal Run (interactive)

```bash
dingleberries ~/projects
```

- Creates/updates per-project `.gitignore` files
- Updates the global aggregate gitignore
- On first run, offers to symlink `~/.config/git/ignore`
- Reports tracked files that should be ignored
- Generates a commands file (`/tmp/dingleberries-commands-*.sh`) with
  `git rm --cached` entries
- Prompts before executing destructive operations (untracking, deletion)

### Verbose

```bash
dingleberries ~/projects --dry-run --verbose
```

Prints progress to stderr: which repos are being scanned, which templates
are cached vs fetched, detection results.

### Scheduled Report (HTML)

```bash
bin/dingleberries-report ~/projects
```

Runs a dry-run scan, then opens an interactive HTML report in the browser.
The report shows checkboxes for each action (gitignore updates, `git rm
--cached`, temp file deletion). Check or uncheck items, click "Apply
Selected", and only the approved actions are executed.

The report server binds to `127.0.0.1` on a random port (no external
access) and shuts down automatically after 30 minutes of inactivity or
immediately after actions are applied.

**Launchd setup (nightly at 9 PM):**

```bash
# Install
cp etc/com.fictorial.dingleberries.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.fictorial.dingleberries.plist

# Verify
launchctl list | grep dingleberries

# Uninstall
launchctl unload ~/Library/LaunchAgents/com.fictorial.dingleberries.plist
rm ~/Library/LaunchAgents/com.fictorial.dingleberries.plist
```

This is off by default. The plist fires at 9 PM daily and logs to
`~/.dingleberries-report.log`.

## File Layout

```
~/projects/sys/dingleberries/     # This tool
  bin/dingleberries               # Bash CLI (arg parsing, formatting, prompts)
  bin/dingleberries-report        # HTML report launcher (pipes scan to server)
  lib/scanner.py                  # Python core (detection, fetching, scanning)
  lib/report_server.py            # HTTP server for interactive HTML report
  etc/com.fictorial.dingleberries.plist  # Launchd plist (nightly schedule)
  tests/test_scanner.py           # Scanner tests
  tests/test_report_server.py     # Report server tests
  pyproject.toml                  # pathspec dependency, managed by uv

~/projects/etc/gitignore/         # Template cache
  _linguist_languages.yml         # Cached from GitHub Linguist
  _gitignore_slugs.txt            # Cached gitignore.io slug list
  node.gitignore                  # Cached from gitignore.io
  python.gitignore
  rust.gitignore
  macos.gitignore
  vim.gitignore
  ...

~/projects/etc/dot-files/gitignore   # Aggregate (auto-generated)
~/.config/git/ignore                 # Symlink -> aggregate
```

## What It Reports

- **Per-project .gitignore changes**: which repos got new/updated gitignore files
- **Tracked files that should be ignored**: files in git that match ignore
  patterns (with sizes, grouped by repo)
- **Temporary files**: `.DS_Store`, `.swp`, `*~`, `.tmp`, `.bak` etc.
  found anywhere in the scan path
- **Waste**: total disk space consumed by files that should be ignored

## Dependencies

- Python 3.11+ (via `uv`)
- [pathspec](https://pypi.org/project/pathspec/) - gitignore pattern matching
- [pyyaml](https://pypi.org/project/PyYAML/) - parsing Linguist's languages.yml
- `git` - for `ls-files` queries

## Adding New Technologies

Most languages are handled automatically via Linguist. To add or fix
mappings:

- **Language name mismatch**: Add to `LINGUIST_NAME_OVERRIDES` in
  `lib/scanner.py`. Maps a Linguist language name to the correct
  gitignore.io slug.

- **Non-language tool**: Add to `TOOL_MARKERS` in `lib/scanner.py`.
  Maps a marker filename to a gitignore.io slug.

- **False positive slug**: Add to `SLUG_BLOCKLIST` in `lib/scanner.py`
  to prevent a generic gitignore.io template from being used.

Verify a slug exists by visiting:

```
https://www.toptal.com/developers/gitignore/api/<slug>
```

## Safety Guarantees

This tool takes a defense-in-depth approach to prevent data loss. File
deletion is the only destructive operation, and it is guarded at two
independent layers (Python and bash). Both must agree before any file is
removed.

### What can be deleted

Only **temporary files** matching a fixed allowlist of names and extensions:

- Exact names: `.DS_Store`, `._.DS_Store`, `Thumbs.db`, `desktop.ini`
- Extensions: `.swp`, `.swo`, `.swn`, `.tmp`, `.bak`, `.orig`
- Tilde backups: any filename ending in `~`

Nothing else is ever deleted. The `git rm --cached` command only untracks
files from the git index -- it does not delete them from disk.

### What can never be deleted

Both layers independently enforce these rules. A file is **refused** if
any of the following are true:

- It is a **directory** (even if named `.DS_Store`)
- It is a **symlink** (even if it points to a temp file)
- It is **outside the scan root** (path traversal protection)
- It is **inside a `.git` directory**
- Its basename does **not match** the temp file allowlist
- It **does not exist**

### How it works

1. **Python layer** (`safe_to_delete()` in `scanner.py`): validates all
   conditions above before a file path enters the "deletable" list.

2. **Bash layer** (`is_safe_to_delete()` in `bin/dingleberries`):
   re-validates every file path independently at the deletion site, right
   before `rm -f`. Files that fail are logged as `REFUSED` and skipped.

3. **Interactive prompt**: deletion only happens after the user confirms
   with `y` at a `[y/N]` prompt. In `--dry-run` mode, no deletion is
   ever attempted.

### Tests

The `TestSafeToDelete` suite (14 tests) verifies every rejection case:

- Normal files, source code, `.gitignore` -- rejected
- Directories (even named like temp files) -- rejected
- Symlinks (even pointing to temp files) -- rejected
- Paths outside the scan root -- rejected
- Path traversal (`../`) -- rejected
- Files inside `.git` -- rejected
- Non-existent paths -- rejected
- Legitimate temp files in the scan root and subdirectories -- allowed
