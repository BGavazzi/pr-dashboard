# Dashboard vertical de PRs — pra encostar na lateral do ultrawide.
# Wrapper que fixa UTF-8 e entra em modo watch por padrão.
#
# Uso:
#   .\pr-dash.ps1                 # watch 60s
#   .\pr-dash.ps1 30              # watch 30s
#   .\pr-dash.ps1 -Once           # render único
#   .\pr-dash.ps1 -Args '--org','<your-org>'      # repassa flags pro .py
param([int]$Interval = 60, [switch]$Once, [string[]]$Args = @())

$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$script = Join-Path $PSScriptRoot "pr_dashboard.py"
if ($Once) {
    python $script @Args
} else {
    python $script --watch $Interval @Args
}
