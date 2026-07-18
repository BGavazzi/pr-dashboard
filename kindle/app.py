#!/usr/bin/env python3
"""PR dashboard for e-ink browsers (Kindle Experimental Browser and similar).

View-only: no jailbreak, no writes to GitHub. Renders the same PR state
`pr_dashboard.py` shows in the terminal, as plain HTML — big fonts, no CSS
tricks or JS the old WebKit engine on a Kindle can't handle. Approve/reject
still happens from your PC (`gh pr review` / GitHub web UI).

Auth: set GH_TOKEN (a fine-grained PAT with read-only PR/contents access) as
an env var. The `gh` CLI picks it up automatically — no `gh auth login`
needed inside the container.

Routes:
    GET /                          PR list (author=@me by default)
    GET /?org=<org>                PR list for a whole org instead
    GET /?review-requested=1       PRs awaiting your review instead of yours
    GET /pr/<owner>/<repo>/<num>   PR detail: description + diff

Run:
    GH_TOKEN=ghp_xxx python app.py         # dev server, port 8082
"""
import json
import os
import subprocess
from datetime import datetime, timezone

from flask import Flask, abort, request

app = Flask(__name__)

PORT = int(os.environ.get("PORT", "8082"))
DIFF_LINE_CAP = 800  # e-ink browsers choke on huge pages; truncate past this


def gh(args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout


def gh_json(args):
    return json.loads(gh(args))


def age_str(created_iso):
    created = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - created).days
    return "today" if days == 0 else f"{days}d"


def ci_str(rollup):
    if not rollup:
        return "no checks"
    states = [(c.get("conclusion") or c.get("state") or "").upper() for c in rollup]
    if any(s in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED") for s in states):
        return "CI: failing"
    if any(s in ("PENDING", "IN_PROGRESS", "QUEUED", "") for s in states):
        return "CI: running"
    return "CI: passing"


def review_str(decision):
    return {
        "APPROVED": "approved",
        "CHANGES_REQUESTED": "changes requested",
        "REVIEW_REQUIRED": "review pending",
    }.get(decision or "", "no review")


def merge_str(status):
    return {
        "CLEAN": "ready to merge",
        "DIRTY": "merge conflict",
        "BEHIND": "branch behind base",
    }.get(status, "")


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


PAGE_HEAD = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}
<title>{title}</title>
<style>
  body {{ font-family: Georgia, serif; font-size: 22px; line-height: 1.4;
         color: #000; background: #fff; margin: 0; padding: 12px 16px; }}
  a {{ color: #000; text-decoration: underline; }}
  .card {{ border-bottom: 2px solid #000; padding: 14px 0; }}
  .meta {{ font-size: 18px; color: #333; }}
  .repo {{ font-size: 18px; }}
  h1 {{ font-size: 26px; margin: 4px 0 10px 0; }}
  h2 {{ font-size: 22px; margin: 14px 0 6px 0; }}
  pre {{ font-family: "Courier New", monospace; font-size: 16px;
         white-space: pre-wrap; word-wrap: break-word; }}
  .add {{ background: #e8e8e8; }}
  .del {{ text-decoration: line-through; color: #555; }}
  .top-nav {{ font-size: 18px; margin-bottom: 10px; }}
</style>
</head><body>
"""

PAGE_TAIL = "</body></html>"


@app.route("/")
def index():
    args = ["search", "prs", "--state", "open", "--limit", "100",
            "--json", "number,title,repository,createdAt,url,isDraft"]
    if request.args.get("review-requested"):
        args[2:2] = ["--review-requested", "@me"]
    else:
        args[2:2] = ["--author", "@me"]
    org = request.args.get("org")
    if org:
        args[2:2] = ["--owner", org]

    try:
        prs = gh_json(args)
    except RuntimeError as e:
        abort(502, description=str(e))
    prs.sort(key=lambda p: p["createdAt"])

    html = [PAGE_HEAD.format(refresh='<meta http-equiv="refresh" content="90">',
                             title="PR Dashboard")]
    html.append(f"<h1>PR Dashboard &middot; {len(prs)}</h1>")
    html.append(f'<div class="meta">updated {datetime.now().strftime("%H:%M")} '
                '&middot; refreshes every 90s</div>')

    if not prs:
        html.append("<p>No open PRs. 🎉</p>")

    for p in prs:
        repo = p["repository"]["nameWithOwner"]
        try:
            rich = gh_json(["pr", "view", str(p["number"]), "--repo", repo, "--json",
                            "reviewDecision,statusCheckRollup,additions,deletions,"
                            "mergeStateStatus"])
        except RuntimeError:
            rich = {}
        owner, name = repo.split("/", 1)
        detail_url = f"/pr/{owner}/{name}/{p['number']}"
        draft = "[draft] " if p["isDraft"] else ""
        merge = merge_str(rich.get("mergeStateStatus"))
        bits = [age_str(p["createdAt"]), ci_str(rich.get("statusCheckRollup")),
                review_str(rich.get("reviewDecision"))]
        if merge:
            bits.append(merge)
        adds, dels = rich.get("additions", 0), rich.get("deletions", 0)

        html.append('<div class="card">')
        html.append(f'<div class="repo">{esc(repo)} #{p["number"]}</div>')
        html.append(f'<div><a href="{detail_url}">{draft}{esc(p["title"])}</a></div>')
        html.append(f'<div class="meta">{" &middot; ".join(bits)} '
                    f'&middot; +{adds}/-{dels}</div>')
        html.append('</div>')

    html.append(PAGE_TAIL)
    return "".join(html)


@app.route("/pr/<owner>/<repo>/<int:number>")
def pr_detail(owner, repo, number):
    slug = f"{owner}/{repo}"
    try:
        meta = gh_json(["pr", "view", str(number), "--repo", slug, "--json",
                        "title,body,createdAt,url,author,reviewDecision,"
                        "statusCheckRollup,mergeStateStatus,additions,deletions"])
        diff = gh(["pr", "diff", str(number), "--repo", slug])
    except RuntimeError as e:
        abort(502, description=str(e))

    lines = diff.splitlines()
    truncated = len(lines) > DIFF_LINE_CAP
    if truncated:
        lines = lines[:DIFF_LINE_CAP]

    html = [PAGE_HEAD.format(refresh="", title=f"{slug} #{number}")]
    html.append(f'<div class="top-nav"><a href="/">&larr; back to list</a></div>')
    html.append(f"<h1>{esc(meta['title'])}</h1>")
    bits = [f'{slug} #{number}', age_str(meta['createdAt']),
            ci_str(meta.get('statusCheckRollup')),
            review_str(meta.get('reviewDecision'))]
    merge = merge_str(meta.get('mergeStateStatus'))
    if merge:
        bits.append(merge)
    bits.append(f"+{meta.get('additions', 0)}/-{meta.get('deletions', 0)}")
    html.append(f'<div class="meta">{" &middot; ".join(bits)}</div>')
    if meta.get("body"):
        html.append("<h2>Description</h2>")
        html.append(f"<pre>{esc(meta['body'])}</pre>")

    html.append(f"<h2>Diff ({len(lines)}{'+' if truncated else ''} lines)</h2>")
    html.append("<pre>")
    for ln in lines:
        cls = "add" if ln.startswith("+") and not ln.startswith("+++") \
            else "del" if ln.startswith("-") and not ln.startswith("---") else ""
        safe = esc(ln)
        html.append(f'<span class="{cls}">{safe}</span>\n' if cls else safe + "\n")
    if truncated:
        html.append(f"\n... truncated at {DIFF_LINE_CAP} lines ...")
    html.append("</pre>")
    html.append(PAGE_TAIL)
    return "".join(html)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
