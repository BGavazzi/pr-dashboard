#!/usr/bin/env python3
"""Vertical PR dashboard — narrow column designed to dock on the side of an
ultrawide monitor. Reads from GitHub via the already-authenticated `gh` CLI.

No embedded credentials: uses `gh auth` (token from the OS keyring). Works on
any machine where `gh auth status` shows logged in.

Usage:
    python pr_dashboard.py                  # single render (my open PRs)
    python pr_dashboard.py --watch          # refresh every 60s + key input (Ctrl+C to quit)
    python pr_dashboard.py --watch 30       # refresh every 30s
    python pr_dashboard.py --no-rich        # skip CI/review — 1 API call only, fast
    python pr_dashboard.py --org ORG        # whole org (not just mine)
    python pr_dashboard.py --review-requested  # PRs awaiting MY review
    python pr_dashboard.py --ready          # only ready to merge (approved+CI+no conflict)
    python pr_dashboard.py --conflicts      # only with merge conflicts
    python pr_dashboard.py --no-builds      # skip running builds panel (1 fewer call/repo)
    python pr_dashboard.py --builds-repo O/R  # repo to watch for builds (repeatable)
    python pr_dashboard.py --no-usage       # skip Claude Code usage panel
    python pr_dashboard.py --antigravity    # opt-in Google Antigravity quota panel (see below)
    python pr_dashboard.py --clear-hidden   # restore all hidden PRs and exit

Stale worktree reaper (cleans disk; separate mode, not part of --watch):
    python pr_dashboard.py --worktrees        # DRY RUN: shows what's reapable and why
    python pr_dashboard.py --reap-worktrees   # REAPS reapable ones (PR merged + 0 ahead
                                              #   of remote + clean working tree)
    python pr_dashboard.py --reap-worktrees --reap-limit 5  # reap at most N per run
    python pr_dashboard.py --worktrees --reap-root C:\\dir   # alternate root (env: PR_DASH_WT_ROOT)
  Protects live checkouts (name *-main / *homolog* / *prod* + ~/.pr-dashboard-keep.json).
  Each removal is logged to ~/.pr-dashboard-reaped.json with a recreate command (fallback).

Desktop notifications (fired on state change between watch cycles):
    --notify               enable notifications (auto-selects backend by OS)
    --no-notify            disable (default)
  Fires when a PR moves to CLEAN (ready to merge) or CI flips to failure.
  Backends: notify-send (Linux), osascript (macOS), PowerShell BurntToast (Windows).

Keys in --watch mode (TTY):
    space                  → reload now from GitHub (reload button)
    a / m / c             → filter: all / ready to merge / with conflicts
    letter/number of card  → hide that PR (persistent)
    r                      → restore all hidden
    q                      → quit

Each card shows: age · CI (✓ ok / ✗ failed / ⋯ running) · review · merge state.
When a build is in progress → "⟳ build running" line.
Requested reviewer who hasn't reviewed yet → "⏳ waiting <names>" line.

"RUNNING BUILDS" panel: workflow runs in progress on watched repos —
staging (push→main) AND prod release (push→tag). These are NOT PR checks,
so the query is separate (`gh run list`). Override with --builds-repo or
env PR_DASH_BUILD_REPOS="owner/a,owner/b". Default = empty (configure via --builds-repo).

"USAGE" panel: Claude Code session (5h) and week (7d) usage %, read from the
OAuth token Claude Code already keeps at ~/.claude/.credentials.json — no
separate login. Read-only: never refreshes or writes back the token (that's
the CLI's job), so a stale/expired token just makes the panel go quiet
instead of erroring. Disable with --no-usage.

"ANTIGRAVITY" panel (opt-in, --antigravity): Google Antigravity quota. Unlike
Claude Code there is no readable token file, so this is best-effort by
construction — two undocumented/reverse-engineered paths, tried in order:
  1. Local mode — detects the running Antigravity IDE's language-server
     process, pulls its --csrf_token/--extension_server_port, and calls its
     local RPC directly. Zero new credentials, but only works while the
     Antigravity IDE window is open.
  2. CLI fallback — shells out to `antigravity-usage --json` if that npm
     tool (github.com/skainguyen1412/antigravity-usage) is installed and
     logged in; it owns its own OAuth token store, separate from this script.
Both can break silently on an Antigravity update — every step fails quiet
and the panel just doesn't appear. Off by default; pass --antigravity to try it.

See full guide in scripts/pr-dashboard.md.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

def _col_width():
    """Use --width N if given, else current terminal columns (clamped 24–120), else 34."""
    if "--width" in sys.argv:
        i = sys.argv.index("--width")
        if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit():
            return max(24, min(120, int(sys.argv[i + 1])))
    cols = shutil.get_terminal_size(fallback=(0, 0)).columns
    return max(24, min(120, cols)) if cols else 34

W = _col_width()
RICH = "--no-rich" not in sys.argv
BUILDS = "--no-builds" not in sys.argv
USAGE = "--no-usage" not in sys.argv
ANTIGRAVITY = "--antigravity" in sys.argv
NOTIFY = "--notify" in sys.argv
LABELS = "123456789bdefghijklnopstuvwxyz"  # excludes a/c/m/q/r (command keys)
HIDDEN_FILE = os.path.join(os.path.expanduser("~"), ".pr-dashboard-hidden.json")
CREDS_FILE = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# The endpoint rate-limits hard without a Claude Code User-Agent; exact patch
# version isn't validated, a stable recent one is fine.
USAGE_USER_AGENT = "claude-code/2.1.183"

# Windows console often defaults to cp1252 — force utf-8 for glyphs/accents
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


class C:
    RESET = "\033[0m"; DIM = "\033[2m"; BOLD = "\033[1m"
    RED = "\033[91m"; GRN = "\033[92m"; YEL = "\033[93m"
    CYN = "\033[96m"; GRY = "\033[90m"; WHT = "\033[97m"; BLU = "\033[94m"


def _notify(title, body):
    """Fire a desktop notification if --notify is set. Best-effort, never crashes."""
    if not NOTIFY:
        return
    try:
        if os.name == "nt":
            script = (
                "Import-Module BurntToast -ErrorAction SilentlyContinue; "
                f"New-BurntToastNotification -Text '{title}','{body}'"
            )
            subprocess.Popen(["powershell", "-WindowStyle", "Hidden", "-Command", script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["osascript", "-e",
                 f'display notification "{body}" with title "{title}"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["notify-send", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _state_snapshot(rows):
    """Lightweight snapshot: pr_key → (mergeStateStatus, CI conclusion) for diffing."""
    snap = {}
    for p, repo, rich in rows:
        key = pr_key(repo, p["number"])
        ci_states = [(c.get("conclusion") or c.get("state") or "").upper()
                     for c in (rich.get("statusCheckRollup") or [])]
        failed = any(s in ("FAILURE", "ERROR", "TIMED_OUT") for s in ci_states)
        snap[key] = (rich.get("mergeStateStatus"), failed)
    return snap


def _diff_notify(prev, curr, rows):
    """Compare snapshots and fire notifications for meaningful state changes."""
    if not NOTIFY or prev is None:
        return
    # Build title lookup
    titles = {pr_key(repo, p["number"]): (p["title"][:40], repo.split("/")[-1])
              for p, repo, _ in rows}
    for key, (merge, failed) in curr.items():
        prev_merge, prev_failed = prev.get(key, (None, None))
        title, repo = titles.get(key, ("PR", ""))
        if merge == "CLEAN" and prev_merge != "CLEAN":
            _notify(f"✅ Ready to merge — {repo}", title)
        elif failed and not prev_failed:
            _notify(f"❌ CI failed — {repo}", title)


def _resolve_token():
    """Read the token ONCE from the keyring (via `gh auth token`) and inject it
    as GH_TOKEN into subsequent calls. This way each `gh` call does NOT reopen
    the Windows Credential Manager — which under burst load (--watch fires N `gh`
    processes per refresh) occasionally fails and sends the request without a
    token → 401."""
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, encoding="utf-8")
        tok = (r.stdout or "").strip()
        if tok:
            return tok
    except OSError:
        pass
    return None  # no token → falls back to legacy per-call keyring behaviour


_TOKEN = _resolve_token()
_ENV = {**os.environ, "GH_TOKEN": _TOKEN} if _TOKEN else None


def gh(args):
    last = ""
    for attempt in range(3):
        r = subprocess.run(["gh", *args], capture_output=True, text=True,
                           encoding="utf-8", env=_ENV)
        if r.returncode == 0:
            return json.loads(r.stdout)
        last = r.stderr.strip()
        # transient 401 (keyring/refresh) → retry with short backoff;
        # any other error → fail immediately without retrying.
        if "401" not in last and "authentication" not in last.lower():
            break
        time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(last)


# ── hidden PR store ─────────────────────────────────────────────────────────

def pr_key(repo, num):
    return f"{repo}#{num}"


def load_hidden():
    try:
        with open(HIDDEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, ValueError, OSError):
        return set()


def save_hidden(hidden):
    try:
        with open(HIDDEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(hidden), f)
    except OSError:
        pass


# ── non-blocking input (cross-platform) ─────────────────────────────────────

def wait_key(timeout):
    """Wait for a keypress for up to `timeout` seconds. Returns the char or None."""
    if os.name == "nt":
        import msvcrt
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):  # arrow/F-keys: consume 2nd byte
                    msvcrt.getwch()
                    return None
                return ch
            time.sleep(0.05)
        return None
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if r:
            return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return None


# ── formatting ───────────────────────────────────────────────────────────────

def age(created_iso):
    created = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - created).days
    if days == 0:
        return "today", C.GRN
    if days < 3:
        return f"{days}d", C.GRN
    if days < 14:
        return f"{days}d", C.YEL
    return f"{days}d", C.RED


def _fmt_reset(iso):
    """'13:30' if the reset is today, else 'Fri 13:30'. Empty string on bad input."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ""
    fmt = "%H:%M" if dt.date() == datetime.now().astimezone().date() else "%a %H:%M"
    return dt.strftime(fmt)


def usage_col(pct):
    if pct >= 80:
        return C.RED
    if pct >= 50:
        return C.YEL
    return C.GRN


def ci_status(rollup):
    """Returns (glyph, running) — `running` is True if a build is in progress."""
    if not rollup:
        return f"{C.GRY}·{C.RESET}", False
    states = [(c.get("conclusion") or c.get("state") or "").upper() for c in rollup]
    if any(s in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED") for s in states):
        return f"{C.RED}✗{C.RESET}", False
    if any(s in ("PENDING", "IN_PROGRESS", "QUEUED", "") for s in states):
        return f"{C.YEL}⋯{C.RESET}", True
    return f"{C.GRN}✓{C.RESET}", False


def pending_reviewers(rich):
    """Logins/teams of requested reviewers who haven't reviewed yet."""
    out = []
    for r in rich.get("reviewRequests") or []:
        out.append(r.get("login") or r.get("name") or r.get("slug") or "?")
    return out


def review_label(decision):
    return {
        "APPROVED": (C.GRN, "approved"),
        "CHANGES_REQUESTED": (C.RED, "changes"),
        "REVIEW_REQUIRED": (C.YEL, "review"),
    }.get(decision or "", (C.GRY, "no review"))


def link(url, text):
    """Terminal hyperlink (OSC 8) — Ctrl+click opens in browser."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def wrap(text, width):
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + (1 if line else 0) > width:
            if line:
                out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out or [""]


# ── fetch + render ────────────────────────────────────────────────────────────

def search_args():
    """Build the `gh search prs` filter from CLI flags."""
    args = ["search", "prs", "--state", "open", "--limit", "100",
            "--json", "number,title,repository,createdAt,url,isDraft"]
    if "--review-requested" in sys.argv:
        args[2:2] = ["--review-requested", "@me"]
    else:
        args[2:2] = ["--author", "@me"]
    if "--org" in sys.argv:
        i = sys.argv.index("--org")
        if i + 1 < len(sys.argv):
            args[2:2] = ["--owner", sys.argv[i + 1]]
    return args


def fetch():
    prs = gh(search_args())
    prs.sort(key=lambda p: p["createdAt"])  # oldest first
    rows = []
    for p in prs:
        repo = p["repository"]["nameWithOwner"]
        rich = {}
        if RICH:
            try:
                rich = gh(["pr", "view", str(p["number"]), "--repo", repo, "--json",
                           "reviewDecision,statusCheckRollup,additions,deletions,"
                           "mergeStateStatus,reviewRequests"])
            except RuntimeError:
                rich = {}
        rows.append((p, repo, rich))
    return rows


def build_repos():
    """Watched repos for builds: --builds-repo (repeatable) > env PR_DASH_BUILD_REPOS > empty default."""
    repos = [sys.argv[i + 1] for i, a in enumerate(sys.argv)
             if a == "--builds-repo" and i + 1 < len(sys.argv)]
    if repos:
        return repos
    env = os.environ.get("PR_DASH_BUILD_REPOS", "").strip()
    if env:
        return [r.strip() for r in env.split(",") if r.strip()]
    return []


ACTIVE_RUN = ("in_progress", "queued", "requested", "waiting", "pending")


def fetch_builds():
    """Workflow runs in progress — staging (push→main) AND prod release (push→tag);
    neither shows up as a check on an open PR, so the query is separate."""
    out = []
    for repo in build_repos():
        repo = repo.strip()
        if not repo:
            continue
        try:
            runs = gh(["run", "list", "--repo", repo, "--limit", "20", "--json",
                       "status,workflowName,headBranch,event,url"])
        except RuntimeError:
            continue
        out += [(repo, r) for r in runs if r.get("status") in ACTIVE_RUN]
    return out


def fetch_usage():
    """Claude Code session (5h) / week (7d) usage %, read from the local OAuth
    token. Read-only — never refreshes or writes the token back; any failure
    (missing file, expired token, network, rate limit) just returns None and
    the panel disappears rather than erroring the whole dashboard."""
    if not USAGE:
        return None
    try:
        with open(CREDS_FILE, encoding="utf-8") as f:
            token = json.load(f)["claudeAiOauth"]["accessToken"]
    except (OSError, ValueError, KeyError):
        return None
    if not token:
        return None
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": USAGE_USER_AGENT,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    out = []
    for key, label in (("five_hour", "session"), ("seven_day", "week")):
        block = data.get(key)
        if block and block.get("utilization") is not None:
            out.append((label, block["utilization"], block.get("resets_at")))
    return out or None


# ── Antigravity (opt-in, reverse-engineered — see module docstring) ─────────

def _find_antigravity_process():
    """Best-effort scan for a running Antigravity language-server process;
    returns (port, csrf_token) pulled off its command line, or None."""
    try:
        if os.name == "nt":
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | "
                 "Where-Object { $_.CommandLine -like '*antigravity*' } | "
                 "Select-Object CommandLine | ConvertTo-Json -Compress"],
                capture_output=True, text=True, encoding="utf-8", timeout=10)
            if r.returncode != 0 or not r.stdout.strip():
                return None
            data = json.loads(r.stdout)
            cmdlines = [d.get("CommandLine", "") for d in
                       (data if isinstance(data, list) else [data])]
        else:
            r = subprocess.run(["ps", "aux"], capture_output=True, text=True,
                               encoding="utf-8", timeout=10)
            if r.returncode != 0:
                return None
            cmdlines = [ln for ln in r.stdout.splitlines()
                       if "antigravity" in ln.lower()]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    for cmd in cmdlines:
        port_m = re.search(r"--extension_server_port[= ]([0-9]+)", cmd or "")
        csrf_m = re.search(r"--csrf_token[= ]([^\s\"']+)", cmd or "")
        if port_m and csrf_m:
            return int(port_m.group(1)), csrf_m.group(1)
    return None


def _antigravity_rpc(port, csrf):
    body = json.dumps({"metadata": {"ideName": "antigravity",
                                    "extensionName": "antigravity", "locale": "en"}}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUserStatus",
        data=body, headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "Connect-Protocol-Version": "1", "X-Codeium-Csrf-Token": csrf,
        })
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _is_autocomplete_model(model_id, label):
    return "gemini-2.5" in (model_id or "") or "Gemini 2.5" in (label or "")


def _antigravity_parse_local(data):
    status = data.get("userStatus", data)
    items = []
    plan = status.get("planStatus") or {}
    avail, monthly = plan.get("availablePromptCredits"), (plan.get("planInfo") or {}).get("monthlyPromptCredits")
    if isinstance(avail, (int, float)) and isinstance(monthly, (int, float)) and monthly > 0:
        items.append(("credits", (monthly - avail) / monthly * 100, None))
    for m in (status.get("cascadeModelConfigData") or {}).get("clientModelConfigs") or []:
        model_id = (m.get("modelOrAlias") or {}).get("model")
        label = m.get("label") or model_id or "model"
        if _is_autocomplete_model(model_id, label):
            continue
        frac = (m.get("quotaInfo") or {}).get("remainingFraction")
        if frac is None:
            continue
        items.append((label, (1 - frac) * 100, (m.get("quotaInfo") or {}).get("resetTime")))
    return items or None


def _antigravity_local():
    proc = _find_antigravity_process()
    if not proc:
        return None
    return _antigravity_parse_local(_antigravity_rpc(*proc))


def _antigravity_parse_cli(snap):
    items = []
    pc = snap.get("promptCredits")
    if pc and pc.get("usedPercentage") is not None:
        items.append(("credits", pc["usedPercentage"] * 100, None))
    for m in snap.get("models") or []:
        if m.get("isAutocompleteOnly"):
            continue
        rp = m.get("remainingPercentage")
        if rp is None:
            continue
        items.append((m.get("label") or m.get("modelId") or "model",
                      (1 - rp) * 100, m.get("resetTime")))
    return items or None


def _antigravity_cli():
    exe = shutil.which("antigravity-usage")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--json"], capture_output=True, text=True,
                           encoding="utf-8", timeout=15)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return _antigravity_parse_cli(json.loads(r.stdout))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def fetch_antigravity():
    """Opt-in only (--antigravity). Tries local IDE mode, then the
    antigravity-usage CLI fallback. Both paths are reverse-engineered and can
    break in unforeseen ways — caught broadly here so a shape change upstream
    just empties the panel instead of taking down the dashboard."""
    if not ANTIGRAVITY:
        return None
    try:
        items = _antigravity_local() or _antigravity_cli()
    except Exception:
        return None
    return items[:4] if items else None


def render(rows, hidden_count, interactive, fmode="all", builds=None, usage=None, antigravity=None):
    """Returns (text, labels) where labels maps key → pr_key."""
    labels = {}
    lines = []
    bar = f"{C.GRY}{'─' * W}{C.RESET}"
    now = datetime.now().strftime("%H:%M")
    titulo = {"ready": "READY TO MERGE", "conflicts": "WITH CONFLICTS"}.get(fmode, "OPEN PRs")
    lines.append(f"{C.BOLD}{C.CYN}PR DASHBOARD{C.RESET}")
    head = f"{C.BOLD}{C.WHT} {titulo} · {len(rows)}{C.RESET}"
    if hidden_count:
        head += f"{C.DIM} ({hidden_count} hidden){C.RESET}"
    lines.append(head)
    lines.append(f"{C.DIM} updated {now}{C.RESET}")
    lines.append(bar)

    if usage:
        lines.append(f"{C.BOLD}{C.YEL} USAGE{C.RESET}")
        for label, pct, resets_at in usage:
            col = usage_col(pct)
            rtxt = _fmt_reset(resets_at)
            reset_part = f"{C.DIM} · resets {rtxt}{C.RESET}" if rtxt else ""
            lines.append(f" {col}{pct:>3.0f}%{C.RESET} {C.WHT}{label}{C.RESET}{reset_part}")
        lines.append(bar)

    if antigravity:
        lines.append(f"{C.BOLD}{C.YEL} ANTIGRAVITY{C.RESET}")
        for label, pct, resets_at in antigravity:
            col = usage_col(pct)
            rtxt = _fmt_reset(resets_at)
            reset_part = f"{C.DIM} · resets {rtxt}{C.RESET}" if rtxt else ""
            lines.append(f" {col}{pct:>3.0f}%{C.RESET} {C.WHT}{label}{C.RESET}{reset_part}")
        lines.append(bar)

    if builds:
        lines.append(f"{C.BOLD}{C.YEL} RUNNING BUILDS · {len(builds)}{C.RESET}")
        for repo, r in builds:
            short = repo.split("/")[-1]
            glyph = "⟳" if r.get("status") == "in_progress" else "⋯"
            prod = "production" in (r.get("workflowName") or "").lower()
            tag = f"{C.RED} prod{C.RESET}" if prod else f"{C.CYN} staging{C.RESET}"
            top = f" {C.YEL}{glyph}{C.RESET} {C.WHT}{short}{C.RESET}{tag}"
            url = r.get("url")
            lines.append(link(url, top) if url else top)
            lines.append(f"   {C.DIM}{r.get('workflowName', '?')} · {r.get('headBranch', '?')}{C.RESET}")
        lines.append(bar)

    if not rows:
        alvo = {"ready": "ready-to-merge", "conflicts": "conflicting"}.get(fmode, "open")
        lines.append(f"{C.DIM} No {alvo} PR visible. 🎉{C.RESET}")
        lines.append(bar)

    for idx, (p, repo, rich) in enumerate(rows):
        short = repo.split("/")[-1]
        a, acol = age(p["createdAt"])
        ci, ci_running = ci_status(rich.get("statusCheckRollup")) if RICH else (" ", False)
        rcol, rlbl = review_label(rich.get("reviewDecision")) if RICH else (C.GRY, "")
        adds, dels = rich.get("additions", 0), rich.get("deletions", 0)

        lbl = ""
        if interactive and idx < len(LABELS):
            ch = LABELS[idx]
            labels[ch] = pr_key(repo, p["number"])
            lbl = f"{C.GRY}{ch}{C.RESET} "

        merge = {
            "CLEAN": f" {C.GRN}⇪{C.RESET}",            # ready to merge
            "DIRTY": f" {C.RED}⚠{C.RESET}",            # merge conflict
            "BEHIND": f" {C.YEL}↺{C.RESET}",           # branch behind base
        }.get(rich.get("mergeStateStatus"), "")         # UNKNOWN/BLOCKED → no marker
        lines.append(f" {lbl}{acol}{a:>4}{C.RESET} {ci} {rcol}{rlbl}{C.RESET}{merge}")
        if RICH:
            revs = pending_reviewers(rich)
            if revs:
                names = ", ".join(revs[:3]) + (f" +{len(revs) - 3}" if len(revs) > 3 else "")
                lines.append(f" {C.YEL}⏳ waiting {names}{C.RESET}")
            if ci_running:
                lines.append(f" {C.YEL}⟳ build running{C.RESET}")
        draft = f"{C.YEL}◌ {C.RESET}" if p["isDraft"] else ""
        repo_ref = f"{C.CYN}{short}{C.RESET} {C.BOLD}#{p['number']}{C.RESET}"
        lines.append(f" {draft}{link(p['url'], repo_ref)}")
        for tl in wrap(p["title"], W - 2):
            lines.append(f" {link(p['url'], C.WHT + tl + C.RESET)}")
        if RICH:
            lines.append(f" {C.DIM}+{adds}/-{dels}{C.RESET}")
        lines.append(bar)

    if interactive:
        def tag(key, lbl, on):
            cor = C.WHT if on else C.GRY
            return f"{cor}{key}{C.RESET}{C.DIM}={lbl}{C.RESET}"
        filtros = " ".join([
            tag("a", "all", fmode == "all"),
            tag("m", "ready", fmode == "ready"),
            tag("c", "conflicts", fmode == "conflicts"),
        ])
        lines.append(f" {filtros}")
        lines.append(f"{C.DIM} space=reload · key=hide · r=restore · q=quit{C.RESET}")

    return "\n".join(lines), labels


# ── stale worktree reaper ─────────────────────────────────────────────────────
# Reaps worktrees that hold nothing new: PR merged + 0 commits ahead of upstream
# + clean working tree. Before removing, logs a recreate record (path/branch/sha/PR)
# as a fallback. Default = dry-run only; actual removal requires --reap-worktrees.

REAP_LOG = os.path.join(os.path.expanduser("~"), ".pr-dashboard-reaped.json")
KEEP_FILE = os.path.join(os.path.expanduser("~"), ".pr-dashboard-keep.json")
# persistent infra checkouts: NEVER reap, even if clean+merged. Name with
# 'homolog'/'prod' or '-main' suffix = live working copy, not a throwaway worktree.
PROTECT_SUBSTR = ("homolog", "prod")


def is_protected(name):
    """True if the worktree is a persistent infra checkout — never reap."""
    low = name.lower()
    if low.endswith("-main") or any(s in low for s in PROTECT_SUBSTR):
        return True
    try:
        with open(KEEP_FILE, encoding="utf-8") as f:
            return name in set(json.load(f))
    except (OSError, ValueError):
        return False


def _git(d, args):
    """`git -C d ...` → stdout stripped, or None on failure."""
    r = subprocess.run(["git", "-C", d, *args], capture_output=True,
                       text=True, encoding="utf-8")
    return r.stdout.strip() if r.returncode == 0 else None


def _unlink_reparse(p):
    """Remove a junction/symlink (the LINK, never the target). Returns True if removed.
    On Windows worktrees often have node_modules as a shared junction;
    following the link in rmtree would delete the real node_modules — so readlink+rmdir."""
    try:
        os.readlink(p)        # only succeeds if it's a reparse point (junction/symlink)
    except OSError:
        return False          # not a link → leave it alone
    try:
        os.rmdir(p)           # directory junction: remove only the link
    except OSError:
        try:
            os.unlink(p)      # file symlink
        except OSError:
            return False
    return True


def _cleanup_dir(path):
    """Remove the ghost directory left behind by `git worktree remove`
    (leftover junctions like node_modules + empty gitignored dirs). NEVER deletes
    real ignored content (.env, dist/…): if any exists, keeps it and returns the reason.
    Returns (clean: bool, leftover: str|None)."""
    if not os.path.isdir(path):
        return True, None
    leftovers = []
    for entry in os.listdir(path):
        full = os.path.join(path, entry)
        if _unlink_reparse(full):
            continue
        if os.path.isdir(full) and not os.listdir(full):
            try:
                os.rmdir(full)
                continue
            except OSError:
                pass
        leftovers.append(entry)
    if leftovers:
        return False, "ignored content: " + ", ".join(sorted(leftovers)[:4])
    try:
        os.rmdir(path)
    except OSError as e:
        return False, str(e)
    return True, None


def _infer_wt_root():
    """Infer worktree root from `git worktree list` in cwd.
    All linked worktrees share a common parent directory — that parent is the root."""
    r = subprocess.run(["git", "worktree", "list", "--porcelain"],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        return None
    paths = [line[len("worktree "):] for line in r.stdout.splitlines()
             if line.startswith("worktree ")]
    if len(paths) < 2:
        return None  # only main worktree; nothing linked
    # linked worktrees start at index 1; their common parent is the root
    parents = {os.path.dirname(os.path.abspath(p)) for p in paths[1:]}
    if len(parents) == 1:
        return parents.pop()
    return None  # worktrees in different dirs — can't infer a single root


def wt_root():
    """Root of sibling worktrees. --reap-root FLAG > env PR_DASH_WT_ROOT > git auto-detect."""
    if "--reap-root" in sys.argv:
        i = sys.argv.index("--reap-root")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    env = os.environ.get("PR_DASH_WT_ROOT", "").strip()
    if env:
        return env
    inferred = _infer_wt_root()
    if inferred:
        print(f"Auto-detected worktree root: {inferred}", file=sys.stderr)
        return inferred
    print("Could not detect worktree root. Set PR_DASH_WT_ROOT or use --reap-root <path>.",
          file=sys.stderr)
    sys.exit(1)


def repo_slug(d):
    """owner/repo from the origin remote URL (ssh or https), or None."""
    url = (_git(d, ["remote", "get-url", "origin"]) or "").rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    parts = [p for p in url.replace(":", "/").split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def main_repo_of(d):
    """Absolute path of the MAIN repo that owns this worktree (used to run git worktree remove)."""
    common = _git(d, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    if common and os.path.basename(common.rstrip("/\\")) == ".git":
        return os.path.dirname(common.rstrip("/\\"))
    return None


def scan_worktrees():
    """Scan wt_root() and diagnose each LINKED worktree (.git = file)."""
    root = wt_root()
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    merged_cache, open_cache = {}, {}   # per repo, to avoid hammering the API
    out = []
    for name in entries:
        d = os.path.join(root, name)
        if not os.path.isfile(os.path.join(d, ".git")):
            continue  # main repo (.git = dir) or non-git → skip
        branch = _git(d, ["rev-parse", "--abbrev-ref", "HEAD"])
        if is_protected(name):
            out.append({"name": name, "path": d, "branch": branch or "?",
                        "reap": False, "why": "protected (infra/live checkout)"})
            continue
        if not branch or branch == "HEAD":
            out.append({"name": name, "path": d, "branch": branch or "?",
                        "reap": False, "why": "detached HEAD"})
            continue
        dirty = bool(_git(d, ["status", "--porcelain"]))
        upstream = _git(d, ["rev-parse", "--abbrev-ref",
                            "--symbolic-full-name", "@{upstream}"])
        ahead = None
        if upstream:
            c = _git(d, ["rev-list", "--count", f"{upstream}..HEAD"])
            ahead = int(c) if c and c.isdigit() else None
        slug = repo_slug(d)
        head = _git(d, ["rev-parse", "HEAD"])

        merged_pr, open_pr = None, False
        if slug:
            if slug not in merged_cache:
                try:
                    merged_cache[slug] = {p["headRefName"]: p for p in gh(
                        ["pr", "list", "--repo", slug, "--state", "merged",
                         "--limit", "300", "--json", "number,headRefName"])}
                except RuntimeError:
                    merged_cache[slug] = {}
            if slug not in open_cache:
                try:
                    open_cache[slug] = {p["headRefName"] for p in gh(
                        ["pr", "list", "--repo", slug, "--state", "open",
                         "--limit", "300", "--json", "number,headRefName"])}
                except RuntimeError:
                    open_cache[slug] = set()
            merged_pr = merged_cache[slug].get(branch)
            open_pr = branch in open_cache[slug]

        why = None
        if dirty:
            why = "dirty (local changes)"
        elif open_pr:
            why = "PR still open"
        elif not slug:
            why = "no origin remote"
        elif not merged_pr:
            why = "no merged PR"
        elif not upstream:
            why = "no upstream (not pushed?)"
        elif ahead is None:
            why = "ahead count indeterminate"
        elif ahead > 0:
            why = f"{ahead} commit(s) ahead of remote"
        out.append({"name": name, "path": d, "branch": branch, "repo": slug,
                    "head": head, "ahead": ahead,
                    "pr": (merged_pr or {}).get("number"),
                    "reap": why is None, "why": why or "reapable"})
    return out


def reap(items, do_it):
    """Remove (if do_it) reapable worktrees; always returns records with recreate commands.
    Only logs to REAP_LOG the ones actually removed. Returns path → rec map."""
    recs = {}
    try:
        with open(REAP_LOG, encoding="utf-8") as f:
            log = json.load(f)
    except (OSError, ValueError):
        log = []
    ts = datetime.now(timezone.utc).isoformat()
    targets = [x for x in items if x["reap"]]
    if "--reap-limit" in sys.argv:                # cap at N per run
        i = sys.argv.index("--reap-limit")
        if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit():
            targets = targets[:int(sys.argv[i + 1])]
    for w in targets:
        main = main_repo_of(w["path"])
        rec = {"ts": ts, "path": w["path"], "branch": w["branch"],
               "repo": w["repo"], "head": w["head"], "pr": w["pr"],
               "main_repo": main, "removed": False, "error": None,
               "recreate": (f'git -C "{main}" worktree add "{w["path"]}" {w["head"]}'
                            if main and w["head"] else None)}
        if do_it:
            if not main:
                rec["error"] = "main repo not found"
            else:
                r = subprocess.run(["git", "-C", main, "worktree", "remove", w["path"]],
                                   capture_output=True, text=True, encoding="utf-8")
                if r.returncode == 0:
                    rec["removed"] = True
                    subprocess.run(["git", "-C", main, "worktree", "prune"],
                                   capture_output=True, text=True)
                    clean, leftover = _cleanup_dir(w["path"])  # clean up ghost dir
                    rec["leftover"] = None if clean else leftover
                else:
                    rec["error"] = r.stderr.strip()
        recs[w["path"]] = rec
    if do_it:
        log.extend([r for r in recs.values() if r["removed"]])
        try:
            with open(REAP_LOG, "w", encoding="utf-8") as f:
                json.dump(log, f, indent=2, ensure_ascii=False)
        except OSError:
            pass
    return recs


def render_worktrees(items, recs, do_it):
    bar = f"{C.GRY}{'─' * W}{C.RESET}"
    reapable = [w for w in items if w["reap"]]
    kept = [w for w in items if not w["reap"]]
    lines = [f"{C.BOLD}{C.CYN}WORKTREES{C.RESET}"]
    titulo = "REAPED" if do_it else "REAPABLE"
    lines.append(f"{C.BOLD}{C.WHT} {titulo} · {len(reapable)}{C.RESET}"
                 f"{C.DIM} of {len(items)} worktrees{C.RESET}")
    lines.append(bar)
    for w in reapable:
        rec = recs.get(w["path"])
        if do_it and rec is not None:
            mark = (f"{C.GRN}✓ reaped{C.RESET}" if rec.get("removed")
                    else f"{C.RED}✗ failed{C.RESET}")
        elif do_it:
            mark = f"{C.YEL}⌫ reapable (next run){C.RESET}"
        else:
            mark = f"{C.RED}⌫ reapable{C.RESET}"
        rec = rec or {}
        pr = f" PR#{w['pr']}" if w.get("pr") else ""
        lines.append(f" {mark} {C.WHT}{w['name']}{C.RESET}{C.DIM}{pr}{C.RESET}")
        lines.append(f"   {C.GRY}{w['branch']}{C.RESET}")
        if do_it and rec.get("error"):
            lines.append(f"   {C.RED}{rec['error']}{C.RESET}")
        if do_it and rec.get("leftover"):
            lines.append(f"   {C.YEL}⚠ dir kept — {rec['leftover']}{C.RESET}")
    if reapable:
        lines.append(bar)
    lines.append(f"{C.DIM} KEPT · {len(kept)}{C.RESET}")
    for w in kept:
        lines.append(f" {C.YEL}•{C.RESET} {C.WHT}{w['name']}{C.RESET} "
                     f"{C.DIM}— {w['why']}{C.RESET}")
    lines.append(bar)
    if reapable and not do_it:
        lines.append(f"{C.DIM} --reap-worktrees to reap · "
                     f"recreate log at ~/.pr-dashboard-reaped.json{C.RESET}")
    return "\n".join(lines)


def clear_screen():
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def main():
    if "--clear-hidden" in sys.argv:
        save_hidden(set())
        print("Hidden PRs restored.")
        return

    if "--worktrees" in sys.argv or "--reap-worktrees" in sys.argv:
        do_it = "--reap-worktrees" in sys.argv
        items = scan_worktrees()
        recs = reap(items, do_it)
        print(render_worktrees(items, recs, do_it))
        return

    watch = "--watch" in sys.argv
    interactive = watch and "--no-input" not in sys.argv and sys.stdin.isatty()
    interval = 60
    if watch:
        i = sys.argv.index("--watch")
        if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit():
            interval = int(sys.argv[i + 1])

    fmode = "ready" if "--ready" in sys.argv else "conflicts" if "--conflicts" in sys.argv else "all"
    hidden = load_hidden()
    all_rows = None
    all_builds = []
    all_usage = None
    all_antigravity = None
    need_fetch = True
    prev_snap = None

    while True:
        if need_fetch:
            all_rows = fetch()
            all_builds = fetch_builds() if BUILDS else []
            all_usage = fetch_usage()
            all_antigravity = fetch_antigravity()
            curr_snap = _state_snapshot(all_rows)
            _diff_notify(prev_snap, curr_snap, all_rows)
            prev_snap = curr_snap
            need_fetch = False
        visible = [r for r in all_rows if pr_key(r[1], r[0]["number"]) not in hidden]
        hidden_count = len(all_rows) - len(visible)  # only user-hidden, not filtered
        if fmode == "ready":
            visible = [r for r in visible if r[2].get("mergeStateStatus") == "CLEAN"]
        elif fmode == "conflicts":
            visible = [r for r in visible if r[2].get("mergeStateStatus") == "DIRTY"]

        if watch:
            clear_screen()
        out, labels = render(visible, hidden_count, interactive, fmode, all_builds, all_usage, all_antigravity)
        print(out)

        if not watch:
            break

        if not interactive:
            time.sleep(interval)
            need_fetch = True
            continue

        ch = wait_key(interval)
        if ch is None:          # timeout → reload data from GitHub
            need_fetch = True
            continue
        ch = ch.lower()
        if ch == "q":
            break
        elif ch == " ":          # space = reload now from GitHub
            need_fetch = True
        elif ch == "a":
            fmode = "all"        # switch filter — local re-render, no refetch
        elif ch == "m":
            fmode = "ready"
        elif ch == "c":
            fmode = "conflicts"
        elif ch == "r":
            hidden.clear()
            save_hidden(hidden)
        elif ch in labels:
            hidden.add(labels[ch])
            save_hidden(hidden)
        # unknown key → just re-render


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"{C.RED}Error talking to gh:{C.RESET} {e}", file=sys.stderr)
        print(f"{C.DIM}run `gh auth status` to confirm you're logged in.{C.RESET}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
