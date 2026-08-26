# Codex project instructions

Before changing code, read these files in order:

1. `docs/PROJECT_HANDOFF.md`
2. `docs/HARDWARE_AND_SAFETY.md`
3. `docs/DEVELOPMENT_STAGES.md`
4. `README.md`
5. `docs/PROJECT_MODULE_DEVELOPMENT_GUIDE.md`

Project rules:

- Configure the two SR830 units by semantic role (`lockin_xx`, `lockin_xy`), not by model-specific numbered slots.
- SR830 #1 is the internal-reference excitation source and measures Vxx. SR830 #2 uses the TTL reference from #1, measures Vxy, and has its SINE OUT physically disconnected.
- Do not reintroduce PPMS, MultiPyVu, ETO, SR865A, or rotator control into the active hardware path.
- Keep raw rejected attempts for audit, but exclude them from default analysis.
- Keep `hardware.local.toml`, local hardware addresses, experimental data, and secrets uncommitted.
- Run the relevant tests before claiming completion.
- After every completed feature, update `docs/DEVELOPMENT_STAGES.md` and the current-stage section in `docs/PROJECT_HANDOFF.md`.
