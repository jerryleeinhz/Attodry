"""Stateful 2400 range/compliance regressions; no real hardware imports."""
from dataclasses import replace
import unittest
import json
from pathlib import Path
import tempfile

from attodry_control.keithley2400 import QcodesKeithley2400, Keithley2400Error
from attodry_control.three_smu_config import SourceMode
from tests.test_keithley2400 import FakeQcodesInstrument, config


class RangeSensitiveInstrument(FakeQcodesInstrument):
    def __init__(self, *, mode=SourceMode.VOLTAGE):
        super().__init__()
        self.source_mode = mode
        self.range = 1e-3 if mode is SourceMode.VOLTAGE else 200.0
        self.compliance = 1e-6 if mode is SourceMode.VOLTAGE else 10.0
        self.auto = True
        self.range_sync = False
        self.errors = []
        self.reject_compliance = 0
        self.extra_compliance_error = None
        self.ignore_range = False
        self.ignore_auto = False
        self.compliancei = lambda value: self.set_compliance(float(f"{value:f}"))
        self.compliancev = lambda value: self.set_compliance(float(f"{value:f}"))

    def set_compliance(self, value):
        name = 'applied_compliance'
        self.calls.append((name, value))
        if value * (1 + 1e-9) < self.range * .001 or self.reject_compliance:
            if self.reject_compliance:
                self.reject_compliance -= 1
            self.errors.append('822,"Too small for sense range"')
        else:
            self.compliance = value
        if self.extra_compliance_error:
            self.errors.append(self.extra_compliance_error)

    def ask(self, command):
        self.calls.append(('ask', command))
        sense = 'CURR' if self.source_mode is SourceMode.VOLTAGE else 'VOLT'
        if command == ':OUTP?': return '1' if self.values['output']=='on' else '0'
        if command == ':SOUR:FUNC?': return 'VOLT' if sense=='CURR' else 'CURR'
        if command in {':SOUR:VOLT?', ':SOUR:CURR?'}: return str(self.values['volt' if 'VOLT' in command else 'curr'])
        if command == ':SYST:ERR?': return self.errors.pop(0) if self.errors else '0,"No error"'
        if command == f':SENS:{sense}:RANG?': return str(self.range * 1.05)
        if command == f':SENS:{sense}:PROT?': return str(self.compliance)
        if command == f':SENS:{sense}:PROT:RSYN?': return str(int(self.range_sync))
        if command.endswith(':RANG:AUTO?'): return str(int(self.auto))
        return self.responses[command]

    def write(self, command):
        super().write(command)
        sense = 'CURR' if self.source_mode is SourceMode.VOLTAGE else 'VOLT'
        protection = f':SENS:{sense}:PROT '
        if command.startswith(protection):
            self.set_compliance(float(command[len(protection):]))
        prefix = f':SENS:{sense}:RANG '
        if command.startswith(prefix) and not self.ignore_range:
            self.range = float(command[len(prefix):])
            self.auto = False
        if command.endswith(':RANG:AUTO ON') and not self.ignore_auto: self.auto = True


def adapter_for(instrument):
    adapter = QcodesKeithley2400('smu_bias', instrument)
    adapter.authorize_status_consumption()
    return adapter


class ComplianceInitializationTests(unittest.TestCase):
    def test_legacy_qcodes_format_reproduces_100na_rounding_to_zero(self):
        instrument = RangeSensitiveInstrument()
        instrument.range = 1e-6
        instrument.compliancei(1e-7)
        self.assertIn(("applied_compliance", 0.0), instrument.calls)
        self.assertTrue(instrument.errors[0].startswith("822"))

    def test_wire_precision_preserves_small_and_fractional_limits_in_both_modes(self):
        for mode, limits in ((SourceMode.VOLTAGE, (1e-9, 1e-7, 1.23456789e-6)),
                             (SourceMode.CURRENT, (.0002, .00123456789, 1.23456789))):
            for limit in limits:
                with self.subTest(mode=mode, limit=limit):
                    instrument = RangeSensitiveInstrument(mode=mode)
                    sense = "CURR" if mode is SourceMode.VOLTAGE else "VOLT"
                    settings = replace(config(), source_mode=mode, **{
                        "max_abs_current_a" if sense == "CURR" else "max_abs_voltage_v": limit})
                    result = adapter_for(instrument).configure(settings)
                    command = f":SENS:{sense}:PROT {limit:.17e}"
                    self.assertIn(("write", command), instrument.calls)
                    self.assertEqual(float(command.split()[-1]), limit)
                    self.assertEqual(result.compliance_limit, limit)
                    self.assertTrue(any(a.get("request") == command
                        for a in result.configuration_audit), result.configuration_audit)

    def test_protection_wire_write_failure_is_audited_without_retry(self):
        instrument = RangeSensitiveInstrument()
        command = f":SENS:CURR:PROT {1e-7:.17e}"
        instrument.fail_write = command
        adapter = adapter_for(instrument)
        with self.assertRaises(OSError):
            adapter.configure(replace(config(), max_abs_current_a=1e-7))
        self.assertEqual(instrument.calls.count(("write", command)), 1)
        self.assertTrue(any(a.get("request") == command and "OSError" in a.get("error", "")
                            for a in adapter.configuration_audit))
        self.assertNotIn(("output", "on"), instrument.calls)

    def test_100na_from_old_1ma_range_is_prepared_before_compliance(self):
        instrument = RangeSensitiveInstrument()
        adapter = adapter_for(instrument)
        readback = adapter.configure(replace(config(), max_abs_current_a=1e-7))
        self.assertEqual(readback.compliance_limit, 1e-7)
        self.assertTrue(instrument.auto)
        self.assertEqual(instrument.errors, [])
        self.assertNotIn(('output', 'on'), instrument.calls)
        prepare = next(i for i, c in enumerate(instrument.calls) if c[0]=='write' and c[1].startswith(':SENS:CURR:RANG '))
        compliance = instrument.calls.index(('applied_compliance', 1e-7))
        self.assertLess(prepare, compliance)

    def test_compatible_nominal_range_does_not_get_unnecessary_range_write(self):
        instrument = RangeSensitiveInstrument()
        instrument.range = 1e-4  # RANG? reports 105 uA, nominal is 100 uA.
        readback = adapter_for(instrument).configure(replace(config(), max_abs_current_a=1e-7))
        self.assertEqual(readback.compliance_limit, 1e-7)
        self.assertFalse(any(c[0]=='write' and c[1].startswith(':SENS:CURR:RANG ') for c in instrument.calls))

    def test_822_is_audited_and_retried_only_once(self):
        instrument = RangeSensitiveInstrument()
        instrument.reject_compliance = 1
        adapter = adapter_for(instrument)
        result = adapter.configure(replace(config(), max_abs_current_a=1e-7))
        self.assertEqual(result.compliance_limit, 1e-7)
        self.assertEqual(instrument.calls.count(('applied_compliance', 1e-7)), 2)
        self.assertTrue(any('822' in str(e.get('readback','')) for e in result.configuration_audit))
        self.assertEqual(instrument.errors, [])  # Configuration error never leaks into cleanup.

    def test_822_retry_stops_if_zero_off_state_is_lost(self):
        for setting in ("output", "volt"):
            with self.subTest(setting=setting):
                class LostIdle(RangeSensitiveInstrument):
                    def set_compliance(self, value):
                        super().set_compliance(value)
                        self.values[setting] = "on" if setting == "output" else .1
                instrument = LostIdle()
                instrument.reject_compliance = 1
                adapter = adapter_for(instrument)
                with self.assertRaisesRegex(Keithley2400Error, "output OFF and zero"):
                    adapter.configure(replace(config(), max_abs_current_a=1e-7))
                self.assertEqual(instrument.calls.count(("applied_compliance", 1e-7)), 1)
                self.assertEqual(
                    instrument.calls.count(("write", ":SENS:CURR:RANG 1e-06")), 1
                )
                self.assertTrue(any(
                    "822" in str(e.get("readback", ""))
                    for e in adapter.configuration_audit
                ))

    def test_repeated_822_fails_with_both_attempts_retained(self):
        instrument = RangeSensitiveInstrument()
        instrument.reject_compliance = 2
        adapter = adapter_for(instrument)
        with self.assertRaisesRegex(Keithley2400Error, 'compliance_attempt_2'):
            adapter.configure(replace(config(), max_abs_current_a=1e-7))
        self.assertEqual(instrument.calls.count(('applied_compliance', 1e-7)), 2)
        self.assertEqual(instrument.errors, [])
        self.assertNotIn(('output','on'), instrument.calls)
        self.assertEqual(adapter.configuration_audit[-1]['stage'], 'configuration_failed')

    def test_unrelated_error_blocks_retry_even_if_822_is_also_present(self):
        instrument = RangeSensitiveInstrument()
        instrument.reject_compliance = 1
        instrument.extra_compliance_error = '-222,"Data out of range"'
        adapter = adapter_for(instrument)
        with self.assertRaisesRegex(Keithley2400Error, '-222'):
            adapter.configure(replace(config(), max_abs_current_a=1e-7))
        self.assertEqual(instrument.calls.count(('applied_compliance',1e-7)), 1)
        self.assertEqual(instrument.errors, [])

    def test_ignored_range_preparation_stops_before_compliance(self):
        instrument = RangeSensitiveInstrument()
        instrument.ignore_range = True
        with self.assertRaisesRegex(Keithley2400Error, 'compatible compliance range'):
            adapter_for(instrument).configure(replace(config(), max_abs_current_a=1e-7))
        self.assertFalse(any(c[0]=='applied_compliance' for c in instrument.calls))

    def test_output_on_or_nonzero_source_never_gets_configuration_writes(self):
        for setting in ('output','volt'):
            with self.subTest(setting=setting):
                instrument = RangeSensitiveInstrument()
                instrument.values[setting] = 'on' if setting=='output' else .1
                with self.assertRaisesRegex(Keithley2400Error, 'output OFF and zero'):
                    adapter_for(instrument).configure(config())
                self.assertTrue(all(c[0]=='ask' for c in instrument.calls))

    def test_status_consumption_must_be_authorized_before_any_query(self):
        instrument = RangeSensitiveInstrument()
        with self.assertRaisesRegex(Keithley2400Error, 'authorization'):
            QcodesKeithley2400('smu_bias',instrument).configure(config())
        self.assertEqual(instrument.calls, [])

    def test_voltage_compliance_for_current_source_uses_compatible_range(self):
        instrument = RangeSensitiveInstrument(mode=SourceMode.CURRENT)
        adapter = adapter_for(instrument)
        result = adapter.configure(replace(config(),source_mode=SourceMode.CURRENT,max_abs_voltage_v=.0002))
        self.assertEqual(result.compliance_limit,.0002)
        self.assertTrue(instrument.auto)
        self.assertIn(('write',':SENS:VOLT:RANG 0.2'),instrument.calls)
        self.assertFalse(any(c[0]=='write' and c[1].startswith(':SENS:CURR:PROT ')
                             for c in instrument.calls))

    def test_communication_failure_is_not_retried_and_partial_audit_survives(self):
        instrument = RangeSensitiveInstrument()
        instrument.fail_write = ':SENS:CURR:RANG 1e-06'
        adapter = adapter_for(instrument)
        with self.assertRaises(OSError):
            adapter.configure(replace(config(),max_abs_current_a=1e-7))
        self.assertEqual(instrument.calls.count(('write',instrument.fail_write)),1)
        self.assertTrue(any('OSError' in e.get('error','') for e in adapter.configuration_audit))
        self.assertFalse(any(c[0]=='applied_compliance' for c in instrument.calls))

    def test_autorange_readback_mismatch_is_blocking(self):
        instrument = RangeSensitiveInstrument()
        instrument.auto = False
        instrument.ignore_auto = True
        with self.assertRaisesRegex(Keithley2400Error,'autorange was not confirmed'):
            adapter_for(instrument).configure(config())
        self.assertFalse(any(c[0]=='applied_compliance' for c in instrument.calls))

    def test_nonterminating_error_queue_is_bounded_and_blocks_writes(self):
        class BrokenQueue(RangeSensitiveInstrument):
            def ask(self, command):
                if command==':SYST:ERR?':
                    self.calls.append(('ask',command))
                    return '822,"Too small for sense range"'
                return super().ask(command)
        instrument = BrokenQueue()
        with self.assertRaisesRegex(Keithley2400Error,'within 16 queries'):
            adapter_for(instrument).configure(config())
        self.assertEqual(instrument.calls.count(('ask',':SYST:ERR?')),16)
        self.assertTrue(all(c[0]=='ask' for c in instrument.calls))

    def test_initial_instrument_error_is_retained_without_configuration_writes(self):
        instrument = RangeSensitiveInstrument()
        instrument.errors = ['-113,"Undefined header"']
        adapter = adapter_for(instrument)
        with self.assertRaisesRegex(Keithley2400Error,'initial instrument errors'):
            adapter.configure(config())
        self.assertTrue(all(c[0]=='ask' for c in instrument.calls))
        self.assertTrue(any('-113' in str(e.get('readback','')) for e in adapter.configuration_audit))


    def test_autorange_must_be_restored_after_temporary_range_preparation(self):
        class LostAuto(RangeSensitiveInstrument):
            def write(self, command):
                super().write(command)
                if command.startswith(':SENS:CURR:RANG '):
                    self.ignore_auto = True
        instrument = LostAuto()
        with self.assertRaisesRegex(Keithley2400Error,'autorange was not confirmed'):
            adapter_for(instrument).configure(replace(config(),max_abs_current_a=1e-7))
        self.assertEqual(instrument.calls.count(('applied_compliance',1e-7)),1)
        self.assertNotIn(('output','on'),instrument.calls)

    def test_100na_readback_silently_clamped_to_1ua_is_still_blocking(self):
        class Clamped(RangeSensitiveInstrument):
            def set_compliance(self, value):
                super().set_compliance(value)
                self.compliance=1e-6
        instrument=Clamped()
        with self.assertRaisesRegex(Keithley2400Error,'compliance readback'):
            adapter_for(instrument).configure(replace(config(),max_abs_current_a=1e-7))
        self.assertNotIn(('output','on'),instrument.calls)

    def test_failed_standalone_configuration_audit_survives_cleanup(self):
        from attodry_control.three_smu import ThreeSmuSession
        from tests.test_three_smu import hardware, fixed_plan
        instrument=RangeSensitiveInstrument()
        instrument.reject_compliance=2
        adapter=adapter_for(instrument)
        hw=hardware()
        hw=replace(hw,smu_bias=replace(hw.smu_bias,max_abs_current_a=1e-7))
        with tempfile.TemporaryDirectory() as directory:
            with ThreeSmuSession.open(hw,fixed_plan(),authorize_writes=True,
                    authorize_status_consumption=True,adapter_factory=lambda *_:adapter,
                    sleep=lambda _:None) as session:
                with self.assertRaisesRegex(Keithley2400Error,'compliance_attempt_2'):
                    list(session.run(output_dir=directory))
                events=[json.loads(line) for line in (session.last_run_dir/'raw.jsonl').read_text(encoding='utf-8').splitlines()]
                failed=next(e for e in events if e['event']=='configure_failed')
                self.assertTrue(any('822' in str(a.get('readback','')) for a in failed['payload']['configuration_audit']))
                metadata=json.loads((session.last_run_dir/'metadata.json').read_text(encoding='utf-8'))
                self.assertFalse(metadata['cleanup']['manual_verification_required'])
                self.assertNotIn(('output','on'),instrument.calls)


class CombinationComplianceAuditTests(unittest.TestCase):
    def fixture(self):
        from tests.test_combination_hardware import ElectricalCombinationTests
        fixture=ElectricalCombinationTests('test_both_loop_orders_fresh_reads_harmonics_regroup_and_cleanup')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_combination_records_successful_configuration_steps(self):
        fixture=self.fixture()
        instrument=RangeSensitiveInstrument()
        instrument.responses[':READ?']='0.0,0.00000005'
        adapter=adapter_for(instrument)
        fixture.factory=lambda *_:adapter
        result=fixture.execute('compliance_success')
        self.assertEqual(result['status'],'completed',result)
        event=next(p for kind,p in fixture.events() if kind=='configure')
        self.assertTrue(event['configuration_readback']['configuration_audit'])
        self.assertTrue(instrument.auto)
        self.assertEqual(instrument.values['output'],'off')

    def test_combination_failed_configuration_retains_822_and_confirms_output_off(self):
        fixture=self.fixture()
        instrument=RangeSensitiveInstrument()
        instrument.reject_compliance=2
        adapter=adapter_for(instrument)
        fixture.factory=lambda *_:adapter
        result=fixture.execute('compliance_failed')
        self.assertEqual(result['status'],'failed')
        self.assertIn('compliance_attempt_2',result['error'])
        event=next(p for kind,p in fixture.events() if kind=='configure_failed')
        self.assertTrue(any('822' in str(a.get('readback','')) for a in event['configuration_audit']))
        smu=next(a for a in result['cleanup']['actions'] if a['module']=='smu')
        self.assertFalse(smu['result']['manual_verification_required'])
        self.assertTrue(smu['result']['actions'][0]['output_off_confirmed'])
        self.assertNotIn(('output','on'),instrument.calls)
        self.assertFalse(any(kind=='raw_reading' for kind,_ in fixture.events()))



if __name__ == '__main__':
    unittest.main()
