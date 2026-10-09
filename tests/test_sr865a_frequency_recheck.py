"""Hardware-free bounded external frequency rechecks and retained evidence."""
from dataclasses import asdict
from types import SimpleNamespace
import json
import unittest

from attodry_control.models import LockinRole
from attodry_control.sr865a import Sr865a, Sr865aError
from attodry_control.electrical_lockin_backend import Sr865aReceiver, ElectricalLockinError
from tests.test_sr865a import FakeResource


class ExternalFrequencyRecheckTests(unittest.TestCase):
    def setUp(self):
        self.resource=FakeResource()
        self.driver=Sr865a(self.resource, LockinRole.XY)
        self.sleeps=[]
        self.driver._sleep=self.sleeps.append

    def sample(self, **flags):
        return self.driver.read_sample(**({'consume_status_latches':True,
            'current_status_supported':True} | flags))

    def mismatches(self, detections, references=None):
        self.resource.responses.update({'HARM?':'2', 'FREQEXT?':references or '71.124229431',
            'FREQDET?':detections})

    def test_reported_mismatch_recovers_with_fresh_pair_and_full_audit(self):
        self.mismatches(['142.2336731','142.24'], ['71.124229431','71.12'])
        sample=self.sample()
        self.assertTrue(sample.status.valid)
        self.assertEqual(self.sleeps,[1.0])
        self.assertEqual(sample.reference_frequency_hz,71.12)
        self.assertEqual(sample.detection_frequency_hz,142.24)
        self.assertEqual([c.frequency_consistent for c in sample.frequency_checks],[False,True])
        self.assertEqual(sample.frequency_checks[0].reference_frequency_hz,71.124229431)
        self.assertTrue(sample.frequency_checks[0].status.valid)
        self.assertEqual(sample.frequency_checks[0].retry_wait_s,1.0)
        self.assertEqual(self.resource.events.count(('query','SNAP? X,Y')),1)
        frequencies=[r for r in sample.raw if r.command in ('FREQEXT?','FREQDET?')]
        self.assertEqual([r.response for r in frequencies],['71.124229431','142.2336731','71.12','142.24'])
        self.assertTrue(all(r.started_at_utc<=r.completed_at_utc for r in frequencies))
        self.assertEqual(self.resource.writes,[])

    def test_third_pair_can_recover_without_widening_h1_h2_h3_thresholds(self):
        for harmonic in (1,2,3):
            for reference,delta in ((17.,.006),(5000.,.6)):
                with self.subTest(harmonic=harmonic, reference=reference):
                    self.setUp()
                    self.resource.responses.update({'HARM?':str(harmonic),'FREQEXT?':str(reference),
                        'FREQDET?':[str(harmonic*(reference+delta)),str(harmonic*(reference-delta)),str(harmonic*reference)]})
                    sample=self.sample()
                    self.assertEqual(self.sleeps,[1.,1.])
                    self.assertEqual([c.frequency_consistent for c in sample.frequency_checks],[False,False,True])
                    self.assertEqual(sample.frequency_checks[-1].reference_tolerance_hz,max(.005,reference*100e-6))
                    self.assertEqual(self.resource.events.count(('query','SNAP? X,Y')),1)
                    self.assertEqual(self.resource.writes,[])

    def test_persistent_mismatch_stops_after_three_pairs_and_revokes_identity(self):
        self.mismatches('142.2336731')
        with self.assertRaisesRegex(Sr865aError,'3/3') as error:
            self.sample()
        self.assertEqual(self.sleeps,[1.,1.])
        self.assertEqual(len(error.exception.frequency_checks),3)
        self.assertEqual(self.resource.events.count(('query','FREQDET?')),3)
        self.assertNotIn(('query','SNAP? X,Y'),self.resource.events)
        with self.assertRaisesRegex(Sr865aError,'identity'):
            self.driver.set_harmonic(1,authorized=True)
        self.assertEqual(self.resource.writes,[])

    def test_status_fault_on_mismatch_never_retries_or_launders_latches(self):
        for cmd,value in [('CUROVLDSTAT?','8'),('LIAS?','8'),('CUROVLDSTAT?','16'),
                          ('LIAS?','16'),('LIAS?','32'),('CUROVLDSTAT?','256'),
                          ('CUROVLDSTAT?','4'),('ERRS?','4'),('ERRS?','1'),
                          ('*ESR?','32'),('*ESR?','64'),('*ESR?','128')]:
            with self.subTest(cmd=cmd,value=value):
                self.setUp();self.mismatches(['142.2336731','142.248458862'])
                self.resource.responses[cmd]=[value,'0']
                with self.assertRaisesRegex(Sr865aError,'status') as error:self.sample()
                self.assertFalse(error.exception.frequency_checks[-1].status.valid)
                self.assertEqual(self.sleeps,[])
                self.assertEqual(self.resource.events.count(('query','FREQDET?')),1)
                self.assertNotIn(('query','SNAP? X,Y'),self.resource.events)

    def test_status_fault_after_wait_does_not_become_valid_when_frequency_recovers(self):
        for cmd,value in [('CUROVLDSTAT?','8'),('LIAS?','8'),('ERRS?','1'),('*ESR?','64')]:
            with self.subTest(cmd=cmd):
                self.setUp();self.mismatches(['142.2336731','142.248458862'])
                self.resource.responses[cmd]=['0',value]
                sample=self.sample()
                self.assertFalse(sample.status.valid)
                self.assertEqual(self.sleeps,[1.])
                self.assertEqual(self.resource.events.count(('query','FREQDET?')),2)

    def test_no_recheck_without_complete_authorized_status_evidence(self):
        for consume,current in ((False,False),(False,True),(True,False)):
            with self.subTest(consume=consume,current=current):
                self.setUp();self.mismatches('142.2336731')
                with self.assertRaises(Sr865aError):
                    self.sample(consume_status_latches=consume,current_status_supported=current)
                self.assertEqual(self.sleeps,[])
                self.assertNotIn(('query','LIAS?'),self.resource.events)
                self.assertNotIn(('query','CUROVLDSTAT?'),self.resource.events)

    def test_changed_harmonic_reference_mode_or_input_aborts_recheck(self):
        for cmd,values in [('HARM?',['2','1']),('RSRC?',['1','0']),('IVMD?',['0','1'])]:
            with self.subTest(cmd=cmd):
                self.setUp();self.mismatches('142.2336731');self.resource.responses[cmd]=values
                with self.assertRaises(Sr865aError) as error:self.sample()
                self.assertEqual(len(error.exception.frequency_checks),1)
                self.assertEqual(self.sleeps,[1.])
                self.assertNotIn(('query','SNAP? X,Y'),self.resource.events)

    def test_invalid_frequency_or_communication_failure_is_not_retried(self):
        for value in ('nan','oops','0','4000000',OSError('fake timeout')):
            with self.subTest(value=value):
                self.setUp();self.mismatches(value)
                with self.assertRaises(Sr865aError):self.sample()
                self.assertEqual(self.sleeps,[])
                self.assertEqual(self.resource.events.count(('query','FREQDET?')),1)
                self.assertNotIn(('query','SNAP? X,Y'),self.resource.events)

    def test_failure_during_recheck_retains_first_pair_and_does_not_try_again(self):
        self.mismatches(['142.2336731',OSError('fake timeout')])
        # FakeResource normally raises a stored exception, but list entries need
        # the same transport behavior when fault injection is sequential.
        original=self.resource.query
        def query(command):
            value=original(command)
            if isinstance(value,Exception):raise value
            return value
        self.resource.query=query
        with self.assertRaises(Sr865aError) as error:self.sample()
        self.assertEqual(len(error.exception.frequency_checks),1)
        self.assertEqual(self.sleeps,[1.])
        self.assertIsNotNone(self.driver.audit[-1].error)

    def test_successful_first_read_and_internal_mode_have_no_added_delay(self):
        sample=self.sample()
        self.assertEqual(len(sample.frequency_checks),1)
        self.assertTrue(sample.frequency_checks[0].frequency_consistent)
        self.assertEqual(self.sleeps,[])
        self.assertEqual(self.resource.events.count(('query','FREQDET?')),1)
        self.setUp();self.resource.responses.update({'RSRC?':'0','FREQINT?':'71.124229431','FREQDET?':'142.2336731'})
        with self.assertRaisesRegex(Sr865aError,'Detection frequency'):self.sample()
        self.assertEqual(self.sleeps,[])
        self.assertEqual(self.resource.events.count(('query','FREQDET?')),1)

    def test_receiver_serializes_recovered_history_and_terminal_failure(self):
        adapter=Sr865aReceiver(self.resource,config=SimpleNamespace(current_status_supported=True))
        adapter._driver._sleep=self.sleeps.append
        self.mismatches(['142.2336731','142.248458862'])
        sample=adapter.read_harmonic_sample(2)
        serialized=asdict(sample)
        self.assertEqual(len(serialized['native_sample']['frequency_checks']),2)
        json.dumps(serialized,default=str)
        self.mismatches('142.2336731')
        with self.assertRaises(ElectricalLockinError) as error:adapter.read_harmonic_sample(2)
        self.assertEqual(len(error.exception.evidence['frequency_checks']),3)
        self.assertFalse(error.exception.evidence['frequency_checks'][0]['frequency_consistent'])
        json.dumps(error.exception.evidence)
        self.assertTrue(error.exception.raw)
        self.assertEqual(self.resource.writes,[])

    def test_failure_after_recovery_retains_history_and_does_not_repeat_xy_read(self):
        for command in ("SNAP? X,Y",):
            self.setUp()
            self.mismatches(['142.2336731', '142.248458862'])
            self.resource.responses[command] = OSError("fake read failure")
            with self.assertRaises(Sr865aError) as error:
                self.sample()
            self.assertEqual(len(error.exception.frequency_checks), 2)
            self.assertEqual(self.resource.events.count(('query', command)), 1)
            self.assertEqual(self.sleeps, [1.])
