# Detached, hidden, one-shot launch of a reviewed command on LK_setup.
# Run through ../../lk-setup-monitor/scripts/lk_ps.sh with these variables:
#   $TaskDir         new run directory that already holds manifest.json
#   $Command         exact command line to start (no single quotes)
#   $ManifestSha256  reviewed SHA-256 of $TaskDir\manifest.json (lowercase hex)
#   $Authorization   one sentence quoting what the user approved
#   $WorkDir         working directory (default: $TaskDir)
#   $DryRun          '1' runs every check and prints the plan without launching
#   $Coexist         optional 'pid@creationUtcIso;...' of running acquisitions the user
#                    explicitly approved to run alongside this one (identity re-checked)
foreach ($name in 'TaskDir', 'Command', 'ManifestSha256', 'Authorization') {
    if (-not (Get-Variable $name -ErrorAction SilentlyContinue)) { throw "missing variable $name" }
}
if (-not (Get-Variable WorkDir -ErrorAction SilentlyContinue)) { $WorkDir = $TaskDir }
$dry = (Get-Variable DryRun -ErrorAction SilentlyContinue) -and $DryRun -eq '1'

if ($TaskDir -match '\\Integration(\\|$)') { throw 'Refusing to launch inside the Integration experiment folder' }
$manifest = Join-Path $TaskDir 'manifest.json'
if (-not (Test-Path -LiteralPath $manifest)) { throw "manifest missing: $manifest" }
$actual = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $ManifestSha256.ToLowerInvariant()) { throw "Reviewed manifest changed: $actual" }
foreach ($name in 'launch_claim.json', 'launch-identity.json', 'launch-result.json') {
    if (Test-Path -LiteralPath (Join-Path $TaskDir $name)) { throw "Task already launched or claimed: $name. Use a new run directory." }
}

$approved = @{}
if (Get-Variable Coexist -ErrorAction SilentlyContinue) {
    foreach ($item in $Coexist -split ';' | Where-Object { $_ }) {
        $parts = $item -split '@', 2
        $approved[[int]$parts[0]] = ([DateTime]$parts[1]).ToUniversalTime().ToString('o')
    }
}
$coexisting = @()
$busy = @()
foreach ($p in Get-CimInstance Win32_Process -Filter "Name='python.exe'") {
    if ($p.CommandLine -match 'jupyter|ipykernel') { continue }
    $created = $p.CreationDate.ToUniversalTime().ToString('o')
    if ($approved.ContainsKey([int]$p.ProcessId) -and $approved[[int]$p.ProcessId] -eq $created) {
        $coexisting += "pid=$($p.ProcessId) created=$created $($p.CommandLine)"
    } else {
        $busy += $p
    }
}
if ($coexisting.Count -gt 0) { 'User-approved coexisting processes:'; $coexisting | ForEach-Object { "  $_" } }
if ($busy.Count -gt 0) {
    "Other non-Jupyter python processes are running:"
    $busy | ForEach-Object { "  pid=$($_.ProcessId) $($_.CommandLine)" }
    if (-not $dry) { throw 'Refusing to launch while other acquisitions may hold instruments; ask the user' }
}

"plan: $Command"
"cwd:  $WorkDir"
if ($dry) { 'DRY RUN: all checks passed, nothing launched'; return }

# CreateNew makes the claim atomic: a second launch attempt fails here.
$claim = @{ at_utc = [DateTime]::UtcNow.ToString('o'); command = $Command; manifest_sha256 = $actual; authorization = $Authorization; coexisting = $coexisting }
$stream = [IO.File]::Open((Join-Path $TaskDir 'launch_claim.json'), [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
$bytes = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -Depth 5 -InputObject $claim))
$stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true); $stream.Dispose()

$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $Command; CurrentDirectory = $WorkDir; ProcessStartupInformation = $startup }
$utf8 = [Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText((Join-Path $TaskDir 'launch-result.json'), (ConvertTo-Json -InputObject @{ ReturnValue = [int]$result.ReturnValue; ProcessId = [int]$result.ProcessId; command = $Command }), $utf8)
if ($result.ReturnValue -ne 0) { throw "Win32_Process.Create failed: $($result.ReturnValue)" }

$process = Get-CimInstance Win32_Process -Filter "ProcessId=$($result.ProcessId)"
if ($null -eq $process) { throw "Process $($result.ProcessId) exited before its identity could be recorded; read the run directory" }
$identity = @{
    pid = [int]$process.ProcessId; parent = [int]$process.ParentProcessId
    creation = $process.CreationDate.ToUniversalTime().ToString('o')
    command = [string]$process.CommandLine; manifest_sha256 = $actual
}
[IO.File]::WriteAllText((Join-Path $TaskDir 'launch-identity.json'), (ConvertTo-Json -InputObject $identity), $utf8)
ConvertTo-Json -Compress -InputObject $identity
