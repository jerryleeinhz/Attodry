"""Only ordinary measured-power deviations may request bounded window recovery."""
from .nkt_config import NktError


class OpticalTargetUnreachable(NktError):
    """A bounded actuator cannot reach its target; never a hardware-fault whitelist."""


class OpticalPowerDeviation(NktError):
    def __init__(self, message, *, evidence):
        super().__init__(message)
        self.evidence = evidence
