"""Explicitly authorized 6221-only commissioning; never opens a detector or laser."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tomllib

from .nkt_config import NktError, integer, keys, number
from .nkt_control import utc_now
from .photoresponse_devices import CurrentSource6221
from .photoresponse_hardware import address, identity
from .photoresponse_transport import visa_resources


def test_6221(path, *, authorize_connection=False, authorize_writes=False, on_event=None,
              manager_factory=None):
    if not authorize_connection or not authorize_writes:
        raise NktError("6221 test requires explicit connection and output-write authorization")
    with Path(path).open("rb") as stream:
        doc = tomllib.load(stream)
    s, plan = doc.get("photoresponse_6221", {}), doc.get("photoresponse_6221_test", {})
    keys(s, "photoresponse_6221", {"address", "expected_idn", "compliance_v", "max_step_a", "settle_s", "tolerance_a"})
    keys(plan, "photoresponse_6221_test", {"current_points_a", "max_abs_current_a", "timeout_ms", "output_directory"})
    address(s["address"])
    identity(s["expected_idn"], "6221")
    integer(plan["timeout_ms"], "timeout_ms", 1000, 120000)
    number(plan["max_abs_current_a"], "max_abs_current_a", 1e-13, .105)
    if not isinstance(plan["current_points_a"], list) or not 1 <= len(plan["current_points_a"]) <= 10000:
        raise NktError("Provide 1 to 10000 explicit current points")
    for point in plan["current_points_a"]:
        number(point, "current point", -plan["max_abs_current_a"], plan["max_abs_current_a"])
    options = {k:v for k,v in s.items() if k != "address"}
    # Constructor validates device constraints BEFORE opening VISA.
    CurrentSource6221(None, **options, max_current_a=plan["max_abs_current_a"])
    record = {"schema_version": 1, "measurement_type": "6221_dc_commissioning",
              "captured_at_utc": utc_now(), "completed": False, "outcome": "rejected",
              "measurement_config": {"source": s, "plan": plan}, "points": [], "error": None,
              "cleanup": None, "transcript": []}
    source = None
    try:
        with visa_resources({"6221": s["address"]}, timeout_ms=plan["timeout_ms"],
                authorize_connection=True, authorize_writes=True, on_event=on_event,
                manager_factory=manager_factory) as resources:
            t = resources["6221"]
            source = CurrentSource6221(t, **options, max_current_a=plan["max_abs_current_a"])
            try:
                record["preflight"] = source.snapshot()
                source.begin()
                for point in plan["current_points_a"]:
                    row = {"requested_current_a": point, "accepted": False, "readback": None}
                    record["points"].append(row)
                    source.set_bias(point)
                    row["readback"] = source.snapshot()
                    source.check(row["readback"])
                    row["accepted"] = True
                    if on_event: on_event({"phase": "commission_point", "point": deepcopy(row)})
                record["outcome"] = "completed"
            except (Exception, KeyboardInterrupt) as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["outcome"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "rejected"
            finally:
                record["last_confirmed_state_before_cleanup"] = deepcopy(source.last_confirmed_state)
                record["cleanup"] = source.cleanup() if source.owned else {"safe": False, "errors": ["Preflight did not acquire source"]}
                record["transcript"] = deepcopy(t.transcript)
                if not record["cleanup"]["safe"]:
                    record["outcome"] = "rejected"
    except (Exception, KeyboardInterrupt) as exc:
        record["error"] = (record["error"] or "") + f" {type(exc).__name__}: {exc}"
        record["outcome"] = "rejected"
    record["completed"] = record["outcome"] == "completed"
    if not record["completed"]:
        for row in record["points"]: row["accepted"] = False
    return record
