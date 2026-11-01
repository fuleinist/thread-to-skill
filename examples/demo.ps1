# Reproduce the README demo with the synthetic fixtures in this repository.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Demo = if ($env:T2S_DEMO_DIR) { $env:T2S_DEMO_DIR } else { Join-Path $Root ".demo" }
$Ws = Join-Path $Demo ".thread2skill"
$Project = Join-Path $Demo "project"

if (Test-Path $Demo) { Remove-Item -Recurse -Force $Demo }
New-Item -ItemType Directory -Force -Path $Project | Out-Null

t2s --home $Ws init --provider offline

$threads = @()
foreach ($path in @(
    (Join-Path $Root "tests/fixtures/slack-export"),
    (Join-Path $Root "tests/fixtures/discord/incidents.json"),
    (Join-Path $Root "tests/fixtures/whatsapp/release-checklist.txt"),
    (Join-Path $Root "tests/fixtures/generic/threads.jsonl")
)) {
    $threads += (t2s --home $Ws ingest $path --json | ConvertFrom-Json)
}

foreach ($thread in $threads) {
    t2s --home $Ws distill $thread.id --provider offline | Out-Null
}

$slug = (t2s --home $Ws list --json | ConvertFrom-Json)[0].slug

t2s --home $Ws list
t2s --home $Ws suggest "integration tests are flaky with postgres"
t2s --home $Ws validate --all
t2s --home $Ws install $slug --harness claude,agents --scope project --project-root $Project
t2s --home $Ws hooks install --harness claude,cursor,deepseek --scope project --project-root $Project
t2s --home $Ws feedback $slug --outcome worked

Write-Output ""
Write-Output "Demo workspace: $Ws"
Write-Output "Installed skills: $(Join-Path $Project '.claude/skills')"