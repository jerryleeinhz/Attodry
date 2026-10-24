"""Guarded wavelength skipping, with owned fakes and original raw failure audit."""
from contextlib import closing
from copy import deepcopy
import json
import tomllib
import unittest
from unittest.mock import patch
from tests import test_internal_optical_combination as fixtures
from tests.test_three_smu_config import OPERATION_TEXT
from attodry_control.combination_hardware import HardwareCombinationStation,load_hardware_combination
from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_store import CombinationStore,open_readonly,run_snapshot
from attodry_control.optical_recovery import OpticalTargetUnreachable
from attodry_control.optical_points import OpticalPointSession
from attodry_control.pm100d import Pm100d,SimulatedPmResource
from attodry_control.nkt_config import NktError


class OpticalUnreachableSkipTests(unittest.TestCase):
    setUp=fixtures.InternalOpticalCombinationTests.setUp
    write_config=fixtures.InternalOpticalCombinationTests.write_config
    write_document=fixtures.InternalOpticalCombinationTests.write_document
    factory=fixtures.InternalOpticalCombinationTests.factory
    events=fixtures.InternalOpticalCombinationTests.events
    backend_factory=fixtures.InternalOpticalCombinationTests.backend_factory
    chopper_factory=fixtures.InternalOpticalCombinationTests.chopper_factory
    enable_chopper=fixtures.InternalOpticalCombinationTests.enable_chopper
    execute=fixtures.InternalOpticalCombinationTests.execute
    assert_closed_laser_off=fixtures.InternalOpticalCombinationTests.assert_closed_laser_off

    def configure_skip(self,*,policy="skip_wavelength",gates=False,repeats=1):
        self.document["combination_scan"].update(unreachable_target_policy=policy,repeats=repeats)
        self.document["power_feedback"]["target_powers_w"]=[5e-6,5e-6]
        self.document["nkt_run"]["points"]=[
            dict(source_level_pct=8.0,wavelength_nm=w,bandwidth_nm=10.0) for w in (500.0,630.0)]
        if gates:
            smu=tomllib.loads(OPERATION_TEXT)
            smu.pop("smu_bias");smu.pop("gate_bottom")
            smu["three_smu_run"].update(delay_s=.03,serpentine=False)
            smu["three_smu_run"]["smu_bias"]=dict(role="off",bidirectional=False)
            smu["three_smu_run"]["gate_bottom"]=dict(role="off",bidirectional=False)
            smu["three_smu_run"]["gate_top"]=dict(role="sweep",bidirectional=False,
                points=[0,.1,-.1],zero_readback_tolerance_v=.05)
            self.document.update(smu)
            self.document["combination_scan"]["order"]=["optical","smu","lockin"]
        self.write_document()

    def meter_factory(self,config):
        def power():
            if self.backend.source["emission_state"]!=3:return 0
            if getattr(self,"forced_power",None) is not None:return self.forced_power
            level=self.backend.source["level_pct"]
            low=self.backend.filter["lower_edge_nm"]<600 or getattr(self,"all_low",False)
            return 4e-6*level/40 if low else .0001*(level/40)**2
        self.meter=Pm100d(config,SimulatedPmResource(power),is_hardware=True,
            authorize_settings=True,authorize_measurement=True,clock=self.clock)
        return self.meter

    def test_default_abort_keeps_boundary_failure_and_never_visits_next_wave(self):
        self.configure_skip(policy="abort")
        result=self.execute()
        self.assertEqual(result["status"],"failed")
        self.assertIn("Target unreachable",result["error"])
        self.assertEqual(result["skipped_conditions"],0)
        self.assertEqual(load_combination_rows(self.database),())
        self.assertFalse(any(k=="wavelength_skipped" for k,_ in self.events()))
        self.assert_closed_laser_off()

    def test_low_wave_skips_and_next_wave_measures_with_verified_normal_cleanup(self):
        self.configure_skip()
        result=self.execute()
        self.assertEqual(result["status"],"completed",result)
        self.assertTrue(result["cleanup"]["clean"],result)
        self.assertEqual(result["skipped_conditions"],1)
        rows=load_combination_rows(self.database)
        self.assertEqual(len(rows),2)
        self.assertEqual({x["requested.optical_wavelength_nm"] for x in rows},{630.0})
        state=run_snapshot(self.database,"internal")
        self.assertEqual((state["accepted_conditions"],state["skipped_conditions"],state["total_conditions"]),(1,1,2))
        with closing(open_readonly(self.database)) as c:
            rejected=c.execute("SELECT error FROM combination_attempts WHERE status='rejected'").fetchone()[0]
            self.assertIn("OpticalTargetUnreachable",rejected)
        skipped=next(p for k,p in self.events() if k=="wavelength_skipped")
        self.assertTrue(skipped["laser_off_confirmed"])
        self.assertEqual(skipped["target_evidence"]["source_current_pct"],40)
        self.assertIn("skipped",result["completion_message"])
        self.assert_closed_laser_off()

    def test_remaining_gate_leaves_skip_and_next_wavelength_resets_gate_and_background(self):
        self.configure_skip(gates=True)
        result=self.execute()
        self.assertEqual(result["status"],"completed",result)
        self.assertTrue(result["cleanup"]["clean"],result)
        self.assertEqual(result["skipped_conditions"],3)
        rows=load_combination_rows(self.database)
        self.assertEqual(len(rows),6)
        self.assertEqual({x["requested.gate_top_v"] for x in rows},{0,.1,-.1})
        self.assertEqual({x["requested.optical_wavelength_nm"] for x in rows},{630.0})
        self.assertFalse(self.adapters["gate_top"].output)
        self.assertEqual(self.adapters["gate_top"].source,0)
        self.assertEqual(len([p for k,p in self.events() if k=="wavelength_skipped"]),1)
        self.assert_closed_laser_off()

    def test_repeat_rechecks_previously_skipped_wave(self):
        self.configure_skip(repeats=2)
        result=self.execute()
        self.assertEqual(result["status"],"completed",result)
        self.assertEqual(result["skipped_conditions"],2)
        self.assertEqual(len([p for k,p in self.events() if k=="wavelength_skipped"]),2)

    def test_generic_nkt_fault_is_never_skipped(self):
        self.configure_skip()
        with patch.object(OpticalPointSession,"qualify",side_effect=NktError("unknown source state")):
            result=self.execute()
        self.assertEqual(result["status"],"failed")
        self.assertEqual(result["skipped_conditions"],0)

    def test_fresh_guard_failure_stops_and_retains_boundary_evidence(self):
        self.configure_skip()
        original=HardwareCombinationStation.skip_unreachable_wavelength
        def fail(station,error):
            self.assertIsInstance(error,OpticalTargetUnreachable)
            raise OSError("fresh communication failed")
        with patch.object(HardwareCombinationStation,"skip_unreachable_wavelength",fail):
            result=self.execute()
        self.assertEqual(result["status"],"failed")
        self.assertIn("fresh communication",result["error"])
        self.assertEqual(result["skipped_conditions"],0)
        audit=next(p for k,p in self.events() if k=="optical_audit")
        self.assertTrue(any(p.get("power_samples") for p in audit["points"]))

    def test_ineligible_typed_error_is_never_a_whitelist(self):
        self.configure_skip()
        with patch.object(OpticalPointSession,"qualify",side_effect=OpticalTargetUnreachable("fabricated bound")):
            result=self.execute()
        self.assertEqual(result["status"],"failed")
        self.assertEqual(result["skipped_conditions"],0)

    def test_loader_rejects_ambiguous_wavelengths_order_and_connected_excitation(self):
        self.configure_skip()
        original=deepcopy(self.document)
        for mutation in (
            lambda d:d["nkt_run"]["points"].__setitem__(1,deepcopy(d["nkt_run"]["points"][0])),
            lambda d:d["combination_scan"].update(order=["lockin","optical"]),
            lambda d:d["lockin_xx"].update(sine_output_connected=True),
            lambda d:d["combination_scan"].update(illumination_policy="continuous_gate_scan"),
            lambda d:d["combination_scan"].update(unreachable_target_policy="ignore_errors"),
        ):
            self.document=deepcopy(original);mutation(self.document);self.write_document()
            with self.assertRaises(ValueError):load_hardware_combination(self.path)
        self.assertEqual(self.manager.opened,[])

    def test_store_never_completes_an_unmeasured_unskipped_condition(self):
        self.configure_skip()
        config=load_hardware_combination(self.path)
        with CombinationStore(self.database) as store:
            store.begin_run("pending",config.snapshot,config.plan.conditions())
            with self.assertRaisesRegex(ValueError,"pending"):
                store.finish_run("pending","completed",{"clean":True},None)

    def test_all_waves_skipped_is_explicit_and_normal_gate_zero_is_measured(self):
        self.configure_skip(gates=True)
        self.all_low=True
        result=self.execute()
        self.assertEqual(result["status"],"completed",result)
        self.assertTrue(result["cleanup"]["clean"],result)
        self.assertEqual(result["skipped_conditions"],6)
        state=run_snapshot(self.database,"internal")
        self.assertEqual(state["accepted_conditions"],0)
        self.assertEqual(state["skipped_conditions"],6)
        self.assertEqual(load_combination_rows(self.database),())
        action=next(x for x in result["cleanup"]["actions"] if x["module"]=="smu")
        self.assertTrue(action["result"]["actions"][0]["zero_readback_recorded"])

    def test_pm_hard_limit_or_missing_light_never_skips(self):
        self.configure_skip()
        self.forced_power=1e-4
        result=self.execute("hard-power")
        self.assertEqual(result["status"],"failed")
        self.assertEqual(result["skipped_conditions"],0)

    def test_low_signal_is_a_fault_not_an_unreachable_target(self):
        self.configure_skip()
        self.forced_power=1e-8
        result=self.execute("low-signal")
        self.assertEqual(result["status"],"failed")
        self.assertEqual(result["skipped_conditions"],0)

    def test_dark_guard_io_failure_prevents_skip(self):
        self.configure_skip()
        original=OpticalPointSession.observe_dark
        def dark(session,*,phase="diagnostic_dark",deadline=None):
            if phase=="skip_unreachable_wavelength":raise OSError("fresh OFF guard failed")
            return original(session,phase=phase,deadline=deadline)
        with patch.object(OpticalPointSession,"observe_dark",dark):result=self.execute()
        self.assertEqual(result["status"],"failed")
        self.assertIn("fresh OFF",result["error"])
        self.assertEqual(result["skipped_conditions"],0)

    def test_boundary_exception_in_formal_window_is_never_skipped(self):
        self.configure_skip()
        with patch.object(OpticalPointSession,"begin_sample",side_effect=OpticalTargetUnreachable("late fault")):
            result=self.execute()
        self.assertEqual(result["status"],"failed")
        self.assertIn("late fault",result["error"])
        self.assertEqual(result["skipped_conditions"],1)

    def test_three_receivers_gate_dark_matching_after_skip(self):
        from tests import test_three_lockin as three
        three.ThreeOpticalTests.setUp(self)
        self.configure_skip(gates=True)
        result=self.execute()
        self.assertEqual(result["status"],"completed",result)
        self.assertTrue(result["cleanup"]["clean"],result)
        self.assertEqual(result["skipped_conditions"],3)
        rows=load_combination_rows(self.database)
        self.assertEqual(len(rows),6)
        for row in rows:
            self.assertIn("measured.lockin_aux_h1_delta_x_v",row)
        self.assertTrue(self.manager.closed and all(x.closed for x in self.manager.resources.values()))

    def test_reference_unlock_during_skip_guard_stops(self):
        self.configure_skip()
        original=HardwareCombinationStation.skip_unreachable_wavelength
        def unlock(station,error):
            self.xy.responses["LIAS?"]="8"
            return original(station,error)
        with patch.object(HardwareCombinationStation,"skip_unreachable_wavelength",unlock):
            result=self.execute()
        self.assertEqual(result["status"],"failed")
        self.assertEqual(result["skipped_conditions"],0)

    def test_skip_audit_failure_prevents_next_wavelength_emission(self):
        self.configure_skip()
        original=CombinationStore.skip_condition
        def fail(store,*args,**kwargs):raise OSError("skip audit disk failure")
        with patch.object(CombinationStore,"skip_condition",fail):result=self.execute()
        self.assertEqual(result["status"],"failed")
        self.assertIn("skip audit disk",result["error"])
        self.assertEqual(result["skipped_conditions"],0)
        prepared=[p for k,p in self.events() if k=="optical_point_prepared"]
        self.assertFalse(any(p["index"]==1 for p in prepared))

    def test_skip_guard_cannot_outlive_original_point_deadline(self):
        self.configure_skip()
        original=HardwareCombinationStation.skip_unreachable_wavelength
        def expired(station,error):
            self.clock.sleep(10)
            return original(station,error)
        with patch.object(HardwareCombinationStation,"skip_unreachable_wavelength",expired):
            result=self.execute()
        self.assertEqual(result["status"],"failed")
        self.assertIn("timeout",result["error"].lower())
        self.assertEqual(result["skipped_conditions"],0)
