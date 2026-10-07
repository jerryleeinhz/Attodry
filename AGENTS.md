# Codex project instructions

Before changing code, read these files in order:

1. `docs/PROJECT_HANDOFF.md`
2. `docs/HARDWARE_AND_SAFETY.md`
3. `docs/DEVELOPMENT_STAGES.md`
4. `README.md`
5. `docs/PROJECT_MODULE_DEVELOPMENT_GUIDE.md`

Project rules:

- Treat all hardware operations as safety critical and fail closed.
- Do not connect to real instruments or issue write commands unless the user explicitly authorizes that stage.
- Preserve strict nominal targets and exact float32 commands: standalone and combination single-X/single-Z plans use the configured X/Z limits (at most X 3 T / Z 9 T). A complete plan using both axes, even at separate points, is vector mode and all targets satisfy the configured resultant limit (at most 3 T).
- Operator-approved readback policy (2026-09-30): derive single X/single Z/vector mode from the complete requested plan; single-axis requires every inactive-axis target exactly zero. Actual readbacks use a fixed 0.5 mT margin at nominal limits <=3 T, vector magnitude <=nominal+0.5 mT, and an independent inactive-axis +/-0.5 mT guard. Record both raw axes and the versioned policy; do not round residuals to zero. Standalone Z9 T is not extended. Generic diagnostics and historical records retain their declared rules. This software change is not real commissioning or permission to deploy/replay an experiment.
- Never infer that the field is zero after a communication failure. Record the last confirmed readback and require manual verification.
- Operator-approved tolerance follow-up (2026-10-01): explicit magnet.readback_tolerance_t (positive, at most 0.0015 T) replaces legacy field_tolerance_t and unifies actual-axis target/hold/formal errors, inactive single-axis readback, and verified-zero actual vector norm with stable dwell. Setpoint ACK has a separate at-most-0.0001 T tolerance and 30 s timeout. Nominal limits, fixed 0.5 mT active/vector boundary margin, strict float32 commands/corners/steps and communication/control/error gates remain. Legacy configs/archives retain declared v2 rules; new v3 archives resolved thresholds and raw axes. No real deployment or replay is implied.
- Configure electrical lock-ins by semantic role (`lockin_xx`, `lockin_xy`), not by model-specific numbered slots.
- SR830 #1 is the internal-reference excitation source and measures Vxx. SR830 #2 uses the TTL reference from #1, measures Vxy, and has its SINE OUT physically disconnected.
- User-approved exception (2026-10-07, offline implementation): `lockin_xx` remains an internal-reference SR830 excitation source. `lockin_xy` may be SR830 or an explicitly configured SR865A external-reference receiver, with SINE OUT physically disconnected. SR865A native SCAL/IRNG/status codes must never be interpreted as SR830 SENS/Reserve/LIAS; preserve XY source/phase/BLAZEX settings and fail closed on unknown status. This change does not authorize real commissioning or deployment.
- Do not reintroduce PPMS, MultiPyVu, ETO, optical source/capture, or rotator control into the active electrical hardware path.
- Keep raw rejected attempts for audit, but exclude them from default analysis.
- Keep `hardware.local.toml`, local hardware addresses, experimental data, and secrets uncommitted.
- Run the relevant tests before claiming completion.
- After every completed feature, update `docs/DEVELOPMENT_STAGES.md` and the current-stage section in `docs/PROJECT_HANDOFF.md`.
- Standing user delivery instruction (2026-10-07): after completing authorized code/documentation changes and relevant verification, commit and push to the existing `origin/codex/integration-four-module-scan`, then synchronize tracked files to `LK_setup` at `C:/Users/LK_Setup/Yuanrong Li/Integration`. Do not request repeat authorization. Inspect the target branch, local changes and acquisition processes first; use a fast-forward update and preserve local hardware configs, addresses, secrets, experimental data and user changes. Never change source used by an active acquisition or terminate user processes to enable synchronization. If safe synchronization is blocked, finish the push, report the blocker and defer the target update. Documentation-only updates may proceed when they do not change active source/configuration. This standing instruction authorizes software delivery, not instrument connection, write commands, commissioning or experiment replay.
