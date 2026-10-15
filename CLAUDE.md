# Claude Code instructions

The project rules live in `AGENTS.md` and apply to Claude Code too:

@AGENTS.md

## Claude-specific notes

- Tests use `unittest`, not pytest (pytest breaks module lookups in the
  photoresponse tests). Run with user site-packages disabled:

  ```bash
  PYTHONPATH=src PYTHONNOUSERSITE=1 python -s -m unittest discover -s tests
  ```

  Narrow the run with `-p 'test_photoresponse*.py'`. Use a writable workspace
  temp dir (`.test-tmp/`) if the default TEMP blocks file tests.
- Some docs are stale. Trust the newest dated entry in
  `docs/DEVELOPMENT_STAGES.md` over older status lines, and say so when they
  conflict.
- Changing source, deploying to the lab PC, committing, and pushing are four
  separate completion steps. Report each one on its own.

## Lab PC (LK_setup)

- Use the `lk-setup-run` skill to deploy or start a measurement, and the
  `lk-setup-monitor` skill to check one that is running. Both skills start
  with a read-only owner check.
- Never touch `C:\Users\LK_Setup\Yuanrong Li\Integration` unless the user
  names it. It belongs to a separate, possibly running, experiment.
- A running `python.exe` is not ours until its PID, creation time, and
  command line match a launch record we wrote.
