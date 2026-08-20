# Codex project instructions

Before changing code, read these files in order:

1. `docs/PROJECT_HANDOFF.md`
2. `docs/HARDWARE_AND_SAFETY.md`
3. `docs/DEVELOPMENT_STAGES.md`
4. `README.md`

Project rules:

- Treat all hardware operations as safety critical and fail closed.
- Do not connect to real instruments or issue write commands unless the user explicitly authorizes that stage.
- Preserve the vector-field invariant `sqrt(Bx^2 + Bz^2) <= 3 T`.
- Never infer that the field is zero after a communication failure. Record the last confirmed readback and require manual verification.
- Configure the two SR830 units by semantic role (`lockin_xx`, `lockin_xy`), not by model-specific numbered slots.
- SR830 #1 is the internal-reference excitation source and measures Vxx. SR830 #2 uses the TTL reference from #1, measures Vxy, and has its SINE OUT physically disconnected.
- Do not reintroduce PPMS, MultiPyVu, ETO, SR865A, or rotator control into the active hardware path.
- Keep raw rejected attempts for audit, but exclude them from default analysis.
- Do not commit vendor DLLs, local hardware addresses, experimental data, secrets, or `hardware.local.toml`.
- Run the relevant tests before claiming completion.
- After every completed feature, update `docs/DEVELOPMENT_STAGES.md` and the current-stage section in `docs/PROJECT_HANDOFF.md`.

