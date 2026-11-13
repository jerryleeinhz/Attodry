"""Adapt the shared CLD controller to the existing guarded optical point engine.

Currents remain amperes and wavelength remains the diode's declared nominal
wavelength. No NKT percentage, filter or wavelength tuning is fabricated.
"""
from __future__ import annotations

from .nkt_config import NktError, number
from .photoresponse_cld import CldController
from .cld_output import protected_ld_off


class CldOpticalController(CldController):
    def protect_startup_off(self, *, authorize_writes=False, confirm_manual_route=False):
        if self.backend.is_hardware and (authorize_writes is not True or confirm_manual_route is not True):
            raise NktError("CLD writes and optical route require explicit authorization")
        if "startup_off" in self.record:
            raise NktError("CLD startup shutdown cannot be replayed")
        self.record["startup_off"] = {}
        return protected_ld_off(self.config, self.backend, record=self.record["startup_off"],
                                phase="cld_startup_off", on_event=self.on_event)

    @property
    def session_closed(self):
        return self.closed

    def _deadline(self, deadline):
        if deadline is not None and self.clock() >= deadline:
            raise NktError("CLD shared optical deadline exhausted")

    def begin_session(self, *, authorize_writes=False, confirm_manual_route=False):
        if self.backend.is_hardware and (not authorize_writes or not confirm_manual_route):
            raise NktError("CLD writes and optical route require explicit authorization")
        self._authorization = (authorize_writes, confirm_manual_route)
        if "startup_off" not in self.record:
            self.protect_startup_off(authorize_writes=authorize_writes,
                                     confirm_manual_route=confirm_manual_route)
        if not self.record["startup_off"].get("off_confirmed") or self.record["startup_off"].get("errors"):
            raise NktError("CLD startup shutdown was not verified")
        state = self.capture("cld_combination_preflight")
        self.healthy(state)
        if state["getLdOutputState"]:
            raise NktError("CLD re-enabled after startup shutdown; preflight blocked")
        self._freeze_temperature_protection(state)
        return state, None

    def configure_session(self):
        authorize_writes, confirm_manual_route = self._authorization
        self.begin(authorize_writes=authorize_writes, confirm_manual_route=confirm_manual_route)

    def prepare_point(self, point, *, deadline=None):
        self._deadline(deadline)
        if not self.owned or self.closed or self.failed:
            raise NktError("CLD optical session is not ready")
        number(point.current_a, "CLD current", 0, self.config.max_current_a)
        if point.wavelength_nm not in {p.wavelength_nm for p in self.config.points}:
            raise NktError("CLD cannot tune to another wavelength")
        result = self._prepare_validated_point(point)
        self._deadline(deadline)
        return result

    def verify_point(self, point=None, *, emitting, phase="cld_formal"):
        if point is not None and point != self.point:
            raise NktError("CLD effective point does not match the requested current/wavelength")
        return super().verify_point(emitting=emitting, phase=phase)

    def turn_off(self, *, deadline=None):
        self._deadline(deadline)
        # Source-off protection is also available before the first point exists.
        if not self.owned or self.closed or self.failed:
            raise NktError("CLD output state is not owned/known")
        self.backend.set("switchLdOutput", False)
        state = self.capture("cld_controlled_off")
        self.healthy(state)
        if state["getLdOutputState"]:
            raise NktError("CLD OFF was not confirmed")
        self._deadline(deadline)

    def start_emission(self, point, *, deadline=None):
        self._deadline(deadline)
        self.verify_point(point, emitting=False)
        result = self.set_emission(True, deadline=deadline)
        self._deadline(deadline)
        return result

    def finish_session(self):
        # Optical cleanup precedes electrical protection. Closing a default
        # VISA manager here could invalidate the lock-ins still needing cleanup.
        if self.owned:
            return self.finish(close_backend=False)
        cleanup = self.record["cleanup"]
        if "ownership_not_acquired" not in cleanup:
            cleanup["ownership_not_acquired"] = True
            cleanup["ld_only_cleanup"] = True
            # Normal current/TEC ownership was never acquired. Protect only LD,
            # using fresh readback, and retain the independent startup evidence.
            if self.record.get("startup_off", {}).get("identity_verified"):
                evidence = cleanup["shutdown"] = {}
                try:
                    protected_ld_off(self.config, self.backend, record=evidence,
                                     phase="cld_partial_open_cleanup", on_event=self.on_event,
                                     verified_identity=self.record["startup_off"])
                except BaseException as exc:
                    cleanup["errors"].append(f"{type(exc).__name__}: {exc}")
                cleanup["off_confirmed"] = evidence.get("off_confirmed") is True
            else:
                cleanup["off_confirmed"] = False
            cleanup["manual_verification_required"] = not cleanup["off_confirmed"] or bool(cleanup["errors"])
        self.closed = True
        return cleanup
