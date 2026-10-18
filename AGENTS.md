# Codex project instructions

Before changing code, read these files in order:

1. `docs/PROJECT_HANDOFF.md`
2. `docs/HARDWARE_AND_SAFETY.md`
3. `docs/DEVELOPMENT_STAGES.md`
4. `README.md`
5. `docs/PROJECT_MODULE_DEVELOPMENT_GUIDE.md`
6. `docs/MEASUREMENT_PERFORMANCE.md`

Project rules:

- Current user-approved optics profile (2026-10-18): two SR830 units, internal-reference XX TTL OUT -> XY REF IN and Thorlabs chopper EXT REF IN; optionally add lockin_aux=SR865A as a third TTL receiver measuring a third sample voltage channel. XY/AUX SINE OUT stay disconnected. XX SINE OUT sample connection is declared separately. See docs/THREE_LOCKIN_GUIDE.md. Software/offline authorization does not authorize deployment, new instrument connection or illumination.
- Historical exception (2026-10-05), only when the operator explicitly selects `pem_xy_xx_sine`: PEM REF OUT -> XY SR865A REF IN; XY SINE OUT+ -> XX SR830 REF IN; XX SINE OUT -> sample. Preserve this existing profile and archives, including external sine zero crossing and preserved XY amplitude/DC. Do not activate this historical routing implicitly in the internal-XX/chopper/AUX profile.
- Treat all hardware operations as safety critical and fail closed.
- Do not connect to real instruments or issue write commands unless the user explicitly authorizes that stage.
- Preserve the photonics branch invariant `sqrt(Bx^2 + Bz^2) <= 3 T` for targets, commands and accepted actual readbacks. Keep exact float32 command checks and configured axis/transition limits; no historical single-Z 9 T allowance or readback margin relaxes this active photonics profile.
- Never infer that the field is zero after a communication failure. Record the last confirmed readback and require manual verification.
- Historical magnetic archives retain their recorded versioned policy and raw axes. Target/hold, setpoint ACK and actual-zero tolerances remain distinct from the strict active photonics resultant boundary. Never reinterpret old results using today's configuration.
- Configure the two SR830 units by semantic role (`lockin_xx`, `lockin_xy`), not by model-specific numbered slots.
- SR830 #1 provides the internal reference and measures Vxx. Its SINE OUT sample connection is declared explicitly. SR830 #2 uses the TTL reference from #1, measures Vxy, and has its SINE OUT physically disconnected.
- Do not reintroduce PPMS, MultiPyVu, ETO or rotator control. SR865A is limited to the explicitly selected historical profiles and the user-approved optional AUX profile.
- Keep raw rejected attempts for audit, but exclude them from default analysis.
- Keep `hardware.local.toml`, local hardware addresses, experimental data, and secrets uncommitted.
- Run the relevant tests before claiming completion.
- Review measurement speed for every feature: query/write counts, repeated verification, axis-entry stability, remaining waits, serial response ownership, timing evidence and failure/retry cost. Follow `docs/MEASUREMENT_PERFORMANCE.md`; never trade hardware safety or raw audit for speed.
- After every completed feature, update `docs/DEVELOPMENT_STAGES.md` and the current-stage section in `docs/PROJECT_HANDOFF.md`.
