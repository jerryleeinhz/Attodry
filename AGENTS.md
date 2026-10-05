# Codex project instructions

Before changing code, read these files in order:

1. `docs/PROJECT_HANDOFF.md`
2. `docs/HARDWARE_AND_SAFETY.md`
3. `docs/DEVELOPMENT_STAGES.md`
4. `README.md`
5. `docs/PROJECT_MODULE_DEVELOPMENT_GUIDE.md`

Project rules:

- User-approved photonics exception (2026-10-05), only for explicit `pem_xy_xx_sine`: PEM REF OUT -> XY SR865A REF IN; XY SINE OUT+ -> XX SR830 REF IN; XX SINE OUT -> sample. XX uses external sine zero crossing. XY amplitude/DC and unused BlazeX mode are preserved without writes; only XX is the sample excitation source. Both SINE outputs are physically connected, but XY does not reach the sample. Original electrical/`pem_xx_xy` contracts below retain their own behavior. The photonics profile enforces strict resultant <=3 T with no legacy field margin. Read-only identity/settings checks were authorized; illuminated acquisition and new output writes require the corresponding explicit stage authorization.
- Treat all hardware operations as safety critical and fail closed.
- Do not connect to real instruments or issue write commands unless the user explicitly authorizes that stage.
- Preserve strict nominal targets and exact float32 commands: standalone and combination single-X/single-Z plans use the configured X/Z limits (at most X 3 T / Z 9 T). A complete plan using both axes, even at separate points, is vector mode and all targets satisfy the configured resultant limit (at most 3 T).
- Operator-approved readback policy (2026-09-30): derive single X/single Z/vector mode from the complete requested plan; single-axis requires every inactive-axis target exactly zero. Actual readbacks use a fixed 0.5 mT margin at nominal limits <=3 T, vector magnitude <=nominal+0.5 mT, and an independent inactive-axis +/-0.5 mT guard. Record both raw axes and the versioned policy; do not round residuals to zero. Standalone Z9 T is not extended. Generic diagnostics and historical records retain their declared rules. This software change is not real commissioning or permission to deploy/replay an experiment.
- Never infer that the field is zero after a communication failure. Record the last confirmed readback and require manual verification.
- Operator-approved tolerance follow-up (2026-10-01): explicit magnet.readback_tolerance_t (positive, at most 0.0015 T) replaces legacy field_tolerance_t and unifies actual-axis target/hold/formal errors, inactive single-axis readback, and verified-zero actual vector norm with stable dwell. Setpoint ACK has a separate at-most-0.0001 T tolerance and 30 s timeout. Nominal limits, fixed 0.5 mT active/vector boundary margin, strict float32 commands/corners/steps and communication/control/error gates remain. Legacy configs/archives retain declared v2 rules; new v3 archives resolved thresholds and raw axes. No real deployment or replay is implied.
- Configure the two SR830 units by semantic role (`lockin_xx`, `lockin_xy`), not by model-specific numbered slots.
- SR830 #1 is the internal-reference excitation source and measures Vxx. SR830 #2 uses the TTL reference from #1, measures Vxy, and has its SINE OUT physically disconnected.
- Do not reintroduce PPMS, MultiPyVu, ETO, SR865A, or rotator control into the active hardware path.
- Keep raw rejected attempts for audit, but exclude them from default analysis.
- Keep `hardware.local.toml`, local hardware addresses, experimental data, and secrets uncommitted.
- Run the relevant tests before claiming completion.
- After every completed feature, update `docs/DEVELOPMENT_STAGES.md` and the current-stage section in `docs/PROJECT_HANDOFF.md`.
