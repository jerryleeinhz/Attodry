---
name: lk-setup-monitor
description: Read-only status check of the LK_setup lab PC and of a running measurement on it (processes, launch identity, newest files, log tail, SQLite progress). Use when the user asks how an experiment is going, whether something is running on LK_setup, to watch or poll a run, or before any deployment or launch there.
---

# Monitor LK_setup (read-only)

Monitoring must never disturb a measurement. Every action in this skill is
read-only. Never open an instrument connection, never import the project's
drivers, and never stop, restart, or kill a process from here. Stopping a run
is covered in `lk-setup-run`.

## Facts

- SSH alias `LK_setup`. Its remote shell is `cmd`, so go through PowerShell.
- Experiment root: `C:\Users\LK_Setup\Yuanrong Li\`
  - `NKT`: current CLD/NKT/DC photoresponse work.
  - `Integration`: a separate experiment, possibly running. Only inspect it
    when the user asks, and never write there.
  - `Integration-photonics`: older PEM/lock-in line.
- Python: `C:\Users\LK_Setup\anaconda3\envs\lyr\python.exe -s -B`.

## 1. Snapshot

Run the bundled helper. It streams the script over SSH stdin, writes nothing on
the lab PC, and avoids `python -c`, which has tripped another task's owner
checks before.

```bash
bash .claude/skills/lk-setup-monitor/scripts/lk_ps.sh \
  .claude/skills/lk-setup-monitor/scripts/lk_status.ps1 \
  "RunDir=C:\Users\LK_Setup\Yuanrong Li\NKT\run_data\<run>" Tail=20
```

All variables are optional:

- `RunDir` adds launch-record matching, newest files, and a log tail.
- `Repo` reports the branch, HEAD, and dirty tracked files of a remote checkout.

Run with no variables to see only the process list.

## 2. Ownership: judge it, don't assume it

A process is "ours" only if its **PID, creation time, and command line** all
match a launch record (`launch-identity.json` or `launch.json`) that we wrote.
The helper prints `RUNNING (pid and creation time match)` only in that case.

- The `area=` label comes from the command line alone. It is a hint, not
  proof.
- Jupyter kernels (`ipykernel_launcher`) may hold instruments. Report them,
  but don't assume they are idle or that they are ours.
- If processes from another area are running, name them in the report and
  don't touch that area.

## 3. Progress from SQLite (optional)

Run data goes to SQLite in WAL mode. To read progress, use the bundled
`scripts/sqlite_snapshot.py`. It opens the database with `mode=ro` in a single
read transaction and covers both the `photo_*` and the legacy `runs`/`events`/
`conditions` tables.

The script must be a named file on the lab PC. The first time, ask the user
before copying it, then record its SHA-256:

```bash
"/c/Program Files/OpenSSH/ssh.exe" LK_setup "if not exist \"C:\Users\LK_Setup\Yuanrong Li\claude_tools\" mkdir \"C:\Users\LK_Setup\Yuanrong Li\claude_tools\""
scp .claude/skills/lk-setup-monitor/scripts/sqlite_snapshot.py "LK_setup:C:/Users/LK_Setup/Yuanrong Li/claude_tools/sqlite_snapshot.py"
```

After that, run it like this:

```bash
"/c/Program Files/OpenSSH/ssh.exe" LK_setup "\"C:\Users\LK_Setup\anaconda3\envs\lyr\python.exe\" -s -B \"C:\Users\LK_Setup\Yuanrong Li\claude_tools\sqlite_snapshot.py\" \"<db path>\" --rows 5"
```

If the remote copy's hash differs from the local file, stop and tell the user.
Don't silently overwrite it.

## 4. Watching a long run

Use `/loop` or a scheduled wake-up. Pick the interval from the cadence of the
measurement (one point every N minutes, so check every few points), not every
minute. On each check:

1. Run the snapshot.
2. Compare it with the previous check: is the launch process still alive, is
   the newest file still growing, has the accepted or condition count advanced?
3. Report a single line when nothing changed. Report in detail when any of
   these happens:
   - the process exited
   - the log has gone silent for more than about 3× the expected point time
   - a log line shows `error`, `interrupted`, `rejected`, `compliance`,
     `unlock`, `overload`, `trip`, or `cleanup`
   - the accepted count stops while the condition count grows

When the process exits, read the final summary or result JSON and report:

- **outcome:** completed, rejected, or interrupted.
- **counts:** accepted out of total conditions.
- **cleanup:** the readbacks in the record, quoted as written.

Cleanup is separate evidence: "output OFF", "setpoint 0", and "readback 0" are
three different facts. Don't infer the hardware is safe from the process
ending, and never infer zero field after a communication failure.

## Reporting

Tell the user what was observed and when (UTC). Quote numbers from the files
instead of paraphrasing them. Say plainly what could not be seen, such as
front-panel state, beam path, or wiring.
