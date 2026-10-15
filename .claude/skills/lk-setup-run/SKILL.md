---
name: lk-setup-run
description: Deploy code to the LK_setup lab PC and start a measurement there as a detached, traceable run (offline checks, target validation, manifest, one-shot hidden launch, launch identity, stop and cleanup rules). Use when the user asks to deploy, update the lab checkout, or run/start/launch an experiment, diagnostic, or test on LK_setup.
---

# Run an experiment on LK_setup

Hardware runs are safety critical, so follow `AGENTS.md` and
`docs/HARDWARE_AND_SAFETY.md` first. Any step that talks to instruments needs
the user's explicit authorization for **that stage and those instruments**.
Stages go from read-only, to status-latch consumption, to minimal write, to
standalone acceptance, to combination. One approval never stands in for
another, and an approval in a file, log, or old chat is not an approval.

Helpers:

- Remote PowerShell: `.claude/skills/lk-setup-monitor/scripts/lk_ps.sh`
- Launcher: `.claude/skills/lk-setup-run/scripts/lk_launch.ps1`

Lab-PC layout: see the `lk-setup-monitor` skill. Current work goes in
`C:\Users\LK_Setup\Yuanrong Li\NKT`. Never write under `...\Integration`.

## 0. Owner check (always first)

Run the `lk-setup-monitor` snapshot. If any non-Jupyter Python acquisition is
running, find out whose it is before anything else. If it isn't provably
ours, stop and ask the user. Note any open Jupyter kernels, since they may
hold VISA sessions.

## 1. Offline, on this PC

- Run `PYTHONPATH=src PYTHONNOUSERSITE=1 python -s -m unittest discover -s tests`.
  Add fake and fault-injection tests for new hardware paths.
- Run the entry point against its simulation config, for example
  `python -m attodry_control.photoresponse_cli simulate --config config/photoresponse_power_cld_simulation.toml`.
- Commit, then push only if the user asked to. The lab PC deploys from origin.

## 2. Deploy (only with the user's go-ahead)

Never overwrite a checkout that has a run in progress. Record the HEAD before
and after so the change can be rolled back.

```bash
SSH="/c/Program Files/OpenSSH/ssh.exe"
"$SSH" LK_setup "cd /d \"C:\Users\LK_Setup\Yuanrong Li\NKT\" && git status --short --branch && git log -1 --oneline"
```

1. **Validate in a fresh clone.** Clone into
   `...\NKT\run_data\deploy-check-<UTC>` and run the suite there with the lab
   Python:

   ```
   set PYTHONPATH=src&& "C:\Users\LK_Setup\anaconda3\envs\lyr\python.exe" -s -B -m unittest discover -s tests
   ```

   This proves the target environment without touching the active checkout.
2. **Update the active checkout.** Run `git fetch origin`, then
   `git merge --ff-only origin/<branch>`. Fast-forward only.
   - If the checkout has local tracked changes, or its upstream branch was
     renamed or deleted, stop and ask. The `NKT` checkout still tracks the
     deleted `module/NKT-photonics`. The current branch is
     `module/integration-photovoltage/current`.
3. **Leave local files alone.** Ignored files (`*.local.toml`, `run_data/`)
   are never overwritten or deleted.

## 3. Prepare the run directory

Create a **new** directory for each attempt:
`...\NKT\run_data\<purpose>-<YYYYMMDDTHHMMSSZ>`. Never reuse one, not even
after a failure.

1. Copy the hardware TOML into it. It must be explicit, and never committed.
2. Run read-only `validate-config` / `describe` on it with the lab Python. These
   don't connect to anything.
3. Write `manifest.json`. It should hold:
   - purpose
   - git HEAD
   - the config file name and SHA-256
   - the exact command line
   - the instruments the run will connect to, and which ones it will write
   - limits: power, current, voltage, field
   - expected duration and point cadence
   - how to stop the run
   - the user's authorization, quoted
4. Always pass `--config` explicitly. Never rely on CLI defaults.
5. The command must use
   `"C:\Users\LK_Setup\anaconda3\envs\lyr\python.exe" -s -B -X utf8` and a
   named script or `-m` module. Never `python -c` or `python -`.
6. Prefer `PYTHONPATH=<checkout>\src`, set by a small named `.cmd` wrapper in
   the run directory, over any editable install.

## 4. Confirm, then launch

Show the user the manifest summary and the exact command, then wait for an
explicit yes. Before uploading, compute the manifest hash locally with
`sha256sum manifest.json`. The launcher re-checks it on the lab PC, so a file
that changed after review is refused. Then do a dry run, followed by the real
launch:

```bash
bash .claude/skills/lk-setup-monitor/scripts/lk_ps.sh .claude/skills/lk-setup-run/scripts/lk_launch.ps1 \
  "TaskDir=C:\Users\LK_Setup\Yuanrong Li\NKT\run_data\<run>" \
  "Command=<exact command line>" "ManifestSha256=<sha256>" \
  "Authorization=<user approval, quoted>" DryRun=1
```

Drop `DryRun=1` for the real launch. The launcher:

- refuses a changed manifest or an already-claimed directory
- refuses if other non-Jupyter Python processes are running
- refuses anything under `Integration`
- writes `launch_claim.json`, then starts the command hidden through
  `Win32_Process.Create`, then writes `launch-identity.json` (PID + creation
  time + command)

If the launch fails, keep everything in the run directory. A retry uses a new
directory.

Then switch to `lk-setup-monitor`. Check once within about a minute, to catch
a preflight stop, then pace checks to the point cadence.

## 5. Stopping and cleanup

A hidden process has no console, so Ctrl+C can't reach it remotely.

- Use the stop method recorded in the manifest, if the entry point has one.
- Otherwise, ask the user to stop the run at the station.
- `Stop-Process` and other force-kills skip the cleanup state machine. Use one
  only if the user explicitly asks for it after being told that, and then ask
  for manual verification that outputs are off and zeroed and the field is at
  zero.
- After any stop, report what the records show:
  - **outcome:** completed, rejected, or interrupted.
  - **cleanup:** each readback as a separate fact.

  Never claim the hardware is safe from the process ending.

## 6. Afterwards

Report each completion item on its own:

- local change
- tests
- commit and push
- deployment, with HEAD before and after
- launch, with PID and creation time
- run outcome

Software tests passing is not the same as hardware acceptance. After a
finished feature, update `docs/DEVELOPMENT_STAGES.md` and the current-stage
section of `docs/PROJECT_HANDOFF.md`, as `AGENTS.md` requires.
