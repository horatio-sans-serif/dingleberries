"""Dingleberries HTML report server.

Reads scan JSON from stdin, serves an interactive HTML report where the user
can approve/deny actions (gitignore updates, git rm --cached, temp file
deletion) with checkboxes, then submit to execute.

Stdlib only -- no additional dependencies required.
"""

import html
import json
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs


SUBPROCESS_TIMEOUT = 30  # seconds


def human_size(nbytes: int) -> str:
    """Format byte count as human-readable size."""
    if nbytes >= 1 << 30:
        return f"{nbytes / (1 << 30):.1f} GB"
    if nbytes >= 1 << 20:
        return f"{nbytes / (1 << 20):.1f} MB"
    if nbytes >= 1 << 10:
        return f"{nbytes / (1 << 10):.1f} KB"
    return f"{nbytes} B"


def generate_html(data: dict, action_url: str) -> str:
    """Build complete HTML page with embedded CSS/JS, checkboxes per action group."""
    gi_actions = data.get("gitignore_actions", [])
    tracked = data.get("dingleberries", {}).get("tracked", [])
    temporary = data.get("dingleberries", {}).get("temporary", [])
    repos_scanned = data.get("repos_scanned", 0)
    waste_bytes = data.get("waste_bytes", 0)
    techs = data.get("techs_detected", {})

    # Group tracked files by repo
    tracked_by_repo: dict[str, list[dict]] = {}
    for item in tracked:
        tracked_by_repo.setdefault(item["repo"], []).append(item)

    parts: list[str] = []
    p = parts.append

    p("<!DOCTYPE html>")
    p("<html lang='en'><head><meta charset='utf-8'>")
    p("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    p("<title>Dingleberries Report</title>")
    p("<style>")
    p("""
*, *::before, *::after { box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f5f5f7; color: #1d1d1f; margin: 0; padding: 24px;
    line-height: 1.5;
}
h1 { font-size: 1.8rem; margin: 0 0 8px; }
h2 { font-size: 1.2rem; margin: 24px 0 12px; color: #333; }
.subtitle { color: #6e6e73; font-size: 0.9rem; margin-bottom: 24px; }
.card {
    background: #fff; border-radius: 12px; padding: 20px;
    margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.stats { display: flex; gap: 24px; flex-wrap: wrap; }
.stat { text-align: center; }
.stat-value { font-size: 1.8rem; font-weight: 700; color: #0071e3; }
.stat-label { font-size: 0.8rem; color: #6e6e73; }
.section-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 8px;
}
.toggle-all {
    font-size: 0.8rem; color: #0071e3; cursor: pointer;
    background: none; border: none; padding: 4px 8px;
}
.toggle-all:hover { text-decoration: underline; }
.item {
    display: flex; align-items: center; gap: 8px; padding: 6px 0;
    border-bottom: 1px solid #f0f0f0;
}
.item:last-child { border-bottom: none; }
.item label { cursor: pointer; flex: 1; font-size: 0.9rem; }
.item .size { color: #6e6e73; font-size: 0.8rem; white-space: nowrap; }
.repo-name {
    font-weight: 600; font-size: 0.95rem; margin-top: 12px;
    padding: 4px 0; color: #1d1d1f;
}
.repo-name:first-child { margin-top: 0; }
.techs { color: #6e6e73; font-size: 0.8rem; font-weight: normal; }
.actions { position: sticky; bottom: 0; background: #f5f5f7; padding: 16px 0; }
.btn {
    background: #0071e3; color: #fff; border: none; border-radius: 8px;
    padding: 12px 32px; font-size: 1rem; cursor: pointer;
    font-weight: 600;
}
.btn:hover { background: #0077ed; }
.btn:disabled { background: #999; cursor: not-allowed; }
.empty { color: #6e6e73; font-style: italic; padding: 8px 0; }
.overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,0.5); backdrop-filter: blur(4px);
    z-index: 1000; justify-content: center; align-items: center;
}
.overlay.active { display: flex; }
.overlay-card {
    background: #fff; border-radius: 16px; padding: 40px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.2); text-align: center;
    max-width: 480px; width: 90%;
}
.overlay-card h2 { font-size: 1.3rem; margin: 0 0 20px; }
.progress-track {
    background: #e8e8ed; border-radius: 8px; height: 12px;
    overflow: hidden; margin: 16px 0;
}
.progress-fill {
    background: #0071e3; height: 100%; border-radius: 8px;
    width: 0%; transition: width 0.3s ease;
}
.progress-label { font-size: 0.9rem; color: #6e6e73; margin-top: 8px; }
.done-result { font-size: 1rem; margin: 8px 0; }
.done-count { font-weight: 700; color: #0071e3; }
.done-error { color: #ff3b30; font-size: 0.9rem; margin-top: 8px; }
.done-close { color: #6e6e73; font-size: 0.85rem; margin-top: 20px; }
.pill {
    display: inline-block; border-radius: 6px; padding: 2px 10px;
    font-size: 0.75rem; font-weight: 600; letter-spacing: 0.02em;
    vertical-align: middle;
}
.pill-create { background: #d1f7e5; color: #0a6f3c; }
.pill-update { background: #fff3cd; color: #7a5d00; }
.count-badge {
    background: #e8e8ed; color: #1d1d1f; border-radius: 10px;
    padding: 2px 8px; font-size: 0.75rem; font-weight: 600;
}
""")
    p("</style></head><body>")

    # Header
    p("<h1>Dingleberries Report</h1>")
    p("<div class='subtitle'>Gitignore hygiene scan results</div>")

    # Summary stats
    p("<div class='card'><div class='stats'>")
    p(f"<div class='stat'><div class='stat-value'>{repos_scanned}</div>")
    p("<div class='stat-label'>Repos Scanned</div></div>")
    p(f"<div class='stat'><div class='stat-value'>{len(techs)}</div>")
    p("<div class='stat-label'>Technologies</div></div>")
    p(f"<div class='stat'><div class='stat-value'>{len(tracked)}</div>")
    p("<div class='stat-label'>Tracked Issues</div></div>")
    p(f"<div class='stat'><div class='stat-value'>{len(temporary)}</div>")
    p("<div class='stat-label'>Temp Files</div></div>")
    p(f"<div class='stat'><div class='stat-value'>{html.escape(human_size(waste_bytes))}</div>")
    p("<div class='stat-label'>Waste</div></div>")
    p("</div></div>")

    p(f"<form method='POST' action='{html.escape(action_url)}' id='actionForm'>")

    # Gitignore actions
    if gi_actions:
        p("<div class='card'>")
        p("<div class='section-header'>")
        p(f"<h2>Gitignore Updates <span class='count-badge'>{len(gi_actions)}</span></h2>")
        p("<button type='button' class='toggle-all' data-group='gi'>Select All</button>")
        p("</div>")
        for action in gi_actions:
            repo = action["repo"]
            act = action["action"]
            techs_str = ", ".join(action.get("techs", []))
            is_create = "create" in act
            pill_class = "pill-create" if is_create else "pill-update"
            pill_text = "Create" if is_create else "Update"
            esc_repo = html.escape(repo)
            cb_name = f"gi:{repo}"
            p("<div class='item'>")
            p(f"<input type='checkbox' name='{html.escape(cb_name)}' value='1' checked class='gi-cb'>")
            p(f"<label><span class='pill {pill_class}'>{pill_text}</span> <code>{esc_repo}/.gitignore</code> <span class='techs'>{html.escape(techs_str)}</span></label>")
            p("</div>")
        p("</div>")

    # Tracked files
    if tracked_by_repo:
        total_tracked = sum(len(v) for v in tracked_by_repo.values())
        p("<div class='card'>")
        p("<div class='section-header'>")
        p(f"<h2>Tracked Files to Untrack <span class='count-badge'>{total_tracked}</span></h2>")
        p("<button type='button' class='toggle-all' data-group='tracked'>Select All</button>")
        p("</div>")
        for repo, items in sorted(tracked_by_repo.items()):
            esc_repo = html.escape(repo)
            repo_size = sum(i["size"] for i in items)
            p(f"<div class='repo-name'>{esc_repo} <span class='techs'>{html.escape(human_size(repo_size))}</span></div>")
            for item in sorted(items, key=lambda x: x["file"]):
                cb_name = f"tracked:{repo}:{item['file']}"
                esc_file = html.escape(item["file"])
                p("<div class='item'>")
                p(f"<input type='checkbox' name='{html.escape(cb_name)}' value='1' checked class='tracked-cb'>")
                p(f"<label><code>{esc_file}</code></label>")
                p(f"<span class='size'>{html.escape(human_size(item['size']))}</span>")
                p("</div>")
        p("</div>")

    # Temporary files
    if temporary:
        p("<div class='card'>")
        p("<div class='section-header'>")
        p(f"<h2>Temporary Files to Delete <span class='count-badge'>{len(temporary)}</span></h2>")
        p("<button type='button' class='toggle-all' data-group='temp'>Select All</button>")
        p("</div>")
        for item in sorted(temporary, key=lambda x: x["file"]):
            cb_name = f"temp:{item['file']}"
            esc_file = html.escape(item["file"])
            p("<div class='item'>")
            p(f"<input type='checkbox' name='{html.escape(cb_name)}' value='1' checked class='temp-cb'>")
            p(f"<label><code>{esc_file}</code></label>")
            p(f"<span class='size'>{html.escape(human_size(item['size']))}</span>")
            p("</div>")
        p("</div>")

    has_actions = bool(gi_actions or tracked_by_repo or temporary)

    if not has_actions:
        p("<div class='card'><div class='empty'>No actions needed. Everything looks clean.</div></div>")

    # Submit button
    if has_actions:
        p("<div class='actions'>")
        p("<button type='submit' class='btn' id='applyBtn'>Apply Selected</button>")
        p("</div>")

    p("</form>")

    # Progress overlay
    p("<div class='overlay' id='overlay'>")
    p("<div class='overlay-card' id='overlayCard'>")
    p("<h2 id='overlayTitle'>Applying Actions...</h2>")
    p("<div class='progress-track'><div class='progress-fill' id='progressFill'></div></div>")
    p("<div class='progress-label' id='progressLabel'>Preparing...</div>")
    p("</div></div>")

    # JavaScript
    p("<script>")
    p("""
const BATCH_SIZE = 100;

document.querySelectorAll('.toggle-all').forEach(btn => {
    btn.addEventListener('click', () => {
        const group = btn.dataset.group;
        const cbs = document.querySelectorAll('.' + group + '-cb');
        const allChecked = Array.from(cbs).every(cb => cb.checked);
        cbs.forEach(cb => { cb.checked = !allChecked; });
        btn.textContent = allChecked ? 'Select All' : 'Deselect All';
    });
});

function showOverlay() {
    document.getElementById('overlay').classList.add('active');
    document.getElementById('actionForm').style.pointerEvents = 'none';
    document.getElementById('actionForm').style.opacity = '0.4';
}

function setProgress(pct, label) {
    document.getElementById('progressFill').style.width = pct + '%';
    document.getElementById('progressLabel').textContent = label;
}

function showDone(totals) {
    const card = document.getElementById('overlayCard');
    let h = '<h2>Actions Complete</h2>';
    if (totals.gitignore_updated)
        h += '<div class="done-result"><span class="done-count">' +
             totals.gitignore_updated + '</span> gitignore file(s) updated</div>';
    if (totals.files_untracked)
        h += '<div class="done-result"><span class="done-count">' +
             totals.files_untracked + '</span> file(s) untracked from git</div>';
    if (totals.files_deleted)
        h += '<div class="done-result"><span class="done-count">' +
             totals.files_deleted + '</span> temp file(s) deleted</div>';
    if (!totals.gitignore_updated && !totals.files_untracked && !totals.files_deleted)
        h += '<div class="done-result">No actions were taken.</div>';
    for (const err of (totals.errors || []))
        h += '<div class="done-error">' + err.replace(/</g,'&lt;') + '</div>';
    h += '<div class="done-close">This tab will close in 5 seconds...</div>';
    card.innerHTML = h;
    setTimeout(() => { fetch('/shutdown', {method:'POST'}); window.close(); }, 5000);
}

function chunk(arr, size) {
    const out = [];
    for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
    return out;
}

async function applyBatched(actionKeys) {
    showOverlay();
    const batches = chunk(actionKeys, BATCH_SIZE);
    const totals = { gitignore_updated: 0, files_untracked: 0, files_deleted: 0, errors: [] };

    for (let i = 0; i < batches.length; i++) {
        const batch = batches[i];
        const actions = {};
        batch.forEach(k => { actions[k] = '1'; });

        setProgress(
            Math.round(((i) / batches.length) * 100),
            'Batch ' + (i + 1) + ' of ' + batches.length +
            ' (' + batch.length + ' actions)...'
        );

        const resp = await fetch('/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ actions })
        });
        const result = await resp.json();
        totals.gitignore_updated += result.gitignore_updated || 0;
        totals.files_untracked += result.files_untracked || 0;
        totals.files_deleted += result.files_deleted || 0;
        totals.errors.push(...(result.errors || []));

        setProgress(
            Math.round(((i + 1) / batches.length) * 100),
            'Completed batch ' + (i + 1) + ' of ' + batches.length
        );
    }

    showDone(totals);
}

document.getElementById('actionForm').addEventListener('submit', (e) => {
    e.preventDefault();
    const cbs = document.querySelectorAll('input[type=checkbox]:checked');
    if (cbs.length === 0) { alert('No actions selected.'); return; }
    if (!confirm('Apply ' + cbs.length + ' selected action(s)?')) return;
    const keys = Array.from(cbs).map(cb => cb.name);
    applyBatched(keys);
});
""")
    p("</script>")
    p("</body></html>")

    return "\n".join(parts)


def generate_done_html(results: dict) -> str:
    """Generate success page with results and auto-close script."""
    gi_count = results.get("gitignore_updated", 0)
    untracked_count = results.get("files_untracked", 0)
    deleted_count = results.get("files_deleted", 0)
    errors = results.get("errors", [])

    parts: list[str] = []
    p = parts.append

    p("<!DOCTYPE html>")
    p("<html lang='en'><head><meta charset='utf-8'>")
    p("<title>Dingleberries - Done</title>")
    p("<style>")
    p("""
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f5f5f7; color: #1d1d1f; margin: 0; padding: 24px;
    display: flex; justify-content: center; align-items: center;
    min-height: 100vh;
}
.done-card {
    background: #fff; border-radius: 16px; padding: 40px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.1); text-align: center;
    max-width: 480px; width: 100%;
}
h1 { font-size: 1.5rem; margin: 0 0 24px; }
.result { font-size: 1rem; margin: 8px 0; }
.count { font-weight: 700; color: #0071e3; }
.error { color: #ff3b30; font-size: 0.9rem; margin-top: 12px; }
.auto-close { color: #6e6e73; font-size: 0.85rem; margin-top: 24px; }
""")
    p("</style></head><body>")
    p("<div class='done-card'>")
    p("<h1>Actions Complete</h1>")

    if gi_count:
        p(f"<div class='result'><span class='count'>{gi_count}</span> gitignore file(s) updated</div>")
    if untracked_count:
        p(f"<div class='result'><span class='count'>{untracked_count}</span> file(s) untracked from git</div>")
    if deleted_count:
        p(f"<div class='result'><span class='count'>{deleted_count}</span> temp file(s) deleted</div>")
    if not (gi_count or untracked_count or deleted_count):
        p("<div class='result'>No actions were taken.</div>")

    for err in errors:
        p(f"<div class='error'>{html.escape(err)}</div>")

    p("<div class='auto-close'>This tab will close in 5 seconds...</div>")
    p("</div>")
    p("<script>setTimeout(() => { window.close(); }, 5000);</script>")
    p("</body></html>")

    return "\n".join(parts)


def execute_actions(actions: dict, scan_root: Path) -> dict:
    """Run approved actions and return result counts.

    actions is a dict of checkbox name -> value from the form POST.
    Names follow the pattern: gi:<repo>, tracked:<repo>:<file>, temp:<path>
    """
    # Import scanner functions for gitignore updates
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from lib import scanner

    results = {
        "gitignore_updated": 0,
        "files_untracked": 0,
        "files_deleted": 0,
        "errors": [],
    }

    # Collect repos needing gitignore updates
    gi_repos: set[str] = set()
    tracked_files: list[tuple[str, str]] = []  # (repo, file)
    temp_files: list[str] = []

    for key in actions:
        if key.startswith("gi:"):
            gi_repos.add(key[3:])
        elif key.startswith("tracked:"):
            # tracked:<repo>:<file>
            rest = key[8:]
            # Repo path may contain colons (unlikely on macOS but be safe)
            # The repo comes from scanner output which uses full paths
            # Split on first colon after the repo path by finding the file
            # We stored as tracked:<repo>:<file> where repo is an absolute path
            # Find the split point: repo is an absolute path starting with /
            # and file is relative, so find the last segment that starts with /
            parts = rest.split(":")
            # Reconstruct: first parts form the repo path, last is the file
            # Since repo is absolute (starts with /) and file is relative,
            # we can split by finding where the relative path begins
            if len(parts) >= 2:
                # Try to find the split: repo paths on macOS don't have colons
                repo = parts[0]
                filepath = ":".join(parts[1:])
                tracked_files.append((repo, filepath))
        elif key.startswith("temp:"):
            temp_files.append(key[5:])

    # Execute gitignore updates
    if gi_repos:
        old_dry_run = scanner.DRY_RUN
        scanner.DRY_RUN = False
        try:
            for repo_path in gi_repos:
                repo = Path(repo_path)
                if not repo.is_dir():
                    results["errors"].append(f"Repo not found: {repo_path}")
                    continue
                try:
                    techs = scanner.detect_repo_techs(repo)
                    tech_templates: dict[str, str] = {}
                    for tech in techs | scanner.UNIVERSAL_TECHS:
                        content, _ = scanner.fetch_template(tech)
                        if content:
                            tech_templates[tech] = content
                    result = scanner.update_project_gitignore(repo, techs, tech_templates)
                    if result["action"] in ("created", "updated"):
                        results["gitignore_updated"] += 1
                    elif result["action"] == "error":
                        results["errors"].append(f"Failed to update gitignore: {repo_path}")
                except Exception as e:
                    results["errors"].append(f"Gitignore error in {repo_path}: {e}")
        finally:
            scanner.DRY_RUN = old_dry_run

    # Execute git rm --cached
    for repo_path, filepath in tracked_files:
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "rm", "--cached", filepath],
                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            )
            if result.returncode == 0:
                results["files_untracked"] += 1
            else:
                results["errors"].append(
                    f"git rm --cached failed for {filepath} in {repo_path}: {result.stderr.strip()}"
                )
        except subprocess.TimeoutExpired:
            results["errors"].append(f"Timeout untracking {filepath} in {repo_path}")
        except Exception as e:
            results["errors"].append(f"Error untracking {filepath}: {e}")

    # Delete temp files
    for filepath in temp_files:
        if not scanner.safe_to_delete(filepath, scan_root):
            results["errors"].append(f"Refused to delete (safety check): {filepath}")
            continue
        try:
            os.unlink(filepath)
            results["files_deleted"] += 1
        except OSError as e:
            results["errors"].append(f"Failed to delete {filepath}: {e}")

    return results


class ReportHandler(BaseHTTPRequestHandler):
    """HTTP handler: GET / serves report, POST /apply executes actions."""

    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        page = generate_html(self.server.scan_data, "/apply")
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/shutdown":
            self._send_json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        if self.path != "/apply":
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")

        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type:
            data = json.loads(body)
            actions = data.get("actions", {})
        else:
            form_data = parse_qs(body)
            actions = {k: v[0] for k, v in form_data.items()}

        results = execute_actions(actions, self.server.scan_root)
        self._send_json(results)

    def _send_json(self, obj: dict):
        body_bytes = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def log_message(self, format, *args):
        """Suppress default request logging."""
        pass


def _find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_server(data: dict, scan_root: str) -> None:
    """Start HTTP server, open browser, serve until done or timeout."""
    port = _find_free_port()
    server = HTTPServer(("127.0.0.1", port), ReportHandler)
    server.scan_data = data
    server.scan_root = Path(scan_root).resolve()

    # Auto-shutdown after 30 minutes if no interaction
    timeout_timer = threading.Timer(1800, server.shutdown)
    timeout_timer.daemon = True
    timeout_timer.start()

    url = f"http://127.0.0.1:{port}/"
    print(f"Report server: {url}", file=sys.stderr)
    webbrowser.open(url)

    try:
        server.serve_forever()
    finally:
        timeout_timer.cancel()
        server.server_close()
        print("Report server stopped.", file=sys.stderr)


def main() -> None:
    """Read JSON from stdin, accept --scan-root arg, run server."""
    scan_root = None
    args = sys.argv[1:]

    i = 0
    while i < len(args):
        if args[i] == "--scan-root" and i + 1 < len(args):
            scan_root = args[i + 1]
            i += 2
        else:
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            sys.exit(1)

    if scan_root is None:
        print("Usage: report_server.py --scan-root <path>", file=sys.stderr)
        print("Reads scan JSON from stdin.", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON on stdin: {e}", file=sys.stderr)
        sys.exit(1)

    run_server(data, scan_root)


if __name__ == "__main__":
    main()
