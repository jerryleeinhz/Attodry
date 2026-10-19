"""Adapt the shared CLD controller to the existing guarded optical point engine.

Currents remain amperes and wavelength remains the diode's declared nominal
wavelength. No NKT percentage, filter or wavelength tuning is fabricated.
"""
from __future__ import annotations

from .nkt_config import NktError, number
from .photoresponse_cld import CldController


class CldOpticalController(CldController):
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
        state = self.capture("cld_combination_preflight")
        self.healthy(state)
        if state["getLdOutputState"]:
            raise NktError("CLD preflight requires laser OFF; no automatic takeover")
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
        return self.finish(close_backend=False)
