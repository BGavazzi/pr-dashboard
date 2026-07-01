#!/usr/bin/env python3
"""Dashboard VERTICAL de PRs abertas — coluna estreita pra encostar na lateral
de um monitor ultrawide. Lê do GitHub via o `gh` CLI já autenticado.

Sem credenciais embutidas: usa `gh auth` (token no keyring do SO). Funciona em
qualquer máquina onde `gh auth status` esteja logado.

Uso:
    python pr_dashboard.py                  # render único (minhas PRs abertas)
    python pr_dashboard.py --watch          # refresca a cada 60s + teclas (Ctrl+C sai)
    python pr_dashboard.py --watch 30       # refresca a cada 30s
    python pr_dashboard.py --no-rich        # pula CI/review — 1 chamada só, rapidão
    python pr_dashboard.py --org ORG        # toda a org (não só as minhas)
    python pr_dashboard.py --review-requested  # PRs aguardando MEU review
    python pr_dashboard.py --ready          # só as prontas pra merge (aprovada+CI+sem conflito)
    python pr_dashboard.py --conflicts      # só as com conflito de merge
    python pr_dashboard.py --no-builds      # não consulta builds em andamento (1 chamada/repo a menos)
    python pr_dashboard.py --builds-repo O/R  # repo p/ vigiar builds (repetível)
    python pr_dashboard.py --clear-hidden   # restaura todas as PRs ocultas e sai

Reaper de worktrees stale (limpa o disco; modo separado, não entra no --watch):
    python pr_dashboard.py --worktrees        # SÓ MOSTRA: o que é reapável e por quê
    python pr_dashboard.py --reap-worktrees   # DEVORA as reapáveis (PR merged + 0 à
                                              #   frente do remoto + limpa)
    python pr_dashboard.py --reap-worktrees --reap-limit 5  # come no máx N por leva
    python pr_dashboard.py --worktrees --reap-root C:\\dir   # outra raiz (env: PR_DASH_WT_ROOT)
  Protege checkouts vivos (nome *-main / *homolog* / *prod* + ~/.pr-dashboard-keep.json).
  Cada remoção vai pra ~/.pr-dashboard-reaped.json com o comando de recreate (fallback).

Teclas no modo --watch (TTY):
    espaço                → recarrega já do GitHub (botão de reload)
    a / m / c             → filtro: todas / prontas pra merge / com conflito
    letra/número do card  → oculta aquela PR (persistente)
    r                     → restaura todas as ocultas
    q                     → sai

Cada card mostra: idade · CI (✓ ok / ✗ falhou / ⋯ rodando) · review · merge.
Quando há build em andamento → linha "⟳ build rodando".
Reviewer solicitado que ainda não revisou → linha "⏳ aguardando <nomes>".

Painel "BUILDS RODANDO": workflow runs em andamento nos repos vigiados —
staging (push→main) E release de prod (push→tag). Esses NÃO são checks de PR,
então a consulta é separada (`gh run list`). Override com --builds-repo ou
a env PR_DASH_BUILD_REPOS="owner/a,owner/b". Default = vazio (configure via --builds-repo).

Ver guia completo em scripts/pr-dashboard.md.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

W = 34  # largura da coluna
LOGO = "⬡⬢⬡⬢"
RICH = "--no-rich" not in sys.argv
BUILDS = "--no-builds" not in sys.argv
LABELS = "123456789bdefghijklnopstuvwxyz"  # exclui a/c/m/q/r (teclas de comando)
HIDDEN_FILE = os.path.join(os.path.expanduser("~"), ".pr-dashboard-hidden.json")

# console do Windows costuma vir em cp1252 — força utf-8 pros glifos/acentos
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


class C:
    RESET = "\033[0m"; DIM = "\033[2m"; BOLD = "\033[1m"
    RED = "\033[91m"; GRN = "\033[92m"; YEL = "\033[93m"
    CYN = "\033[96m"; GRY = "\033[90m"; WHT = "\033[97m"; BLU = "\033[94m"


def _resolve_token():
    """Lê o token UMA vez do keyring (via `gh auth token`) pra injetar como
    GH_TOKEN nas chamadas seguintes. Assim cada `gh` NÃO reabre o Credential
    Manager do Windows — cuja leitura sob rajada (o --watch dispara N processos
    `gh` por refresh) falha de vez em quando e manda a request sem token → 401."""
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, encoding="utf-8")
        tok = (r.stdout or "").strip()
        if tok:
            return tok
    except OSError:
        pass
    return None  # sem token → cai no comportamento antigo (keyring por chamada)


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
        # 401 transitório (keyring/refresh) → re-tenta com backoff curto;
        # erro de outra natureza → falha já, sem insistir.
        if "401" not in last and "authentication" not in last.lower():
            break
        time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(last)


# ── store de PRs ocultas ────────────────────────────────────────────────────

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


# ── input não-bloqueante (cross-platform) ───────────────────────────────────

def wait_key(timeout):
    """Espera uma tecla por até `timeout` segundos. Retorna o char ou None."""
    if os.name == "nt":
        import msvcrt
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):  # setas/F-keys: consome o 2º byte
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


# ── formatação ───────────────────────────────────────────────────────────────

def age(created_iso):
    created = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - created).days
    if days == 0:
        return "hoje", C.GRN
    if days < 3:
        return f"{days}d", C.GRN
    if days < 14:
        return f"{days}d", C.YEL
    return f"{days}d", C.RED


def ci_status(rollup):
    """Retorna (glifo, rodando) — `rodando` True se há build em andamento."""
    if not rollup:
        return f"{C.GRY}·{C.RESET}", False
    states = [(c.get("conclusion") or c.get("state") or "").upper() for c in rollup]
    if any(s in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED") for s in states):
        return f"{C.RED}✗{C.RESET}", False
    if any(s in ("PENDING", "IN_PROGRESS", "QUEUED", "") for s in states):
        return f"{C.YEL}⋯{C.RESET}", True
    return f"{C.GRN}✓{C.RESET}", False


def pending_reviewers(rich):
    """Logins/times de reviewers solicitados que ainda não revisaram."""
    out = []
    for r in rich.get("reviewRequests") or []:
        out.append(r.get("login") or r.get("name") or r.get("slug") or "?")
    return out


def review_label(decision):
    return {
        "APPROVED": (C.GRN, "aprovado"),
        "CHANGES_REQUESTED": (C.RED, "mudanças"),
        "REVIEW_REQUIRED": (C.YEL, "review"),
    }.get(decision or "", (C.GRY, "sem review"))


def link(url, text):
    """Hyperlink de terminal (OSC 8) — Ctrl+clique abre no navegador."""
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


# ── busca + render ────────────────────────────────────────────────────────────

def search_args():
    """Monta o filtro do `gh search prs` a partir das flags."""
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
    prs.sort(key=lambda p: p["createdAt"])  # mais antiga primeiro
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
    """Repos vigiados p/ builds: --builds-repo (repetível) > env PR_DASH_BUILD_REPOS > padrão vazio."""
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
    """Workflow runs em andamento — staging (push→main) E release de prod (push→tag);
    nenhum dos dois aparece como check de PR aberta, por isso a consulta é separada."""
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


def render(rows, hidden_count, interactive, fmode="all", builds=None):
    """Retorna (texto, labels) onde labels mapeia tecla → pr_key."""
    labels = {}
    lines = []
    bar = f"{C.GRY}{'─' * W}{C.RESET}"
    now = datetime.now().strftime("%H:%M")
    titulo = {"ready": "PRONTAS PRA MERGE", "conflicts": "COM CONFLITO"}.get(fmode, "PRs ABERTAS")
    lines.append(f"{C.BOLD}{C.BLU} {LOGO}{C.RESET}{C.GRY} │ {C.RESET}{C.BOLD}{C.CYN}PR DASHBOARD{C.RESET}")
    head = f"{C.BOLD}{C.WHT} {titulo} · {len(rows)}{C.RESET}"
    if hidden_count:
        head += f"{C.DIM} ({hidden_count} ocultas){C.RESET}"
    lines.append(head)
    lines.append(f"{C.DIM} atualiz. {now}{C.RESET}")
    lines.append(bar)

    if builds:
        lines.append(f"{C.BOLD}{C.YEL} BUILDS RODANDO · {len(builds)}{C.RESET}")
        for repo, r in builds:
            short = repo.split("/")[-1]
            glyph = "⟳" if r.get("status") == "in_progress" else "⋯"
            prod = "production" in (r.get("workflowName") or "").lower()
            tag = f"{C.RED} prod{C.RESET}" if prod else f"{C.CYN} homolog{C.RESET}"
            top = f" {C.YEL}{glyph}{C.RESET} {C.WHT}{short}{C.RESET}{tag}"
            url = r.get("url")
            lines.append(link(url, top) if url else top)
            lines.append(f"   {C.DIM}{r.get('workflowName', '?')} · {r.get('headBranch', '?')}{C.RESET}")
        lines.append(bar)

    if not rows:
        alvo = {"ready": "pronta pra merge", "conflicts": "com conflito"}.get(fmode, "aberta")
        lines.append(f"{C.DIM} Nenhuma PR {alvo} visível. 🎉{C.RESET}")
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
            "CLEAN": f" {C.GRN}⇪{C.RESET}",            # pronta pra merge
            "DIRTY": f" {C.RED}⚠{C.RESET}",            # conflito de merge
            "BEHIND": f" {C.YEL}↺{C.RESET}",            # branch atrás da base
        }.get(rich.get("mergeStateStatus"), "")        # UNKNOWN/BLOCKED → sem marcador
        lines.append(f" {lbl}{acol}{a:>4}{C.RESET} {ci} {rcol}{rlbl}{C.RESET}{merge}")
        if RICH:
            revs = pending_reviewers(rich)
            if revs:
                nomes = ", ".join(revs[:3]) + (f" +{len(revs) - 3}" if len(revs) > 3 else "")
                lines.append(f" {C.YEL}⏳ aguardando {nomes}{C.RESET}")
            if ci_running:
                lines.append(f" {C.YEL}⟳ build rodando{C.RESET}")
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
            tag("a", "todas", fmode == "all"),
            tag("m", "prontas", fmode == "ready"),
            tag("c", "conflito", fmode == "conflicts"),
        ])
        lines.append(f" {filtros}")
        lines.append(f"{C.DIM} espaço=recarrega · tecla=oculta · r=restaura · q=sai{C.RESET}")

    return "\n".join(lines), labels


# ── reaper de worktrees stale ──────────────────────────────────────────────────
# Devora worktrees que JÁ não guardam nada de novo: PR merged + 0 commits à frente
# do upstream + working tree limpa. Antes de remover, grava um log de recreate
# (path/branch/sha/PR) — o fallback "em caso de emerda". Default = só mostra;
# devorar de verdade exige --reap-worktrees.

REAP_LOG = os.path.join(os.path.expanduser("~"), ".pr-dashboard-reaped.json")
KEEP_FILE = os.path.join(os.path.expanduser("~"), ".pr-dashboard-keep.json")
# checkouts persistentes de infra: NUNCA reapar, mesmo limpos+merged. Nome com
# 'homolog'/'prod' ou sufixo '-main' = cópia de trabalho viva, não worktree throwaway.
PROTECT_SUBSTR = ("homolog", "prod")


def is_protected(name):
    """True se a worktree é checkout persistente (infra) — nunca reapar."""
    low = name.lower()
    if low.endswith("-main") or any(s in low for s in PROTECT_SUBSTR):
        return True
    try:
        with open(KEEP_FILE, encoding="utf-8") as f:
            return name in set(json.load(f))
    except (OSError, ValueError):
        return False


def _git(d, args):
    """`git -C d ...` → stdout strip, ou None se o git falhar."""
    r = subprocess.run(["git", "-C", d, *args], capture_output=True,
                       text=True, encoding="utf-8")
    return r.stdout.strip() if r.returncode == 0 else None


def _unlink_reparse(p):
    """Remove um junction/symlink (a LIGAÇÃO, nunca o alvo). True se removeu.
    No Windows as worktrees costumam ter node_modules como junction compartilhado;
    seguir o link num rmtree apagaria o node_modules real — por isso readlink+rmdir."""
    try:
        os.readlink(p)        # só não lança se for reparse point (junction/symlink)
    except OSError:
        return False          # não é link → não mexe
    try:
        os.rmdir(p)           # junction de diretório: remove só a ligação
    except OSError:
        try:
            os.unlink(p)      # symlink de arquivo
        except OSError:
            return False
    return True


def _cleanup_dir(path):
    """Remove o diretório-fantasma que o `git worktree remove` deixa pra trás
    (sobra junction tipo node_modules + dirs vazios gitignored). NUNCA apaga
    conteúdo real ignorado (.env, dist/…): se houver, mantém e devolve o motivo.
    Retorna (limpo: bool, leftover: str|None)."""
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
        return False, "conteúdo ignorado: " + ", ".join(sorted(leftovers)[:4])
    try:
        os.rmdir(path)
    except OSError as e:
        return False, str(e)
    return True, None


def wt_root():
    """Raiz das worktrees irmãs. --reap-root FLAG > env PR_DASH_WT_ROOT."""
    if "--reap-root" in sys.argv:
        i = sys.argv.index("--reap-root")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    root = os.environ.get("PR_DASH_WT_ROOT", "").strip()
    if not root:
        print("Defina PR_DASH_WT_ROOT ou use --reap-root <caminho>.", file=sys.stderr)
        sys.exit(1)
    return root


def repo_slug(d):
    """owner/repo a partir do remote origin (ssh ou https), ou None."""
    url = (_git(d, ["remote", "get-url", "origin"]) or "").rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    parts = [p for p in url.replace(":", "/").split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def main_repo_of(d):
    """Caminho absoluto do repo PRINCIPAL dono desta worktree (pra rodar o remove)."""
    common = _git(d, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    if common and os.path.basename(common.rstrip("/\\")) == ".git":
        return os.path.dirname(common.rstrip("/\\"))
    return None


def scan_worktrees():
    """Varre wt_root() e diagnostica cada worktree LINKED (.git = arquivo)."""
    root = wt_root()
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    merged_cache, open_cache = {}, {}   # por repo, pra não martelar a API
    out = []
    for name in entries:
        d = os.path.join(root, name)
        if not os.path.isfile(os.path.join(d, ".git")):
            continue  # repo principal (.git = dir) ou não-git → ignora
        branch = _git(d, ["rev-parse", "--abbrev-ref", "HEAD"])
        if is_protected(name):
            out.append({"name": name, "path": d, "branch": branch or "?",
                        "reap": False, "why": "protegida (infra/checkout vivo)"})
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
            why = "suja (mudanças locais)"
        elif open_pr:
            why = "PR ainda aberta"
        elif not slug:
            why = "sem remote origin"
        elif not merged_pr:
            why = "sem PR merged"
        elif not upstream:
            why = "sem upstream (não pushada?)"
        elif ahead is None:
            why = "ahead indeterminado"
        elif ahead > 0:
            why = f"{ahead} commit(s) à frente do remoto"
        out.append({"name": name, "path": d, "branch": branch, "repo": slug,
                    "head": head, "ahead": ahead,
                    "pr": (merged_pr or {}).get("number"),
                    "reap": why is None, "why": why or "reapável"})
    return out


def reap(items, do_it):
    """Remove (se do_it) as worktrees reapáveis; sempre devolve recs com recreate.
    Grava no REAP_LOG só as efetivamente removidas. Retorna mapa path → rec."""
    recs = {}
    try:
        with open(REAP_LOG, encoding="utf-8") as f:
            log = json.load(f)
    except (OSError, ValueError):
        log = []
    ts = datetime.now(timezone.utc).isoformat()
    targets = [x for x in items if x["reap"]]
    if "--reap-limit" in sys.argv:                # come no máximo N por vez
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
                rec["error"] = "repo principal não localizado"
            else:
                r = subprocess.run(["git", "-C", main, "worktree", "remove", w["path"]],
                                   capture_output=True, text=True, encoding="utf-8")
                if r.returncode == 0:
                    rec["removed"] = True
                    subprocess.run(["git", "-C", main, "worktree", "prune"],
                                   capture_output=True, text=True)
                    clean, leftover = _cleanup_dir(w["path"])  # mata o dir-fantasma
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
    lines = [f"{C.BOLD}{C.BLU} {LOGO}{C.RESET}{C.GRY} │ {C.RESET}{C.BOLD}{C.CYN}WORKTREES{C.RESET}"]
    titulo = "DEVORADAS" if do_it else "REAPÁVEIS"
    lines.append(f"{C.BOLD}{C.WHT} {titulo} · {len(reapable)}{C.RESET}"
                 f"{C.DIM} de {len(items)} worktrees{C.RESET}")
    lines.append(bar)
    for w in reapable:
        rec = recs.get(w["path"])
        if do_it and rec is not None:
            mark = (f"{C.GRN}✓ devorada{C.RESET}" if rec.get("removed")
                    else f"{C.RED}✗ falhou{C.RESET}")
        elif do_it:
            mark = f"{C.YEL}⌫ reapável (próxima leva){C.RESET}"
        else:
            mark = f"{C.RED}⌫ reapável{C.RESET}"
        rec = rec or {}
        pr = f" PR#{w['pr']}" if w.get("pr") else ""
        lines.append(f" {mark} {C.WHT}{w['name']}{C.RESET}{C.DIM}{pr}{C.RESET}")
        lines.append(f"   {C.GRY}{w['branch']}{C.RESET}")
        if do_it and rec.get("error"):
            lines.append(f"   {C.RED}{rec['error']}{C.RESET}")
        if do_it and rec.get("leftover"):
            lines.append(f"   {C.YEL}⚠ dir mantido — {rec['leftover']}{C.RESET}")
    if reapable:
        lines.append(bar)
    lines.append(f"{C.DIM} MANTIDAS · {len(kept)}{C.RESET}")
    for w in kept:
        lines.append(f" {C.YEL}•{C.RESET} {C.WHT}{w['name']}{C.RESET} "
                     f"{C.DIM}— {w['why']}{C.RESET}")
    lines.append(bar)
    if reapable and not do_it:
        lines.append(f"{C.DIM} --reap-worktrees pra devorar · "
                     f"recreate fica em ~/.pr-dashboard-reaped.json{C.RESET}")
    return "\n".join(lines)


def clear_screen():
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def main():
    if "--clear-hidden" in sys.argv:
        save_hidden(set())
        print("PRs ocultas restauradas.")
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
    need_fetch = True

    while True:
        if need_fetch:
            all_rows = fetch()
            all_builds = fetch_builds() if BUILDS else []
            need_fetch = False
        visible = [r for r in all_rows if pr_key(r[1], r[0]["number"]) not in hidden]
        hidden_count = len(all_rows) - len(visible)  # só as ocultas pelo usuário
        if fmode == "ready":
            visible = [r for r in visible if r[2].get("mergeStateStatus") == "CLEAN"]
        elif fmode == "conflicts":
            visible = [r for r in visible if r[2].get("mergeStateStatus") == "DIRTY"]

        if watch:
            clear_screen()
        out, labels = render(visible, hidden_count, interactive, fmode, all_builds)
        print(out)

        if not watch:
            break

        if not interactive:
            time.sleep(interval)
            need_fetch = True
            continue

        ch = wait_key(interval)
        if ch is None:          # timeout → recarrega dados do GitHub
            need_fetch = True
            continue
        ch = ch.lower()
        if ch == "q":
            break
        elif ch == " ":          # espaço = recarrega já do GitHub (botão de reload)
            need_fetch = True
        elif ch == "a":
            fmode = "all"        # troca de filtro — re-render local, sem refetch
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
        # tecla desconhecida → só re-renderiza


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"{C.RED}Erro ao falar com o gh:{C.RESET} {e}", file=sys.stderr)
        print(f"{C.DIM}rode `gh auth status` pra confirmar que está logado.{C.RESET}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
