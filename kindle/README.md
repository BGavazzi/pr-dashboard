# PR dashboard for Kindle (view-only)

Read your open PRs — description + diff — from a Kindle's built-in
**Experimental Browser**. No jailbreak, no app install on the device.
Approve/reject still happens from your PC (`gh pr review` or the GitHub
web UI) — this is read-only by design.

## Why no jailbreak

Unlike [kindletron](https://github.com/oceanpenguin/kindletron) (which
mirrors a remote desktop via VNC screenshots + a KUAL extension, and needs
a jailbroken device), this just serves plain HTML that the Kindle's stock
browser can already render. Big fonts, no CSS the old WebKit engine
chokes on, no JS at all.

## Server setup (homeserver, e.g. 192.168.1.249)

1. Create a GitHub fine-grained PAT with **read-only** `Pull requests` +
   `Contents` scope on the repos you want visible. Do not grant write scope.
2. Copy `.env.example` to `.env` and fill in `GH_TOKEN`.
3. Port **8090** is used here (8080-8083 already taken by other services on this box).
   (Evolution already holds 8081).
4. Build and run:
   ```bash
   docker compose up -d --build
   ```
5. Confirm it's reachable from your LAN: `curl http://192.168.1.249:8090/`

## Kindle setup

1. On the Kindle: **Settings → menu (⋯) → Experimental Browser** (older
   firmware: Home → menu → "Experimental Browser"; some models hide it
   behind typing a fake web address from the search box first — varies by
   firmware version).
2. Navigate to `http://192.168.1.249:8090/`.
3. Tap a PR title to open its description + diff. Tap "← back to list" to
   return.
4. Bookmark the URL (browser menu → bookmark this page) so it's one tap
   next time.

The list page auto-refreshes every 90s (`<meta refresh>`). The detail page
does not auto-refresh, so reading a diff won't get yanked out from under
you.

## Defaults & query params

- Default: open PRs authored by you (`--author @me`), same as
  `pr_dashboard.py`'s default.
- `?review-requested=1` — PRs awaiting **your** review instead.
- `?org=<org>` — whole org instead of just your PRs.

Example: `http://192.168.1.249:8090/?review-requested=1`

## Limits

- Diffs are truncated at 800 lines (e-ink browsers are slow with huge
  pages) — a "truncated" note appears with the count.
- No auth in front of this — it's plain HTTP on your LAN, showing PR
  titles/diffs from whatever repos your token can read. Keep it on the LAN
  (don't port-forward it), and use a read-only token as noted above.
- Not real-time — matches the 90s refresh, not a live feed.
