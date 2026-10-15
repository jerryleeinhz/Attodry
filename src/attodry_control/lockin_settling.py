"""One monotonic deadline per detector, shared by all acquisition settings."""
from __future__ import annotations

import math
import time


SETTLING_POLICY = "final-settings-one-settle-v1"
ROLES = ("lockin_xx", "lockin_xy")


class LockinSettling:
    def __init__(self, *, monotonic=None, sleep=None):
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.last_change = {}
        self.changes = []
        self.window_generation = 0

    def changed(self, reason, roles=ROLES):
        now = self.monotonic()
        for role in roles:
            self.last_change[role] = now
        self.changes.append({"reason": reason, "roles": list(roles), "completed_monotonic_s": now})

    @property
    def pending(self):
        return self.changes[self.window_generation:]

    def wait(self, required_by_role, record):
        now = self.monotonic()
        deadlines = {}
        for role, required in required_by_role.items():
            if not math.isfinite(required) or required < 0:
                raise ValueError("Lock-in settling duration must be finite and nonnegative")
            deadlines[role] = self.last_change.get(role, now) + required
        remaining = max(0.0, max(deadlines.values(), default=now) - now)
        audit = {"policy": SETTLING_POLICY, "checked_monotonic_s": now,
                 "required_s_by_role": required_by_role,
                 "last_change_monotonic_s_by_role": dict(self.last_change),
                 "deadline_monotonic_s_by_role": deadlines,
                 "already_elapsed_s_by_role": {r: max(0.0, now - self.last_change.get(r, now))
                                                for r in required_by_role},
                 "sleep_requested_s": remaining, "completed": False}
        record.setdefault("settling_waits", []).append(audit)
        if remaining:
            self.sleep(remaining)
        audit.update(completed=True, finished_monotonic_s=self.monotonic())
        return audit

    def wrap(self, instrument, role):
        return _SettingInstrument(instrument, role, self)


class _SettingInstrument:
    def __init__(self, instrument, role, settling):
        self.instrument, self.role_name, self.settling = instrument, role, settling

    def __getattr__(self, name):
        method = getattr(self.instrument, name)
        if not (name.startswith("set_") or name in {"write_fixed_settings", "configure_receiver"}):
            return method

        def call(*args, **kwargs):
            result = method(*args, **kwargs)
            # The sole excitation/reference source perturbs both detectors.
            roles = ROLES if name in {"set_sine_output", "set_minimum_sine_output",
                                     "set_internal_reference_frequency"} else (self.role_name,)
            self.settling.changed(self.role_name + "." + name, roles)
            return result
        return call
