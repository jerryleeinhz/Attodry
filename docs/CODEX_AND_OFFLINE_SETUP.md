# Codex and offline setup

## Continue in Codex

Open this repository folder as a new Codex project, then start with:

```text
请先阅读 AGENTS.md 和 docs/PROJECT_HANDOFF.md，检查 git diff 与测试，然后继续 docs/DEVELOPMENT_STAGES.md 中当前阶段的下一项。只有需要真实硬件写入时才停下来让我确认。
```

This keeps future work anchored to the confirmed hardware and safety decisions. At the end of every completed task, ask Codex to update the stage documents.

## Windows environment without Conda

Install 64-bit Python 3.11 from the official Python installer. Enable the Python Launcher during installation. Then open PowerShell in the project directory:

```powershell
py -3.11 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
python -m unittest discover -s tests -v
```

Activation only affects the current PowerShell window. A new window must activate `.venv` again.

## Prepare a wheelhouse on an online Windows computer

Use the same Python minor version and CPU architecture as the offline control computer:

```powershell
py -3.11 -m venv .build-venv
.\.build-venv\Scripts\Activate.ps1
python -m pip install --upgrade pip build
python -m build --wheel
New-Item -ItemType Directory -Force wheelhouse
python -m pip download --dest wheelhouse .
Copy-Item .\dist\*.whl .\wheelhouse\
```

For the currently implemented extras, use `.[hardware,analysis]` when downloading
the wheelhouse. The wheelhouse is not final for real hardware until the exact SMU
models determine whether another vendor library is required.

Copy these items to the offline computer:

- the `wheelhouse` directory;
- `config/hardware.example.toml`;
- this documentation;
- the separately licensed/installed vendor attoDRY runtime and DLL.

Do not assume that Python wheels contain the LabVIEW runtime, VISA runtime, USB driver, or `attoDRYxyz64bit.dll`.

## Install from the wheelhouse offline

Create and activate a fresh environment, then install without contacting PyPI:

```powershell
py -3.11 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --no-index --find-links .\wheelhouse attodry-transport-control
```

Copy `config/hardware.example.toml` to `config/hardware.local.toml`, edit all `CHANGE_ME` values, and run only the read-only diagnostic stage first.

Before hardware commissioning, prove the installed environment with:

```powershell
python -m unittest discover -s tests -v
attodry-simulate --database .\run_data\release-check.sqlite --run-id release-check --inject-first-unlock
attodry-monitor --database .\run_data\release-check.sqlite --run-id release-check
```

Then follow `LAB_COMMISSIONING.md`; passing simulation does not authorize a real
instrument connection or write.
