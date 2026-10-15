#!/usr/bin/env bash
# Run a local PowerShell script on LK_setup by streaming it over SSH stdin.
# No file is written on the lab PC, and no long EncodedCommand is needed.
#
# Usage: lk_ps.sh SCRIPT.ps1 [Name=Value ...]
#   Each Name=Value becomes a `$Name = 'Value'` line prepended to the script.
#   Values must not contain single quotes.
set -euo pipefail

SSH="${LK_SSH:-/c/Program Files/OpenSSH/ssh.exe}"
HOST="${LK_HOST:-LK_setup}"
script="$1"; shift

{
  printf '%s\n' '$ErrorActionPreference = "Stop"; $ProgressPreference = "SilentlyContinue"'
  printf '%s\n' '[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)'
  for kv in "$@"; do
    name="${kv%%=*}"; value="${kv#*=}"
    case "$value" in *"'"*) echo "single quote in $name" >&2; exit 2;; esac
    printf "\$%s = '%s'\n" "$name" "$value"
  done
  cat "$script"
} | timeout "${LK_TIMEOUT:-90}" "$SSH" -o BatchMode=yes -o ConnectTimeout=15 "$HOST" \
    'powershell -NoProfile -NonInteractive -Command "try { [ScriptBlock]::Create([Console]::In.ReadToEnd()).Invoke() } catch { $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }; [Console]::Error.WriteLine($e.Message); exit 1 }"'
