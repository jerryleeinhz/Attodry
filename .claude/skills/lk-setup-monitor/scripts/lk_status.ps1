# Read-only LK_setup snapshot. Run through lk_ps.sh. Writes nothing on the lab PC.
# Optional variables (set by lk_ps.sh Name=Value):
#   $RunDir  run/output directory to inspect
#   $Tail    lines to show from the newest log/jsonl files (default 15)
#   $Repo    git checkout to report HEAD/dirty state for
$root = 'C:\Users\LK_Setup\Yuanrong Li'
if (-not (Get-Variable Tail -ErrorAction SilentlyContinue)) { $Tail = 15 }
$Tail = [int]$Tail

function Get-Area([string]$text) {
    if ($null -eq $text) { return 'unknown' }
    foreach ($area in 'Integration-photonics', 'Integration', 'NKT') {
        if ($text -match [regex]::Escape("$root\$area") -or $text -match [regex]::Escape(("$root\$area" -replace '\\', '/'))) { return $area }
    }
    if ($text -match 'jupyter|ipykernel') { return 'jupyter' }
    return 'other'
}

"== LK_setup snapshot $([DateTime]::UtcNow.ToString('o')) on $env:COMPUTERNAME"

"`n== python processes (area is inferred from the command line only; it does not prove ownership)"
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'")
if ($procs.Count -eq 0) { 'none' }
foreach ($p in $procs) {
    $cpu = [math]::Round(($p.KernelModeTime + $p.UserModeTime) / 1e7, 1)
    "pid=$($p.ProcessId) parent=$($p.ParentProcessId) created=$($p.CreationDate.ToUniversalTime().ToString('o')) area=$(Get-Area $p.CommandLine) cpu_s=$cpu"
    "    $($p.CommandLine)"
}

if (Get-Variable Repo -ErrorAction SilentlyContinue) {
    "`n== repo $Repo"
    git -C $Repo --no-optional-locks rev-parse --abbrev-ref HEAD
    git -C $Repo --no-optional-locks log -1 --format='%h %cI %s'
    git -C $Repo --no-optional-locks status --porcelain --untracked-files=no
}

if (Get-Variable RunDir -ErrorAction SilentlyContinue) {
    "`n== run dir $RunDir"
    if (-not (Test-Path -LiteralPath $RunDir)) { "missing"; return }

    foreach ($name in 'launch-identity.json', 'launch.json') {
        $path = Join-Path $RunDir $name
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $id = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
        $idPid = if ($id.pid) { [int]$id.pid } else { [int]$id.ProcessId }
        $idCreated = if ($id.creation) { $id.creation } else { $id.creation_utc }
        $live = Get-CimInstance Win32_Process -Filter "ProcessId=$idPid"
        $match = $null -ne $live -and $live.CreationDate.ToUniversalTime().ToString('o') -eq ([DateTime]$idCreated).ToUniversalTime().ToString('o')
        "launch record ${name}: pid=$idPid created=$idCreated -> " + $(if ($match) { 'RUNNING (pid and creation time match)' } elseif ($live) { 'pid reused by another process, ours has exited' } else { 'exited' })
    }

    "`n-- newest files"
    Get-ChildItem -LiteralPath $RunDir -Recurse -File |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 15 |
        ForEach-Object { '{0}  {1,12}  {2}' -f $_.LastWriteTimeUtc.ToString('u'), $_.Length, $_.FullName.Substring($RunDir.Length).TrimStart('\') }

    $logs = Get-ChildItem -LiteralPath $RunDir -Recurse -File |
        Where-Object { $_.Extension -in '.jsonl', '.log', '.err', '.out' } |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 2
    foreach ($log in $logs) {
        "`n-- tail $($log.Name) (age $([int]([DateTime]::UtcNow - $log.LastWriteTimeUtc).TotalSeconds) s)"
        Get-Content -LiteralPath $log.FullName -Tail $Tail -Encoding UTF8
    }
}
