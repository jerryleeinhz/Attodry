"""OFF state configuration and query-only ZERO monitoring never enable a source."""
from dataclasses import replace
import unittest

from attodry_control.keithley2400 import QcodesKeithley2400, VisaKeithley2400Monitor, Keithley2400Error
from attodry_control.three_smu_config import OffState
from tests.test_keithley2400 import FakeQcodesInstrument, FakeVisaResource, config


def resource():
    instance = FakeVisaResource()
    instance.responses.update({':SOUR:VOLT?': '0', ':OUTP:SMOD?': 'ZERO',
        ':SENS:FUNC:CONC?':'1', ':SENS:FUNC?':'"VOLT:DC","CURR:DC"',
        ':FORM:ELEM?':'VOLT,CURR', ':SENS:DATA?':'0.001,0.000000002'})
    return instance


class OffStateDriverTests(unittest.TestCase):
    def test_all_explicit_states_are_written_and_verified_while_off(self):
        for state, command, response in ((OffState.NORMAL,'NORM','NORMAL'),
                (OffState.ZERO,'ZERO','ZERO'), (OffState.HIGH_IMPEDANCE,'HIMP','HIMPEDANCE'),
                (OffState.GUARD,'GUAR','GUARD')):
            with self.subTest(state=state):
                instrument = FakeQcodesInstrument()
                instrument.responses[':OUTP:SMOD?'] = response
                adapter = QcodesKeithley2400('smu_bias',instrument)
                adapter.authorize_status_consumption()
                actual = adapter.configure(replace(config(),off_state=state))
                self.assertEqual(actual.off_state,state.value)
                self.assertIn(('write',':OUTP:SMOD '+command),instrument.calls)
                self.assertEqual(adapter.preflight().off_state,state.value)
                self.assertNotIn(('output','on'),instrument.calls)

    def test_preserve_keeps_existing_query_and_write_behavior(self):
        instrument = FakeQcodesInstrument()
        adapter = QcodesKeithley2400('smu_bias',instrument)
        adapter.authorize_status_consumption()
        self.assertIsNone(adapter.configure(config()).off_state)
        self.assertIsNone(adapter.preflight().off_state)
        self.assertFalse(any('SMOD' in str(call) for call in instrument.calls))
        res = FakeVisaResource()
        VisaKeithley2400Monitor('smu_bias',res).read_monitor()
        self.assertNotIn(':OUTP:SMOD?',res.queries)
        self.assertNotIn(':SENS:DATA?',res.queries)

    def test_state_mismatch_unknown_and_write_failure_reject_without_enable(self):
        for response in ('HIMP','unrecognized'):
            with self.subTest(response=response):
                instrument = FakeQcodesInstrument()
                instrument.responses[':OUTP:SMOD?'] = response
                adapter = QcodesKeithley2400('smu_bias',instrument)
                adapter.authorize_status_consumption()
                with self.assertRaises(Keithley2400Error):
                    adapter.configure(replace(config(),off_state=OffState.ZERO))
                self.assertNotIn(('output','on'),instrument.calls)
        instrument = FakeQcodesInstrument(); instrument.fail_write=':OUTP:SMOD ZERO'
        adapter = QcodesKeithley2400('smu_bias',instrument); adapter.authorize_status_consumption()
        with self.assertRaises(OSError): adapter.configure(replace(config(),off_state=OffState.ZERO))
        self.assertNotIn(('output','on'),instrument.calls)

    def test_off_zero_latest_values_are_never_labelled_fresh(self):
        res = resource(); monitor = VisaKeithley2400Monitor('smu_bias',res)
        first = monitor.read_monitor(allow_off_zero=True)
        second = monitor.read_monitor(allow_off_zero=True)
        self.assertEqual((first.voltage_v,first.current_a),(0.001,2e-9))
        self.assertEqual(second.voltage_v,first.voltage_v)
        self.assertFalse(first.output_enabled)
        self.assertEqual(first.off_state,'zero')
        self.assertEqual(first.reading_origin,'latest_buffer')
        self.assertEqual(first.measurement_freshness,'unknown')
        self.assertTrue(first.received_at_utc)
        self.assertEqual(first.raw_response,'0.001,0.000000002')
        self.assertNotIn(':READ?',res.queries)
        self.assertFalse(any('INIT' in q or 'MEAS' in q for q in res.queries))
        self.assertNotIn(':SYST:ERR?',res.queries)

    def test_buffer_format_is_parsed_without_writing_instrument_settings(self):
        res=resource(); res.responses[':FORM:ELEM?']='CURR,TIME,VOLT,STAT,RES'
        res.responses[':SENS:DATA?']='2e-9,42.5,0.001,0,9.91e37'
        reading=VisaKeithley2400Monitor('smu_bias',res).read_monitor(allow_off_zero=True)
        self.assertEqual((reading.voltage_v,reading.current_a,reading.device_timestamp_s),(0.001,2e-9,42.5))
        self.assertEqual(reading.measurement_freshness,'unknown')

    def test_other_states_or_output_on_reject_before_any_read_trigger(self):
        for command, response in ((':OUTP?','1'),(':SOUR:FUNC?','CURR'),
                                   (':OUTP:SMOD?','HIMP'),(':OUTP:SMOD?','UNKNOWN')):
            with self.subTest(command=command,response=response):
                res=resource(); res.responses[command]=response
                if command==':SOUR:FUNC?': res.responses[':SOUR:CURR?']='0'
                with self.assertRaises(Keithley2400Error):
                    VisaKeithley2400Monitor('smu_bias',res).read_monitor(allow_off_zero=True)
                self.assertNotIn(':SENS:DATA?',res.queries)
                self.assertNotIn(':READ?',res.queries)

    def test_invalid_vi_or_unverified_measurement_configuration_rejects(self):
        for command,response in ((':SENS:DATA?','9.91e37,0'),(':SENS:DATA?','nan,0'),
                (':SENS:DATA?','0,inf'),(':SENS:DATA?','0'),(':SENS:DATA?','garbage'),
                (':FORM:ELEM?','VOLT,VOLT,CURR'),(':SENS:FUNC:CONC?','0'),
                (':SENS:FUNC?','"CURR:DC"')):
            with self.subTest(command=command,response=response):
                res=resource(); res.responses[command]=response
                with self.assertRaises(Keithley2400Error):
                    VisaKeithley2400Monitor('smu_bias',res).read_monitor(allow_off_zero=True)
                self.assertNotIn(':READ?',res.queries)

    def test_output_or_mode_changed_during_query_rejects_values(self):
        for command,last in ((':OUTP?','1'),(':OUTP:SMOD?','NORM')):
            with self.subTest(command=command):
                res=resource(); original=res.query; counts={}
                def query(request):
                    counts[request]=counts.get(request,0)+1
                    value=original(request)
                    return last if request==command and counts[request]>1 else value
                res.query=query
                with self.assertRaisesRegex(Keithley2400Error,'changed'):
                    VisaKeithley2400Monitor('smu_bias',res).read_monitor(allow_off_zero=True)

    def test_owned_off_monitor_uses_same_transport_and_formal_read_remains_on_only(self):
        instrument=FakeQcodesInstrument()
        instrument.responses.update(resource().responses)
        adapter=QcodesKeithley2400('smu_bias',instrument); adapter.authorize_status_consumption()
        adapter.configure(replace(config(),off_state=OffState.ZERO))
        instrument.calls.clear()
        reading=adapter.read_off_zero_monitor()
        self.assertEqual(reading.reading_origin,'latest_buffer')
        self.assertNotIn(('ask',':READ?'),instrument.calls)
        self.assertFalse(any(call[0]=='write' or call==('output','on') for call in instrument.calls))
        with self.assertRaisesRegex(Keithley2400Error,'output OFF'): adapter.read()

