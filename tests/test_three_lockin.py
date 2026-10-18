"""Three-channel behavior through fake transports; no hardware imports."""
from copy import deepcopy
from dataclasses import replace
import unittest

from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.internal_lockin_group import InternalLockinGroupSession
from attodry_control.lockin_overload import overload_summary, reading_allows_continuation
from attodry_control.optical_background import MatchedLockinBackground, BackgroundConfig
from attodry_control.analysis_observations import channel_quality
from tests import test_combination_hardware as fixtures
from tests.photonics_lockin_helpers import FakeResource, FakeManager
from tests.optical_helpers import Clock

AUX = '''
[lockin_aux]
enabled = true
model = "SR865A"
address = "FAKE::AUX"
channel_label = "Third sample voltage"
reference_source = "external_ttl"
external_reference_edge = "rising"
sine_output_connected = false
input_mode = "a_minus_b"
shield_grounding = "float"
input_coupling = "ac"
time_constant_s = 0.3
filter_slope_db_oct = 24
sensitivity_mode = "fixed"
sensitivity_full_scale_v = 0.001
phase_shift_deg = 0.0
harmonics = [1]
[lockin_aux.sr865a]
input_range_v_peak = 0.1
reference_input_impedance_ohm = 1000000.0
sync_output_mode = "preserve"
current_status_supported = true
'''
POLICY = '''
[lockin_aux]
allowed_fixed_full_scales_v = [0.001, 0.0002]
'''


class ThreeLockinTests(unittest.TestCase):
    setUp = fixtures.ElectricalCombinationTests.setUp
    write_config = fixtures.ElectricalCombinationTests.write_config

    def config(self, extra=AUX, source=None):
        self.write_config(order=("lockin",), source=source or self.base, extra=extra)
        safety = self.directory / "lockin_safety.toml"
        if "[lockin_aux]" not in safety.read_text():
            with safety.open("a") as f:
                f.write(POLICY)
        return load_hardware_combination(self.path)

    def make(self, *, configured=True):
        source = self.base.replace("excitation_xy_harmonics = [1, 2, 3]", "excitation_xy_harmonics = [1]")
        cfg = self.config(source=source)
        # The integration fixture's first series/current safety check is retained.
        self.clock = Clock()
        self.events = []
        document = {r: {"model": "SR865A" if r == "lockin_aux" else "SR830",
                        "address": "FAKE::" + r.removeprefix("lockin_").upper()}
                    for r in ("lockin_xx", "lockin_xy", "lockin_aux")}
        mgr = FakeManager(document)
        mgr.resources["FAKE::AUX"] = FakeResource("SR865A", "lockin_aux")
        for name, resource in mgr.resources.items():
            for key in ("FREQ?", "FREQEXT?", "FREQDET?", "FREQINT?"):
                resource.responses[key] = str(cfg.lockin.lockin_xx.frequency_hz)
            if name == "FAKE::XX":
                resource.responses["FMOD?"] = "1"
                resource.responses["PHAS?"] = "13.5"
            if name == "FAKE::XY":
                resource.responses["PHAS?"] = "-20"
        session = InternalLockinGroupSession(self.path, cfg.lockin,
            lambda kind,payload:self.events.append((kind,deepcopy(payload))),
            manager_factory=lambda:mgr, monotonic=self.clock, sleep=self.clock.sleep)
        self.addCleanup(session.close)
        if configured:
            session.open(); session.configure(); session.set_point(.004); session.qualify()
        return session,mgr,cfg

    def test_config_and_construction_are_pure_and_include_aux(self):
        s,m,cfg = self.make(configured=False)
        self.assertEqual(m.opened, [])
        self.assertEqual(s.harmonics_by_role, {"xx":(1,), "xy":(1,), "aux":(1,)})
        self.assertEqual(cfg.lockin.lockin_aux.channel_label, "Third sample voltage")

    def test_wrong_aux_identity_opens_no_write_and_closes_every_resource(self):
        s,m,_ = self.make(configured=False)
        m.resources["FAKE::AUX"].responses["*IDN?"] = "Stanford_Research_Systems,SR830,aux,v1"
        with self.assertRaises(RuntimeError):
            s.open()
        self.assertTrue(m.closed)


        self.assertTrue(all(r.closed and not r.writes for r in m.resources.values()))

    def test_three_channels_qualified_and_disconnected_receivers_never_write_source(self):
        s,m,_ = self.make()
        sample = s.sample_point()
        self.assertTrue(sample["valid_for_analysis"])
        self.assertEqual(len(sample["measurements"]), 12)
        self.assertEqual(sample["overload_summary"]["invalid_pairs_by_role"]["lockin_aux"], 0)
        entry = sample["status"]["samples"][0]
        self.assertEqual(set(entry["role_read_times_utc"]), {"xx","xy","aux"})
        for role in ("XY","AUX"):
            self.assertFalse(any(v.startswith(("SLVL ", "SOFF ", "FREQ ")) for v in m.resources["FAKE::"+role].writes))
        self.assertEqual(m.resources["FAKE::XX"].responses["PHAS?"], "13.5")
        self.assertEqual(m.resources["FAKE::XY"].responses["PHAS?"], "-20")
        self.assertTrue(s.cleanup()["verified"])

    def test_one_common_remaining_wait_and_no_repeat_qualification_dwell(self):
        s,_,_ = self.make()
        before = self.clock()
        s.disturbed("optical.tuning_completed")
        self.clock.sleep(1)
        s.qualify()
        self.assertAlmostEqual(self.clock() - before, 1.5) # 0.3 s x fixture's 5 tau
        before = self.clock()
        s.qualify()
        self.assertEqual(self.clock(), before)

    def test_aux_longer_time_constant_controls_shared_wait(self):
        s,_,cfg = self.make(configured=False)
        cfg.lockin.lockin_aux = replace(cfg.lockin.lockin_aux, time_constant_s=1)
        s = InternalLockinGroupSession(self.path,cfg.lockin,lambda *_:None,
                manager_factory=lambda:FakeManager(),monotonic=self.clock,sleep=self.clock.sleep)
        self.assertEqual(s.config.settle_s, 5)

    def test_aux_overload_retains_raw_and_healthy_companions(self):
        s,m,_ = self.make()
        m.resources["FAKE::AUX"].responses["CUROVLDSTAT?"] = "16"
        # Set policy explicitly; no hard/unknown status is excused.
        s.status_policy.mode = "record_continue"
        s.config = replace(s.config,overload_policy="record_continue")
        sample = s.sample_point()
        self.assertFalse(sample["valid_for_analysis"])
        self.assertTrue(reading_allows_continuation(sample))
        row={"status.lockin":sample["status"],**{"measured."+k:v for k,v in sample["measurements"].items()}}
        self.assertEqual(channel_quality(row,"measured.lockin_aux_h1_x_v")[0],"flagged")
        self.assertEqual(channel_quality(row,"measured.lockin_xy_h1_x_v")[0],"clear")
        self.assertEqual(sample["overload_summary"]["invalid_pairs_by_role"]["lockin_aux"],1)
        tampered=deepcopy(sample)
        del tampered["status"]["samples"][0]["samples"]["aux"]
        self.assertFalse(reading_allows_continuation(tampered))

    def test_aux_frequency_mismatch_and_settings_drift_are_terminal(self):
        for command,value in (("FREQEXT?","18"),("IRNG?","1")):
            with self.subTest(command=command):
                s,m,_=self.make()
                m.resources["FAKE::AUX"].responses[command]=value
                with self.assertRaises((ValueError,RuntimeError)):
                    s.sample_point()
                self.assertTrue(s.failed)

    def test_partial_third_read_failure_keeps_prior_role_evidence(self):
        s,m,_=self.make()
        def fail(resource,command):
            if command=="SNAP? X,Y":
                raise OSError("fake third-channel transport failure")
        m.resources["FAKE::AUX"].on_query=fail
        with self.assertRaises(RuntimeError):
            s.sample_point()
        roles=[p["role"] for kind,p in self.events if kind=="lockin_raw_role"]
        self.assertIn("lockin_xx",roles);self.assertIn("lockin_xy",roles)
        self.assertTrue(s.failed)
        self.assertTrue(s.cleanup()["source_protection_verified"])

    def test_dark_subtraction_includes_third_channel_and_own_quality(self):
        s,_,_=self.make()
        dark=s.sample_point()
        b=MatchedLockinBackground(BackgroundConfig(True,1,600))
        b.capture([dark],{"reference":"XX TTL","phase":"unchanged","roles":["xx","xy","aux"]})
        light=deepcopy(dark)
        for role in ("xx","xy","aux"):
            light["measurements"]["lockin_"+role+"_h1_x_v"] += .00001
            light["status"]["samples"][0]["samples"][role]["x_v"] += .00001
        result=b.apply(light,{"reference":"XX TTL","phase":"unchanged","roles":["xx","xy","aux"]})
        self.assertAlmostEqual(result["measurements"]["lockin_aux_h1_delta_x_v"],.00001)

    def test_invalid_aux_declarations_rejected_before_io(self):
        cases=[AUX.replace("FAKE::AUX","FAKE::XX"),
               AUX.replace('model = "SR865A"','model = "SR830"'),
               AUX.replace("sine_output_connected = false","sine_output_connected = true"),
               AUX.replace("1000000.0","50.0"),
               AUX.replace("current_status_supported = true","current_status_supported = false")]
        for text in cases:
            with self.subTest(text=text[:50]),self.assertRaises(ValueError):
                self.config(extra=text)

    def test_disabled_aux_keeps_dual_path_and_missing_policy_fails_closed(self):
        cfg=self.config(extra='\n[lockin_aux]\nenabled=false\n')
        self.assertIsNone(cfg.lockin.lockin_aux)
        safety=self.directory/"lockin_safety.toml"
        safety.write_text(safety.read_text().split("[lockin_aux]")[0])
        self.write_config(order=("lockin",),extra=AUX)
        with self.assertRaisesRegex(ValueError,"explicit.*safety"):
            load_hardware_combination(self.path)

    def test_three_channel_plot_and_matched_dark_render(self):
        from attodry_control.unified_plotting import render_plot
        import matplotlib.pyplot as plt
        s,_,_=self.make()
        dark=s.sample_point()
        b=MatchedLockinBackground(BackgroundConfig(True,1,600))
        b.capture([dark],{"reference":"XX TTL","roles":["xx","xy","aux"]})
        light=b.apply(s.sample_point(),{"reference":"XX TTL","roles":["xx","xy","aux"]})
        light["status"]["background"] = light["background"]
        row={"source_path":"fake.sqlite","run_id":"fake","condition_id":"0","sample_index":0,
             "accepted":True,"actual.optical_power_w":3e-6,"status.lockin":light["status"],
             **{"measured."+k:v for k,v in light["measurements"].items()}}
        figure,data=render_plot([row],{"mode":"three_channels","x":"actual.optical_power_w",
                   "statistics":"raw","photoresponse":"matched"})
        self.addCleanup(plt.close,figure)
        self.assertEqual(len(figure.axes),3)
        self.assertIn("measured.lockin_aux_h1_delta_amplitude_v",data["report"]["channels"])
        self.assertEqual(data["report"]["plotted_row_count"],3)
        self.assertIn("Third sample voltage",figure.axes[2].get_title())

    def test_complete_combination_store_roundtrip_and_cleanup(self):
        from attodry_control.combination_hardware import run_electrical_combination
        from attodry_control.combination_store import CombinationStore
        from attodry_control.combination_analysis import load_combination_rows
        s,m,cfg=self.make(configured=False)
        with CombinationStore(self.database) as store:
            result=run_electrical_combination(cfg,store,"three-fake",authorize_hardware=True,
                confirm_xy_sine_disconnected=True,manager_factory=lambda:m,
                monotonic=self.clock,sleep=self.clock.sleep)
        self.assertEqual(result["status"],"completed",result)
        rows=load_combination_rows(self.database,run_id="three-fake")
        self.assertEqual(len(rows),2)
        self.assertIn("measured.lockin_aux_h1_x_v",rows[0])
        self.assertTrue(rows[0]["valid_for_analysis_by_role"]["lockin_aux"])
        self.assertTrue(m.closed and all(r.closed for r in m.resources.values()))

    def test_aux_unknown_status_is_not_continuable(self):
        s,m,_=self.make()
        s.status_policy.mode="record_continue"
        m.resources["FAKE::AUX"].responses["CUROVLDSTAT?"]="4096"
        with self.assertRaises(ValueError):
            s.sample_point()
        self.assertTrue(s.failed)

    def test_deadline_and_guard_are_shared_with_aux_wait(self):
        s,_,_=self.make()
        s.disturbed("beam_changed")
        s.wait_deadline=self.clock()+.5
        with self.assertRaises(TimeoutError):
            s.sample_point()
        self.assertTrue(s.failed)

    def test_independent_dark_signature_uses_aux_formal_settings(self):
        from attodry_control.optical_background_analysis import _context
        s,_,cfg=self.make()
        light=s.sample_point()
        row={"analysis_context":{"lockin":cfg.snapshot["hardware"]["lockin"]},
             "status.lockin":light["status"],"requested.lockin_frequency_hz":s.target_frequency_hz}
        fingerprint=_context(row,"lockin_aux",1)
        modified=deepcopy(row)
        modified["status.lockin"]["samples"][0]["settings_after"]["aux"]["input_range_v_peak"]=1
        with self.assertRaisesRegex(ValueError,"changed formal settings"):
            _context(modified,"lockin_aux",1)
        self.assertIn("SR865A",fingerprint)

    def test_close_failure_does_not_skip_other_resources(self):
        s,m,_=self.make()
        def bad_close():
            raise OSError("fake close failure")
        m.resources["FAKE::AUX"].close=bad_close
        with self.assertRaisesRegex(RuntimeError,"close failed"):
            s.close()
        self.assertTrue(m.resources["FAKE::XX"].closed)
        self.assertTrue(m.resources["FAKE::XY"].closed)
        self.assertTrue(m.closed)





    def test_actual_xx_nominal_current_and_voltage_limits_remain_enforced(self):
        s,_,_=self.make()
        total=s._source_args.series_resistance_ohm+50+s._source_args.device_resistance_ohm
        s._source_args.max_device_current_a=.004/total
        # Within ordinary readback tolerance, but over the original current
        # boundary. Numeric closeness must not relax the nominal sample limit.
        with self.assertRaisesRegex(ValueError,"current limit"):
            s._check_source({"source_voltage_v":.0040000001,"dc_offset_v":0,"dc_mode":"not_supported"},expected=.004)
        s._source_args.max_device_current_a=1
        s._source_args.max_device_voltage_v=0
        with self.assertRaisesRegex(ValueError,"voltage limit"):
            s._check_source({"source_voltage_v":.004,"dc_offset_v":0,"dc_mode":"not_supported"},expected=.004)

    def test_preillumination_readiness_defers_receiver_frequency_until_qualification(self):
        s,m,_=self.make()
        m.resources["FAKE::AUX"].responses["FREQEXT?"]="0"
        ready=s.verify_ready()
        self.assertTrue(ready["ready"])
        self.assertFalse(ready["companion_reference_qualified"])
        self.assertEqual(s.read_coordinates(verify_companion=False)["lockin_frequency_hz"],s.target_frequency_hz)
        with self.assertRaises((ValueError,RuntimeError)):
            s.qualify()
        self.assertTrue(s.failed)



    def test_legacy_overload_policy_cannot_excuse_unregistered_aux_fault(self):
        from attodry_control.lockin_overload import OverloadPolicy,PhotonicsStatusPolicy
        fault="lockin_aux input/reserve overload"
        self.assertEqual(OverloadPolicy("record_continue").partition([fault])[0],[fault])
        self.assertEqual(PhotonicsStatusPolicy("record_continue",selected={"lockin_aux":(1,)}).partition([fault])[0],[])

    def test_three_h1_receivers_at_operator_8hz(self):
        self.base = self.base.replace("frequency_hz = 17.777", "frequency_hz = 8.0")
        s,_,_=self.make()
        sample=s.sample_point()
        self.assertEqual(sample["actual"]["lockin_frequency_hz"],8)
        for reading in sample["status"]["samples"][0]["samples"].values():
            self.assertEqual(reading["reference_frequency_hz"],8)
            self.assertEqual(reading["detection_frequency_hz"],8)



    def test_receivers_match_actual_master_not_only_nominal_target(self):
        s,_,_=self.make()
        target=s.target_frequency_hz
        with self.assertRaisesRegex(ValueError,"actual XX readback"):
            s._check_frequencies({"xx":target+.0008,"xy":target-.0008,"aux":target})



    def test_no_aux_on_historical_pem_and_readiness_fault_is_terminal(self):
        from attodry_control.photonics_lockin_points import PhotonicsLockinPointSession
        s,m,_=self.make()
        with self.assertRaisesRegex(ValueError,"internal-XX"):
            PhotonicsLockinPointSession(self.path,replace(s.config,reference_topology="pem_xx_xy"),lambda *_:None)
        m.resources["FAKE::AUX"].responses["IRNG?"]="1"
        with self.assertRaises(ValueError):
            s.verify_ready()
        self.assertTrue(s.failed)
        with self.assertRaises(RuntimeError):
            s.verify_ready()


from tests import test_internal_optical_combination as optics
import tomllib

class ThreeOpticalTests(unittest.TestCase):
    write_config = fixtures.ElectricalCombinationTests.write_config
    factory = fixtures.ElectricalCombinationTests.factory
    write_document = optics.InternalOpticalCombinationTests.write_document
    backend_factory = optics.InternalOpticalCombinationTests.backend_factory
    meter_factory = optics.InternalOpticalCombinationTests.meter_factory
    chopper_factory = optics.InternalOpticalCombinationTests.chopper_factory
    execute = optics.InternalOpticalCombinationTests.execute

    def setUp(self):
        optics.InternalOpticalCombinationTests.setUp(self)
        self.document.update(tomllib.loads(AUX))
        self.document["optical_background"]={"enabled":True,"samples":2,"max_age_s":600,
                                              "capture_mode":"per_condition"}
        self.document["lockin_aux"]["time_constant_s"]=.01
        with (self.directory/"lockin_safety.toml").open("a") as f:
            f.write(POLICY)
        self.write_document()
        self.manager=FakeManager(self.document)
        self.manager.resources["FAKE::AUX"]=FakeResource("SR865A","lockin_aux")
        for address,resource in self.manager.resources.items():
            for key in ("FREQ?","FREQEXT?","FREQDET?","FREQINT?"):
                resource.responses[key]="100"
            if address=="FAKE::XX":
                resource.responses["FMOD?"]="1"

    def test_optics_dark_three_lockin_full_lifecycle(self):
        from attodry_control.combination_analysis import load_combination_rows
        result=self.execute("optical-three")
        self.assertEqual(result["status"],"completed",result)
        self.assertTrue(result["cleanup"]["clean"],result["cleanup"])
        rows=load_combination_rows(self.database,run_id="optical-three")
        self.assertEqual(len(rows),2)
        for row in rows:
            self.assertIn("measured.lockin_aux_h1_delta_x_v",row)
            self.assertEqual(row["status.lockin"]["background"]["channels"]["aux:h1"]["dark_count"],2)
        self.assertEqual(self.backend.source["emission_state"],0)
        self.assertTrue(self.meter.closed)
        self.assertTrue(self.manager.closed and all(r.closed for r in self.manager.resources.values()))

    def test_offline_launch_preview_shows_aux_model_clock_and_scale(self):
        from attodry_control.combination_launch import launch_summary
        from attodry_control.combination_terminal import launch_text
        cfg=load_hardware_combination(self.path)
        text=launch_text(launch_summary(cfg,self.database,"offline"))
        self.assertIn("lockin_aux (SR865A)",text)
        self.assertIn("AUX SR865A REF IN",text)
        self.assertIn("1 Mohm",text)
        self.assertFalse(self.manager.opened)

    def test_optics_smu_three_channels_with_matched_gate_darks(self):
        from tests.test_three_smu_config import OPERATION_TEXT
        from attodry_control.combination_analysis import load_combination_rows
        smu=tomllib.loads(OPERATION_TEXT)
        smu.pop("smu_bias");smu.pop("gate_bottom")
        smu["three_smu_run"].update(delay_s=.03,serpentine=False)
        smu["three_smu_run"]["smu_bias"]={"role":"off","bidirectional":False}
        smu["three_smu_run"]["gate_bottom"]={"role":"off","bidirectional":False}
        smu["three_smu_run"]["gate_top"]={"role":"sweep","bidirectional":False,
                                          "points":[-.1,0,.1],"zero_readback_tolerance_v":.05}
        self.document.update(smu)
        self.document["combination_scan"].update(order=["optical","smu","lockin"],
                         illumination_policy="continuous_gate_scan",samples_per_condition=1)
        self.write_document()
        result=self.execute("optical-gate-three")
        self.assertEqual(result["status"],"completed",result)
        rows=load_combination_rows(self.database)
        self.assertEqual(len(rows),3)
        self.assertTrue(all("measured.lockin_aux_h1_delta_x_v" in r for r in rows))

    def test_three_detectors_with_xx_synchronized_chopper(self):
        optics.InternalOpticalCombinationTests.enable_chopper(self)
        result=self.execute("optical-chopper-three")
        self.assertEqual(result["status"],"completed",result)
        self.assertTrue(result["cleanup"]["clean"],result["cleanup"])
        self.assertTrue(self.chopper.closed)
        self.assertEqual(self.backend.source["emission_state"],0)

