"""Accepted formal evidence is independent of current raw/environment readbacks."""
from pathlib import Path
import tempfile
import unittest

from attodry_control.combination_scan import AxisPoint, CombinationPlan, ScanAxis
from attodry_control.combination_store import CombinationStore, _last_formal_measurement


class MonitorFormalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = CombinationStore(Path(temporary.name)/"formal.sqlite")
        self.addCleanup(self.store.connection.close)
        self.plan = CombinationPlan((ScanAxis("temperature", tuple(AxisPoint({"temperature_k":t})
            for t in (1.7,4.2))),ScanAxis("lockin",(AxisPoint({
                "lockin_excitation_v_rms":.004,"lockin_frequency_hz":8.}),))),samples_per_condition=3)
        self.conditions = self.plan.conditions()
        for run in ("fixed","other"):
            self.store.begin_run(run,self.plan.snapshot(),self.conditions)

    def capture(self,run,condition,*,accepted=True,offset=0.):
        cid=self.conditions[condition]["condition_id"]
        attempt=self.store.begin_attempt(run,cid)
        for i in range(3):
            reading={"module":"lockin","captured_at_utc":f"2026-10-20T10:00:0{i}+00:00",
                     "actual":{"lockin_frequency_hz":8.},"measurements":{"xx_v":offset+i},
                     "clean":True,"status":{"raw":["never transfer this"],"smu_audit":[1,2,3]}}
            smu={"module":"smu","captured_at_utc":f"2026-10-20T09:59:5{i}+00:00",
                     "actual":{"gate_bottom_v":.025},"measurements":{},"clean":True,
                     "status":{"power_target_w":25e-6,"device_transcripts":["private"],
                               "power_target_assessment":{"target_in_tolerance":True,"target_deviation_w":0.}}}
            self.store.sample(run,cid,attempt,i,{"started_at_utc":reading["captured_at_utc"],
                "finished_at_utc":reading["captured_at_utc"],"reads":[smu,reading],"clean":True})
        if accepted:
            self.store.finish_attempt(run,cid,attempt,expected_samples=3)
        return cid,attempt

    def formal(self):
        return _last_formal_measurement(self.store.connection,"fixed",self.plan.snapshot())

    def test_missing_accepted_formal_does_not_promote_started_or_raw(self):
        self.capture("fixed",0,accepted=False)
        self.store.event("fixed","raw_reading",{"condition_id":"condition-000000", "reading":{
            "module":"lockin","measurements":{"xx_v":999.}}})
        self.assertIsNone(self.formal())

    def test_last_accepted_formal_excludes_newer_started_rejected_and_other_run(self):
        cid,attempt=self.capture("fixed",0)
        self.capture("fixed",1,accepted=False,offset=100)
        self.capture("other",0,offset=200)
        result=self.formal()
        self.assertEqual((result["condition_id"],result["attempt_index"],result["sample_index"]),(cid,attempt,2))
        self.assertEqual(result["attempt_status"],"accepted")
        self.assertEqual(result["expected_samples"],3)
        self.assertEqual(result["modules"]["lockin"]["measurements"]["xx_v"],2)
        self.assertEqual(result["context"]["axes"]["temperature"]["index"],0)
        self.store.finish_attempt("fixed","condition-000001",0,expected_samples=3,error="retained rejection")
        self.assertEqual(self.formal()["condition_id"],cid)

    def test_projection_retains_per_module_time_and_target_without_transcripts(self):
        self.capture("fixed",0)
        result=self.formal()
        self.assertNotEqual(result["modules"]["lockin"]["captured_at_utc"],result["modules"]["smu"]["captured_at_utc"])
        self.assertEqual(result["modules"]["smu"]["actual"]["gate_bottom_v"],.025)
        self.assertNotIn("raw",str(result))
        self.assertNotIn("device_transcripts",str(result))
        self.assertNotIn("smu_audit",str(result))

    def test_next_accepted_condition_replaces_previous_formal(self):
        self.capture("fixed",0)
        self.capture("fixed",1,offset=300)
        self.assertEqual(self.formal()["condition_id"],"condition-000001")
        self.assertEqual(self.formal()["modules"]["lockin"]["measurements"]["xx_v"],302)


if __name__=="__main__":
    unittest.main()
