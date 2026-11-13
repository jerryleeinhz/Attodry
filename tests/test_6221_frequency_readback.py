"""Frequency readback precision stays distinct from source accuracy/limits."""
from copy import deepcopy
from dataclasses import asdict
import unittest

from attodry_control.excitation_6221_config import parse_excitation_6221
from attodry_control.keithley6221_ac import Keithley6221Ac, Keithley6221Error, validate_active_state
from tests.test_keithley6221_ac import Fake6221, source_config


class DecimalFrequency6221(Fake6221):
    def query(self, command):
        value=super().query(command)
        return format(float(value), '.6e') if command=='SOUR:WAVE:FREQ?' else value


def log_config():
    table=asdict(source_config(current_rms_points_a=[10e-9],compliance_voltage_v=5.0))
    del table['frequency_points_hz']
    table['frequency_ranges_hz']=[{'min':17,'max':7000,'scale':'log','points':31}]
    return parse_excitation_6221(table)


class FrequencyReadbackTests(unittest.TestCase):
    def driver(self, resource, config):
        driver=Keithley6221Ac(resource, config, authorize_writes=True,
            sleep=lambda _:self.fail('Readback rounding must not add waiting'))
        driver.preflight();driver.configure()
        return driver

    def test_recorded_second_log_point_is_accepted_live_and_in_archive(self):
        config=log_config();frequency=config.frequency_points_hz[1]
        resource=DecimalFrequency6221();driver=self.driver(resource,config)
        state=driver.set_point(10e-9,frequency)
        self.assertEqual(state['frequency_hz'],20.77801)
        self.assertGreater(abs(state['frequency_hz']-frequency),frequency*1e-7)
        self.assertTrue(validate_active_state(state,config,10e-9,frequency,require_quantization=True))
        counts=(len(resource.queries),len(resource.commands))
        self.assertEqual(counts,(135,18))

    def test_all_31_log_points_keep_commands_queries_and_requested_grid(self):
        config=log_config();traces=[]
        for factory in (Fake6221,DecimalFrequency6221):
            resource=factory();driver=self.driver(resource,config)
            for frequency in config.frequency_points_hz:
                state=driver.set_point(10e-9,frequency)
                self.assertEqual(driver.current_point,(10e-9,frequency))
                expected=float(format(frequency,'.6e')) if factory is DecimalFrequency6221 else frequency
                self.assertEqual(state['actual']['excitation_frequency_hz'],expected)
                self.assertTrue(validate_active_state(state,config,10e-9,frequency,require_quantization=True))
            self.assertTrue(driver.cleanup()['verified'])
            traces.append((resource.queries,resource.commands))
        self.assertEqual(traces[0],traces[1])

    def test_wrong_frequency_nonfinite_or_unknown_values_still_fail(self):
        config=log_config();frequency=config.frequency_points_hz[1]
        resource=DecimalFrequency6221();driver=self.driver(resource,config)
        state=driver.set_point(10e-9,frequency)
        for value in (17.0,20.77800,20.77810,207.7801,float('nan'),float('inf'),True):
            bad=deepcopy(state);bad['frequency_hz']=value
            bad['actual']['excitation_frequency_hz']=value
            with self.subTest(value=value),self.assertRaises(ValueError):
                validate_active_state(bad,config,10e-9,frequency,require_quantization=True)
        for value in ('17.0','20.77800','207.7801'):
            resource.state['SOUR:WAVE:FREQ']=value
            with self.subTest(value=value),self.assertRaises(Keithley6221Error):
                driver.readback()
        resource.fail_query='SOUR:WAVE:FREQ?'
        with self.assertRaises((OSError,Keithley6221Error)):
            driver.readback()

    def test_frequency_endpoints_and_decimal_carry(self):
        frequencies=[.001,9.9999996,99.999996,999.99996,99999.996,100000.]
        config=source_config(current_rms_points_a=[10e-9],frequency_points_hz=frequencies)
        driver=self.driver(DecimalFrequency6221(),config)
        for requested in frequencies:
            state=driver.set_point(10e-9,requested)
            self.assertEqual(state['frequency_hz'],float(format(requested,'.6e')))
            self.assertTrue(validate_active_state(state,config,10e-9,requested,require_quantization=True))


if __name__=='__main__':
    unittest.main()
