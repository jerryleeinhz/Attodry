"""Optional OFF-state settings and the standalone fixed-zero monitoring plan."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from attodry_control.config import _parse_gate, RunMode, ConfigError
from attodry_control.three_smu_config import (OffState, ScanMode, SourceMode, FinishAction,
    ThreeSmuConfigError, ThreeSmuHardwareConfig, _parse_hardware_role, validate_plan_targets,
    load_three_smu_operation_config, ChannelRole)
from tests.test_three_smu_config import smu, plan, channel, BOTTOM_ONLY_OPERATION_TEXT


class OffStateConfigTests(unittest.TestCase):
    def hardware_table(self):
        return dict(model='Keithley2400',address='FAKE::1',source_mode='voltage',
                    max_abs_voltage_v=10.0,max_abs_current_a=1e-3,nplc=1.0,
                    source_auto_range=True,measure_auto_range=True,four_wire=False)

    def test_legacy_optional_and_all_mode_names_parse_in_both_paths(self):
        table=self.hardware_table()
        self.assertEqual(_parse_hardware_role(table,'gate_bottom').off_state,OffState.PRESERVE)
        self.assertEqual(_parse_gate(table,'gate_bottom',RunMode.HARDWARE,'gate_bottom').off_state,'preserve')
        for state in OffState:
            with self.subTest(state=state):
                table['off_state']=state.value
                self.assertEqual(_parse_hardware_role(table,'gate_bottom').off_state,state)
                self.assertEqual(_parse_gate(table,'gate_bottom',RunMode.HARDWARE,'gate_bottom').off_state,state.value)
        for value in ('unsupported',1,None):
            with self.subTest(value=value):
                table['off_state']=value
                with self.assertRaises(ThreeSmuConfigError): _parse_hardware_role(table,'gate_bottom')
                with self.assertRaises(ConfigError): _parse_gate(table,'gate_bottom',RunMode.HARDWARE,'gate_bottom')

    def monitor_plan(self,**changes):
        return plan(ScanMode.OFF_ZERO_MONITOR,gate_bottom=channel(ChannelRole.FIXED,fixed=0.0),
                    point_count=10,**changes)

    def test_fixed_zero_monitor_generates_only_zero_coordinates(self):
        config=replace(smu('gate_bottom','FAKE::1'),off_state=OffState.ZERO)
        points=validate_plan_targets(ThreeSmuHardwareConfig(gate_bottom=config),self.monitor_plan())
        self.assertEqual(len(points),10)
        self.assertTrue(all(p.coordinates=={'gate_bottom':0.0} and p.segment=='off_zero_monitor' for p in points))

    def test_monitor_rejects_sweeps_nonzero_hold_and_missing_roles(self):
        for channel_value in (channel(ChannelRole.SWEEP,points=(-1.,0.,1.)),
                              channel(ChannelRole.FIXED,fixed=1.),channel(ChannelRole.OFF)):
            with self.subTest(channel=channel_value), self.assertRaises(ThreeSmuConfigError):
                plan(ScanMode.OFF_ZERO_MONITOR,gate_bottom=channel_value)
        with self.assertRaises(ThreeSmuConfigError): self.monitor_plan(finish_action=FinishAction.HOLD)
        with self.assertRaises(ThreeSmuConfigError): self.monitor_plan(pulse_high_s=1.)

    def test_monitor_requires_explicit_zero_and_voltage_source(self):
        config=smu('gate_bottom','FAKE::1')
        for modified in (config,replace(config,off_state=OffState.HIGH_IMPEDANCE),
                         replace(config,off_state=OffState.ZERO,source_mode=SourceMode.CURRENT)):
            with self.subTest(config=modified),self.assertRaises(ThreeSmuConfigError):
                validate_plan_targets(ThreeSmuHardwareConfig(gate_bottom=modified),self.monitor_plan())

    def test_daily_toml_accepts_zero_setting_without_new_required_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'operation.toml'
            path.write_text(BOTTOM_ONLY_OPERATION_TEXT,encoding='utf-8')
            self.assertEqual(load_three_smu_operation_config(path).hardware.gate_bottom.off_state,OffState.PRESERVE)
            path.write_text(BOTTOM_ONLY_OPERATION_TEXT.replace('four_wire = false','four_wire = false\noff_state = "zero"'),encoding='utf-8')
            self.assertEqual(load_three_smu_operation_config(path).hardware.gate_bottom.off_state,OffState.ZERO)

