# `pr_dashboard.py` — vertical PR dashboard

Built to stay on top of PRs opened by multiple coding agents running in parallel. When you have several AI agents each opening PRs on your behalf, it's easy to lose track of what's waiting for review, what's blocked by conflicts, and what's ready to merge. This dashboard gives you a persistent, narrow column on the side of your screen so you always know the state at a glance.

Narrow column (34 chars wide) showing open PRs, designed to **dock on the side of an ultrawide monitor** and run in watch mode. Each PR is a clickable card; in watch mode it's a **live interactive TUI** (filter and hide by keypress). Zero embedded credentials — uses the already-authenticated `gh` CLI.

## Prerequisites

- [`gh` CLI](https://cli.github.com/) installed and logged in:
  ```bash
  gh auth status   # should show "Logged in"
  gh auth login    # if not yet
  ```
- Python 3.10+ (no external dependencies — stdlib only).
- Terminal with **OSC 8 hyperlink** support for clickable links
  (Windows Terminal, iTerm2, kitty, WezTerm, GNOME Terminal — all supported).

## Quick start

```bash
# single render of YOUR open PRs
python pr_dashboard.py

# watch mode — live TUI in the corner of your screen: refreshes every 60s + responds to keys
python pr_dashboard.py --watch
python pr_dashboard.py --watch 30      # refresh every 30s
```

On Windows there's a PowerShell wrapper that fixes UTF-8:

```powershell
.\pr-dash.ps1            # watch 60s
.\pr-dash.ps1 30         # watch 30s
.\pr-dash.ps1 -Once      # single render
```

## Keys (--watch mode, TTY)

The TUI responds to keypresses without restarting. Filters are live state:

| Key | Action |
|---|---|
| `space` | **reload now** from GitHub (reload button — doesn't wait for the interval) |
| `a` | show **all** PRs |
| `m` | only **ready to merge** (`mergeStateStatus == CLEAN`) |
| `c` | only **with conflicts** (`mergeStateStatus == DIRTY`) |
| `r` | **restore** all hidden PRs |
| `q` | **quit** |
| card letter/number | **hide** that PR (persistent — see below) |

The active filter is highlighted in the footer. Switching filters or hiding a PR re-renders **immediately** (no new API call); GitHub reload only happens when the `--watch` interval expires.

## Flags

| Flag | What it does |
|---|---|
| `--watch [seconds]` | Loop with interactive TUI (default 60s). |
| `--no-rich` | Skips CI/review/merge/diff → **1 API call** only. Much faster. |
| `--ready` | Opens already filtered to "ready to merge" (= key `m`). |
| `--conflicts` | Opens already filtered to "with conflicts" (= key `c`). |
| `--org <ORG>` | Covers the entire org, not just PRs authored by you. |
| `--review-requested` | PRs awaiting **your** review (instead of yours). |
| `--no-builds` | Skip the **RUNNING BUILDS** panel (1 fewer `gh run list` call per repo). |
| `--builds-repo <O/R>` | Extra repo to watch for builds (**repeatable**). Overrides default. |
| `--clear-hidden` | Restore all hidden PRs and exit. |
| `--no-input` | Watch without keyboard (pure refresh) — for non-TTY environments. |

Examples:

```bash
python pr_dashboard.py --watch --ready                # open on ready-to-merge
python pr_dashboard.py --org <your-org> --watch       # whole org, interactive
python pr_dashboard.py --review-requested             # awaiting my review
python pr_dashboard.py --clear-hidden                 # reset hidden PRs
```

Via wrapper, pass flags with `-Args`:

```powershell
.\pr-dash.ps1 -Args '--org','<your-org>','--ready'
```

## Reading a card

```
 a  12d  ✗ changes ⚠     ← [label] · age · CI · review · merge state
 owner/my-repo #42         ← repo + number (Ctrl+click opens PR)
 feat(companies): notes    ← title with wrap (also clickable)
 history (append-only)…
 +856/-1                   ← diff (lines +/-)
────────────────────────
```

- **label** (`a`, `1`…): interactive mode only — press to hide the card.
- **Age**: green `<3d` · yellow `<14d` · red `≥14d`. Cards sorted **oldest first**.
- **CI**: `✓` all green · `✗` some failure · `⋯` running · `·` no checks.
- **Review**: `approved` / `changes` / `review` (pending) / `no review`.
- **Merge**: `⇪` (green) ready to merge · `⚠` (red) conflict · `↺` (yellow) branch behind base · *(nothing)* = `UNKNOWN`/`BLOCKED`.
- **`◌`** before the repo = PR is a **draft**.
- **`⏳ waiting <names>`**: appears when there's a requested reviewer who hasn't reviewed yet (up to 3 names + count). Sourced from `reviewRequests`.
- **`⟳ build running`**: appears when the PR's CI is in progress.

## RUNNING BUILDS panel

Above the cards, when there are **workflow runs in progress** on watched repos:

```
 RUNNING BUILDS · 1
 ⟳ owner/my-repo  production        ← glyph · repo · environment
   Deploy staging · main             ← workflow · branch/tag (Ctrl+click opens the run)
```

This is **intentionally separate from PR checks**: if CI runs on `push` (not on PRs), those runs don't appear as checks on any open PR. The panel fetches them via `gh run list`. `prod` (red) = workflow with "production" in the name; otherwise `homolog` (cyan).

**Watched repos** (precedence): `--builds-repo` (repeatable) → env `PR_DASH_BUILD_REPOS="owner/a,owner/b"` → default empty (panel disabled if no repo configured). Disable explicitly with `--no-builds`.

## Hidden PRs

Hiding a card (label key) removes it and saves to `~/.pr-dashboard-hidden.json` (key `owner/repo#num`) — stays hidden across runs. `r` restores all; `--clear-hidden` also works. The "(N hidden)" counter in the header counts only PRs you hid, not those filtered by `--ready`/`--conflicts`.

## Stale worktree reaper

When running many parallel agents, you accumulate worktrees fast. The reaper cleans up worktrees that are safe to delete: PR merged + branch 0 commits ahead of remote + clean working tree. Before removing anything it logs a recreate command to `~/.pr-dashboard-reaped.json` as a fallback.

Protected from reaping: names ending in `-main`, containing `homolog` or `prod`, or listed in `~/.pr-dashboard-keep.json`.

```bash
# Dry run — shows what's reapable and why each kept worktree is kept
python pr_dashboard.py --worktrees

# Actually reap
python pr_dashboard.py --reap-worktrees

# Cap at N removals per run
python pr_dashboard.py --reap-worktrees --reap-limit 5

# Specify worktree root (default: PR_DASH_WT_ROOT env var)
python pr_dashboard.py --worktrees --reap-root C:\your\worktrees
```

Set the root permanently via `PR_DASH_WT_ROOT` env var so you don't need `--reap-root` every time.

## Design notes

- **No credentials in code.** The token comes from the OS keyring via `gh auth`. Safe to version and share without leaking anything.
- **API cost.** Default mode makes `1 + N` calls (list + 1 `gh pr view` per PR for CI/review/merge/diff) **+ 1 `gh run list` per watched repo** for the builds panel. Switching filters or hiding cards does **not** call the API (local re-render). With many PRs, use `--no-rich`; to cut the panel, `--no-builds`.
- **Not real-time.** Watch sleeps the full interval between reloads — but `space` forces an immediate reload without waiting.

## Known limitations

- `gh search prs` doesn't expose `reviewDecision`/CI/merge directly → hence the `gh pr view` per PR. Without it (`--no-rich`) you lose those columns and the `--ready`/`--conflicts` filters.
- **`mergeStateStatus` is eventually consistent.** GitHub computes mergeability lazily: the first query on a PR may return `UNKNOWN` (no marker, excluded from `--ready`/`--conflicts`). The next watch refresh usually resolves it. A PR "disappearing" from `--ready` for one cycle is expected, not a bug.
- Column width is fixed at `W = 34` at the top of the script — adjust there if you want a different width.
