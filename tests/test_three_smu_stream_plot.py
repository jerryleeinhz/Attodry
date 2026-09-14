import json
import tempfile
import unittest
from urllib.request import urlopen

from attodry_control.keithley2400 import KeithleyReading
from attodry_control.three_smu import ThreeSmuSample, TimedReading
from attodry_control.three_smu_config import (
    ChannelPlan,
    ChannelRole,
    FinishAction,
    ScanMode,
    ThreeSmuScanPlan,
)
from attodry_control.three_smu_plot import (
    axis_options,
    axis_value,
    default_plot_specs,
    plot_sample_from_formal_sample,
    plot_xy,
    select_samples,
)
from attodry_control.three_smu_stream import ThreeSmuLivePublisher


def _plan() -> ThreeSmuScanPlan:
    return ThreeSmuScanPlan(
        mode=ScanMode.MULTI_SMU_MAP,
        samples_per_point=1,
        delay_s=0.0,
        serpentine=False,
        finish_action=FinishAction.ZERO_DISABLE,
        point_count=1,
        pulse_high_s=0.0,
        pulse_period_s=0.0,
        smu_bias=ChannelPlan(ChannelRole.SWEEP, False, points=(-0.1, 0.1)),
        gate_top=ChannelPlan(ChannelRole.SWEEP, False, points=(0.0, 1.0)),
        gate_bottom=ChannelPlan(ChannelRole.OFF, False),
    )


def _sample(point_index: int, top: float) -> ThreeSmuSample:
    return ThreeSmuSample(
        point_index=point_index,
        repeat_index=0,
        segment="forward",
        elapsed_s=float(point_index),
        coordinates={"smu_bias": -0.1 if point_index % 2 == 0 else 0.1, "gate_top": top},
        readings={
            "smu_bias": TimedReading(
                "2026-09-01T00:00:00+00:00",
                KeithleyReading(
                    voltage_v=-0.1 if point_index % 2 == 0 else 0.1,
                    current_a=(point_index + 1) * 1e-6,
                    source_setpoint=-0.1 if point_index % 2 == 0 else 0.1,
                    output_enabled=True,
                    compliance_trip=False,
                    status='0,"No error"',
                    status_query_consumed=True,
                ),
            ),
            "gate_top": TimedReading(
                "2026-09-01T00:00:00+00:00",
                KeithleyReading(
                    voltage_v=top,
                    current_a=1e-9,
                    source_setpoint=top,
                    output_enabled=True,
                    compliance_trip=False,
                    status='0,"No error"',
                    status_query_consumed=True,
                ),
            ),
        },
        clean=True,
        problems=(),
    )


def _next_event(response):
    event = None
    while True:
        line = response.readline().decode("utf-8").strip()
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            return event, json.loads(line[6:])


class ThreeSmuStreamPlotTests(unittest.TestCase):
    def test_loopback_stream_replays_and_finishes_without_hardware(self) -> None:
        publisher = ThreeSmuLivePublisher(port=0)
        publisher.start(_plan(), total_samples=4)
        try:
            with urlopen(publisher.endpoint, timeout=3) as response:
                event, payload = _next_event(response)
                self.assertEqual(event, "run_started")
                self.assertEqual(payload["active_roles"], ["smu_bias", "gate_top"])
                publisher.publish_sample(_sample(0, 0.0))
                event, payload = _next_event(response)
                self.assertEqual(event, "sample")
                self.assertAlmostEqual(
                    payload["readings"]["smu_bias"]["resistance_ohm"], -100000.0
                )
                publisher.finish(status="completed")
                event, payload = _next_event(response)
                self.assertEqual(event, "run_finished")
                self.assertEqual(payload["status"], "completed")
        finally:
            publisher.close()

    def test_cross_role_axes_make_a_gate_indexed_iv_family(self) -> None:
        samples = tuple(plot_sample_from_formal_sample(_sample(index, top)) for index, top in enumerate((0.0, 0.0, 1.0, 1.0)))
        options = dict(axis_options(samples))
        self.assertEqual(options["smu_bias: conductance G (S)"], "smu_bias.conductance_s")
        self.assertAlmostEqual(axis_value(samples[0], "smu_bias.conductance_s"), -1e-5)
        selected = select_samples(samples, slice_axis="gate_top.coordinate", slice_value=1.0)
        self.assertEqual(len(selected), 2)
        specs = default_plot_specs(samples, "multi_smu_map")
        self.assertEqual(specs[1]["series"], "gate_top.coordinate")
        for mode in (
            "time_trace", "bias_iv", "top_gate_transfer", "bottom_gate_transfer",
            "paired_gate", "multi_smu_map", "software_pulse",
        ):
            self.assertTrue(default_plot_specs(samples, mode), mode)
        try:
            figure = plot_xy(
                samples,
                x_axis="smu_bias.coordinate",
                y_axis="smu_bias.current_a",
                series_axis="gate_top.coordinate",
            )
        except RuntimeError as exc:
            self.skipTest(str(exc))
        self.assertEqual(len(figure.axes[0].lines), 2)
        figure.clear()


if __name__ == "__main__":
    unittest.main()
