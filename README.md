# `pr_dashboard.py` — dashboard vertical de PRs abertas

Coluna estreita (34 col) com as PRs abertas, pensada pra **encostar na lateral
de um monitor ultrawide** e deixar rodando em modo watch. Cada PR é um card
clicável; no modo watch é uma **TUI interativa** (filtra e oculta por tecla).
Zero credencial embutida — usa o `gh` CLI já autenticado.

## Pré-requisitos

- [`gh` CLI](https://cli.github.com/) instalado e logado:
  ```bash
  gh auth status   # deve mostrar "Logged in"
  gh auth login    # se ainda não estiver
  ```
- Python 3.10+ (sem dependências externas — só stdlib).
- Terminal com suporte a **hyperlinks OSC 8** pros links clicáveis
  (Windows Terminal, iTerm2, kitty, WezTerm, GNOME Terminal — todos suportam).

## Uso rápido

```bash
# render único das MINHAS PRs abertas
python pr_dashboard.py

# modo watch — TUI viva no canto da tela: refresca a cada 60s + responde a teclas
python pr_dashboard.py --watch
python pr_dashboard.py --watch 30      # refresca a cada 30s
```

No Windows tem o wrapper PowerShell que já fixa UTF-8:

```powershell
.\pr-dash.ps1            # watch 60s
.\pr-dash.ps1 30         # watch 30s
.\pr-dash.ps1 -Once      # render único
```

## Teclas (modo --watch, TTY)

A TUI responde a teclas sem reabrir. Filtros são estado ao vivo:

| Tecla | Ação |
|---|---|
| `espaço` | **recarrega já** do GitHub (botão de reload — não espera o intervalo) |
| `a` | mostra **todas** as PRs |
| `m` | só as **prontas pra merge** (`mergeStateStatus == CLEAN`) |
| `c` | só as **com conflito** (`mergeStateStatus == DIRTY`) |
| `r` | **restaura** todas as PRs ocultas |
| `q` | **sai** |
| letra/nº do card | **oculta** aquela PR (persistente — ver abaixo) |

O filtro ativo aparece destacado no rodapé. Trocar de filtro ou ocultar uma PR
re-renderiza **na hora** (sem nova chamada de API); a recarga do GitHub só
acontece quando o intervalo do `--watch` estoura.

## Flags

| Flag | O que faz |
|---|---|
| `--watch [segundos]` | Loop com TUI interativa (default 60s). |
| `--no-rich` | Pula CI/review/merge/diff → **1 chamada** de API só. Bem mais rápido. |
| `--ready` | Abre já filtrado em "prontas pra merge" (= tecla `m`). |
| `--conflicts` | Abre já filtrado em "com conflito" (= tecla `c`). |
| `--org <ORG>` | Cobre toda a org, não só PRs autoradas por você. |
| `--review-requested` | PRs aguardando o **seu** review (em vez das suas). |
| `--no-builds` | Não consulta o painel **BUILDS RODANDO** (1 chamada `gh run list` a menos por repo). |
| `--builds-repo <O/R>` | Repo extra pra vigiar builds (**repetível**). Override do default. |
| `--clear-hidden` | Restaura todas as PRs ocultas e sai. |
| `--no-input` | Watch sem teclado (refresh puro) — pra ambientes não-TTY. |

Exemplos:

```bash
python pr_dashboard.py --watch --ready                # abre nas prontas
python pr_dashboard.py --org <your-org> --watch       # toda a org, interativo
python pr_dashboard.py --review-requested             # esperando meu review
python pr_dashboard.py --clear-hidden                 # zera as ocultas
```

Via wrapper, repasse flags com `-Args`:

```powershell
.\pr-dash.ps1 -Args '--org','<your-org>','--ready'
```

## Como ler um card

```
 a  12d  ✗ mudanças ⚠     ← [label] · idade · CI · review · estado de merge
 owner/my-repo #42         ← repo + número (Ctrl+clique abre a PR)
 feat(companies): notes    ← título com wrap (também clicável)
 history (append-only)…
 +856/-1                   ← diff (linhas +/-)
────────────────────────
```

- **label** (`a`, `1`…): só no modo interativo — aperte pra ocultar o card.
- **Idade**: verde `<3d` · amarelo `<14d` · vermelho `≥14d`. Cards ordenados da
  **mais antiga pro topo**.
- **CI**: `✓` tudo verde · `✗` alguma falha · `⋯` rodando · `·` sem checks.
- **Review**: `aprovado` / `mudanças` / `review` (pendente) / `sem review`.
- **Merge**: `⇪` (verde) pronta pra merge · `⚠` (vermelho) conflito ·
  `↺` (amarelo) branch atrás da base · *(nada)* = `UNKNOWN`/`BLOCKED`.
- **`◌`** antes do repo = PR em **draft**.
- **`⏳ aguardando <nomes>`**: aparece quando há reviewer solicitado que ainda
  não revisou (até 3 nomes + contador). Tirado de `reviewRequests`.
- **`⟳ build rodando`**: aparece quando o CI da PR está em andamento.

## Painel BUILDS RODANDO

Acima dos cards, quando há **workflow runs em andamento** nos repos vigiados:

```
 BUILDS RODANDO · 1
 ⟳ owner/my-repo  homolog        ← glifo · repo · ambiente (homolog/prod)
   Deploy staging · main          ← workflow · branch/tag (Ctrl+clique abre o run)
```

Isso é **separado dos checks de PR de propósito**: se o CI roda em `push` (não
em PR), esses runs não aparecem como check de nenhuma PR aberta. O painel os
busca via `gh run list`. `prod` (vermelho) = workflow com "production" no nome;
senão `homolog` (ciano).

**Repos vigiados** (precedência): `--builds-repo` (repetível) → env
`PR_DASH_BUILD_REPOS="owner/a,owner/b"` → padrão vazio (painel desativado se
nenhum repo configurado). Desliga o painel explicitamente com `--no-builds`.

## PRs ocultas

Ocultar um card (tecla do label) some com ele e grava em
`~/.pr-dashboard-hidden.json` (chave `owner/repo#num`) — continua oculto nas
próximas execuções. `r` restaura tudo; `--clear-hidden` também. O contador
"(N ocultas)" no header conta só as ocultas por você, não as filtradas por
`--ready`/`--conflicts`.

## Notas de design

- **Sem creds no código.** O token vem do keyring do SO via `gh auth`. Dá pra
  versionar e compartilhar sem vazar nada.
- **Custo de API.** O modo default faz `1 + N` chamadas (lista + 1 `gh pr view`
  por PR pra CI/review/merge/diff) **+ 1 `gh run list` por repo vigiado** pro
  painel de builds. Trocar de filtro ou ocultar **não** chama a API (re-render
  local). Com muitas PRs, use `--no-rich`; pra cortar o painel, `--no-builds`.
- **Não é tempo real.** O watch dorme o intervalo inteiro entre recargas — mas
  `espaço` força um reload imediato sem esperar.

## Limitações conhecidas

- `gh search prs` não expõe `reviewDecision`/CI/merge diretamente → daí o
  `gh pr view` por PR. Sem ele (`--no-rich`) você perde essas colunas e os
  filtros `--ready`/`--conflicts`.
- **`mergeStateStatus` é eventualmente consistente.** O GitHub calcula a
  mergeabilidade de forma preguiçosa: a primeira consulta a uma PR pode voltar
  `UNKNOWN` (sem marcador, e fora de `--ready`/`--conflicts`). O próximo refresh
  do watch normalmente já resolve. Então uma PR "sumir" de `--ready` por um
  ciclo é esperado, não bug.
- A largura é fixa em `W = 34` no topo do script — ajuste lá se quiser outra.
```
