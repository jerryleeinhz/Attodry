"""OFF ZERO observations never use formal acquisition or enable an output."""
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from attodry_control.keithley2400 import KeithleyMonitorReading
from attodry_control.three_smu import ThreeSmuSafetyError, ThreeSmuSession
from attodry_control.three_smu_config import OffState, ScanMode
from tests.test_three_smu import FakeAdapter, fixed_plan, hardware


class MonitorAdapter(FakeAdapter):
    def __init__(self, *args, monitor_failure=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.monitor_failure = monitor_failure
        self.monitor_reads = 0

    def preflight(self):
        state = super().preflight()
        return replace(state, off_state=None if self.config is None else self.config.off_state.value)

    def configure(self, config):
        state = super().configure(config)
        return replace(state, off_state=config.off_state.value)

    def read(self):
        self.log.append(('formal_read_forbidden', self.role))
        raise AssertionError('OFF monitor must not call the formal read path')

    def read_off_zero_monitor(self, *, consume_status_queue=False):
        self.log.append(('off_zero_read', self.role, consume_status_queue))
        self.monitor_reads += 1
        if self.monitor_failure:
            raise self.monitor_failure
        if self.output or self.config is None or self.config.off_state is not OffState.ZERO:
            raise AssertionError('Monitoring requires configured OFF/ZERO')
        return KeithleyMonitorReading(
            identity=self.identity, source_mode=self.config.source_mode,
            source_setpoint=0., output_enabled=False, voltage_v=0.002,
            current_a=1e-9, compliance_limit=self.config.max_abs_current_a,
            source_range=1., measure_range=1., four_wire=False,
            compliance_trip=False, status='0,No error',
            status_queue_consumed=consume_status_queue, off_state='zero',
            reading_origin='latest_buffer', measurement_freshness='unknown',
            received_at_utc='2026-11-16T12:00:00+00:00', raw_response='0.002,1e-9',
        )


class Recorder:
    def __init__(self):
        self.events = []

    def event(self, kind, payload):
        self.events.append((kind, payload))


class OffZeroMonitorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        configured = hardware()
        self.hardware = replace(configured, smu_bias=replace(configured.smu_bias, off_state=OffState.ZERO))
        self.plan = replace(fixed_plan(), mode=ScanMode.OFF_ZERO_MONITOR,
                            point_count=2, samples_per_point=2)
        self.log = []
        self.adapters = {}
        self.monitor_failure = None

    def adapter_factory(self, role, config):
        self.log.append(('connect', role))
        adapter = MonitorAdapter(role, self.log, config, monitor_failure=self.monitor_failure)
        self.adapters[role] = adapter
        return adapter

    def session_open(self, configured, plan, **kwargs):
        return ThreeSmuSession.open(configured, plan, adapter_factory=self.adapter_factory,
                                    sleep=lambda _: None, **kwargs)

    def assert_no_formal_or_enable(self):
        self.assertFalse(any(item[0] == 'formal_read_forbidden' for item in self.log))
        self.assertFalse(any(item[0] == 'output' and item[2] is True for item in self.log))

    def metadata(self):
        files = list(self.output.rglob('monitor_metadata.json'))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text(encoding='utf-8'))

    def call_monitor(self):
        from attodry_control.smu_off_zero_monitor import run_off_zero_monitor
        self.opener = Mock(side_effect=self.session_open)
        with redirect_stdout(io.StringIO()):
            return run_off_zero_monitor(self.hardware, self.plan, output_dir=self.output,
                                       session_open=self.opener, print_fn=lambda *a, **k: None,
                                       sleep=lambda _: None, run_name='offline monitor')

    def test_formal_run_rejects_monitor_mode_before_any_setting_write(self):
        with self.session_open(self.hardware, self.plan,
                               authorize_writes=True, authorize_status_consumption=True) as session:
            before = list(self.log)
            with self.assertRaises(ThreeSmuSafetyError):
                next(session.run(output_dir=self.output))
            self.assertEqual(self.log, before)
        self.assert_no_formal_or_enable()
        self.assertFalse(list(self.output.rglob('data.csv')))

    def test_combination_point_entry_rejects_monitor_before_configuration(self):
        with self.session_open(self.hardware, self.plan,
                               authorize_writes=True, authorize_status_consumption=True) as session:
            before = list(self.log)
            with self.assertRaises(ThreeSmuSafetyError):
                session.begin_points(Recorder())
            self.assertEqual(self.log, before)
        self.assert_no_formal_or_enable()

    def test_prepare_reuses_session_adapters_and_remains_output_off(self):
        recorder = Recorder()
        with self.session_open(self.hardware, self.plan,
                               authorize_writes=True, authorize_status_consumption=True) as session:
            session.prepare_off_zero_monitor(recorder)
            adapter = session.adapters['smu_bias']
            self.assertIs(adapter, self.adapters['smu_bias'])
            self.assertEqual(adapter.config.off_state, OffState.ZERO)
            self.assertFalse(adapter.output)
            self.assertEqual(adapter.source, 0.)
            latest = adapter.read_off_zero_monitor(consume_status_queue=True)
            self.assertEqual(latest.measurement_freshness, 'unknown')
        self.assert_no_formal_or_enable()
        self.assertEqual(sum(item[0] == 'connect' for item in self.log), 1)
        self.assertEqual(sum(item[0] == 'close' for item in self.log), 1)

    def test_monitor_retains_observations_without_formal_samples_or_zero_certification(self):
        self.assertEqual(self.call_monitor(), 0)
        self.opener.assert_called_once()
        self.assertTrue(self.opener.call_args.kwargs['authorize_writes'])
        self.assertTrue(self.opener.call_args.kwargs['authorize_status_consumption'])
        self.assertEqual(self.adapters['smu_bias'].monitor_reads, 4)
        self.assert_no_formal_or_enable()
        metadata = self.metadata()
        self.assertIs(metadata['accepted'], False)
        self.assertEqual(metadata['formal_samples'], 0)
        self.assertIs(metadata['actual_zero_verified'], False)
        self.assertEqual(metadata['status'], 'completed')
        self.assertFalse(list(self.output.rglob('data.csv')))
        observations = '\n'.join(path.read_text(encoding='utf-8')
                                 for path in self.output.rglob('*.jsonl'))
        self.assertIn('latest_buffer', observations)
        self.assertIn('unknown', observations)
        self.assertIn('0.002', observations)
        self.assertEqual(sum(item[0] == 'close' for item in self.log), 1)

    def test_read_failure_records_failure_and_closes_same_connection(self):
        self.monitor_failure = OSError('injected monitor failure')
        try:
            code = self.call_monitor()
        except OSError:
            code = None
        if code is not None:
            self.assertNotEqual(code, 0)
        metadata = self.metadata()
        self.assertEqual(metadata['status'], 'failed')
        self.assertIs(metadata['accepted'], False)
        self.assertEqual(metadata['formal_samples'], 0)
        self.assertIs(metadata['actual_zero_verified'], False)
        self.assert_no_formal_or_enable()
        self.assertEqual(sum(item[0] == 'close' for item in self.log), 1)

    def test_keyboard_interrupt_is_monitored_terminal_not_an_accepted_scan(self):
        self.monitor_failure = KeyboardInterrupt()
        self.assertEqual(self.call_monitor(), 130)
        metadata = self.metadata()
        self.assertEqual(metadata['status'], 'interrupted')
        self.assertIs(metadata['accepted'], False)
        self.assertEqual(metadata['formal_samples'], 0)
        self.assertIs(metadata['actual_zero_verified'], False)
        self.assert_no_formal_or_enable()
        self.assertEqual(sum(item[0] == 'close' for item in self.log), 1)

    def monitor_toml(self):
        from tests.test_three_smu_config import BOTTOM_ONLY_OPERATION_TEXT
        return (BOTTOM_ONLY_OPERATION_TEXT
                .replace('mode = "bottom_gate_transfer"', 'mode = "off_zero_monitor"')
                .replace('role = "sweep"', 'role = "fixed"')
                .replace('four_wire = false', 'four_wire = false\noff_state = "zero"')
                .replace('points = [-1.0, 0.0, 1.0]', 'fixed = 0.0')
                .replace('output_directory = "runs"',
                         'output_directory = ' + json.dumps(self.output.as_posix())))

    def test_cli_run_uses_monitor_route_without_formal_stream(self):
        from attodry_control.three_smu_cli import run
        path = self.output / 'monitor.toml'
        path.write_text(self.monitor_toml(), encoding='utf-8')
        publisher = Mock(side_effect=AssertionError('Formal publisher must not start'))
        with redirect_stdout(io.StringIO()):
            code = run(['run', '--config', str(path)], session_open=self.session_open,
                       print_fn=lambda *a, **k: None, sleep=lambda _: None,
                       live_publisher_factory=publisher)
        self.assertEqual(code, 0)
        publisher.assert_not_called()
        self.assert_no_formal_or_enable()
        self.assertEqual(self.metadata()['formal_samples'], 0)

    def test_combination_parser_rejects_monitor_axis_before_any_instrument(self):
        from attodry_control.combination_hardware import load_hardware_combination
        path = self.output / 'combination.toml'
        text = self.monitor_toml() + '''
[project]
mode = "hardware"
database_path = "unused.sqlite"
[combination_scan]
backend = "hardware"
order = ["smu"]
samples_per_condition = 1
repeats = 1
run_name = "monitor is not formal"
note = "offline"
'''
        path.write_text(text, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'diagnostic only'):
            load_hardware_combination(path)


if __name__ == '__main__':
    unittest.main()
