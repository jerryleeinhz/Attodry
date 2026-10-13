"""Offline commissioning and fault tests. Never open a real VISA/DLL resource."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import ctypes as ct
import io
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

from attodry_control.nkt_config import NktError
from attodry_control.nkt_control import NktController, SimulatedNkt
from attodry_control.photoresponse import PhotoresponseRunner, SimulatedElectrical
from attodry_control.photoresponse_analysis import load_results, export_results
from attodry_control.photoresponse_cld import CldController, SimulatedCld, TL4000
from attodry_control.photoresponse_cli import main
from attodry_control.photoresponse_config import load_photoresponse_config
from attodry_control.photoresponse_environment import load_environment, open_environment
from attodry_control.photoresponse_hardware import ElectricalHardwareConfig, HardwareElectrical
from attodry_control.photoresponse_storage import PhotoStore, run_lock
from attodry_control.photoresponse_transport import VisaTransport, Serial2182Transport, visa_resources
from attodry_control.models import VectorField
from test_photoresponse import Clock, setup, example
from test_photoresponse_hardware import Resource, source

ROOT = Path(__file__).resolve().parents[1]


def cld_config(**changes):
    config = load_photoresponse_config(ROOT / "config/photoresponse_cld_simulation.toml")
    return replace(config, database_path=None, **changes)


def environment_config():
    return load_environment(tomllib.loads((ROOT / "config/photoresponse_environment_simulation.toml").read_text()), False)


def hardware_setup(mode="dc_current_voltage", detector="keithley2182a", relay=True):
    config = cld_config()
    optical = replace(config.nkt, backend="tl4000", dll_path="fake.dll", address="FAKE::CLD::INSTR",
                      expected_serial="FAKE", diode_model="FAKE DIODE", diode_wiring_confirmed=True)
    host, meter = Resource(), Resource("2182A" if detector == "keithley2182a" else "2400")
    host.downstream = meter
    source_config = {"address":"FAKE::6221::INSTR", "expected_idn":host.values["*IDN"]}
    if mode == "dc_current_voltage":
        source_config.update(compliance_v=.1, max_step_a=2.5e-9, settle_s=0, tolerance_a=1e-13)
    d = {"expected_idn":meter.values["*IDN"], "voltage_range_v":.1 if detector == "keithley2182a" else .2, "nplc":1}
    if not relay: d["address"] = "FAKE::DETECTOR::INSTR"
    if detector == "keithley2400":
        d.update(current_range_a=1e-6, compliance=1e-8 if mode == "voltage_bias_current" else .2,
                 max_step_v=.01, settle_s=0, tolerance_v=1e-6)
    hardware = ElectricalHardwareConfig(10000, True, True, "via_6221" if relay else "visa",
        source_config if relay or mode == "dc_current_voltage" else None, d)
    biases = {"dc_current_voltage":(4e-12, 1e-8), "open_circuit_voltage":(None,), "voltage_bias_current":(0., .001)}
    config = replace(config, nkt=optical, mode=mode, detector=detector, hardware=hardware,
                     bias_points=biases[mode], max_abs_current_a=1e-8, max_abs_voltage_v=.1,
                     samples_per_window=1, sample_interval_s=.001, settle_s=0)
    transports = {"6221":VisaTransport(host,role="6221",authorize_writes=True),
                  "detector":VisaTransport(meter,role="detector",authorize_writes=True)}
    electrical = HardwareElectrical(config,transports,sleep=lambda _:None)
    backend = SimulatedCld(config.nkt)
    backend.is_hardware = True  # Explicit fake of the production contract.
    clock = Clock()
    laser = CldController(config.nkt,backend,clock=clock,sleep=clock.sleep)
    runner = PhotoresponseRunner(config,laser,electrical,authorize_writes=True,confirm_manual_route=True)
    return runner, host, meter, electrical, backend


class HardwareIntegrationTests(unittest.TestCase):
    def test_complete_relay_direct_and_2400_modes(self):
        for mode, detector, relay in [("dc_current_voltage","keithley2182a",True),
                ("open_circuit_voltage","keithley2182a",True),
                ("open_circuit_voltage","keithley2182a",False),
                ("voltage_bias_current","keithley2400",False),
                ("open_circuit_voltage","keithley2400",False),
                ("dc_current_voltage","keithley2400",False)]:
            with self.subTest(mode=mode,detector=detector,relay=relay):
                runner,host,meter,electrical,backend = hardware_setup(mode,detector,relay)
                record = runner.run()
                self.assertTrue(record["completed"],record["error"] or record["cleanup"])
                self.assertTrue(all(row["accepted"] for row in record["conditions"]))
                self.assertFalse(backend.state["getLdOutputState"])
                if mode == "open_circuit_voltage":
                    self.assertIsNone(electrical.source)
                    self.assertFalse(any(c.startswith("SOUR:") and not c.endswith("?") for c in host.commands))
                    self.assertNotIn("OUTP:STAT 1",host.commands)
                else:
                    self.assertIn("READ?",meter.commands)

    def test_bad_detector_identity_never_enables_source(self):
        runner,host,meter,electrical,_ = hardware_setup()
        meter.values["*IDN"] = "WRONG"
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertNotIn("OUTP:STAT 1",host.commands)
        self.assertFalse(electrical.source.owned)

    def test_armed_delta_blocks_forwarding_without_disarming_another_session(self):
        runner,host,meter,_,_ = hardware_setup()
        host.values["SOUR:DELT:ARM"]="1"
        self.assertFalse(runner.run()["completed"])
        self.assertEqual(meter.commands,[])
        self.assertFalse(any(not c.endswith("?") for c in host.commands))

    def test_active_source_blocks_optics_and_initial_environment_preparation(self):
        runner,host,_,_,backend=hardware_setup()
        env_config=environment_config()
        config=replace(runner.config,environment=env_config)
        runner.config=config
        runner.environment=open_environment(env_config)
        host.values["OUTP:STAT"]="1"
        before=dict(backend.state)
        record=runner.run()
        self.assertFalse(record["completed"])
        self.assertEqual(backend.state,before)
        self.assertNotIn("cryostat.set_temperature",runner.environment.driver.event_log)
        self.assertEqual(host.values["OUTP:STAT"],"1") # No takeover of someone else's output.

    def test_raw_overload_is_preserved_and_every_output_cleaned(self):
        runner,host,meter,electrical,backend = hardware_setup()
        original = meter.query
        meter.query = lambda cmd: "9.9e37" if cmd == "READ?" else original(cmd)
        record = runner.run()
        self.assertFalse(record["completed"])
        sample = record["conditions"][0]["windows"][0]["samples"][0]
        self.assertEqual(sample["detector_last_raw_reading"], "9.9e37")
        self.assertEqual(host.values["OUTP:STAT"],"0")
        self.assertFalse(backend.state["getLdOutputState"])

    def test_cleanup_continues_to_2400_when_6221_cleanup_raises(self):
        _,_,meter,electrical,_ = hardware_setup("dc_current_voltage","keithley2400",False)
        electrical.begin()
        with patch.object(electrical.source,"cleanup",side_effect=OSError("source failed")):
            with self.assertRaises(NktError): electrical.cleanup()
        self.assertEqual(meter.values["OUTP"],"0")

    def test_2400_24_bit_status_rejects_range_compliance_and_processing(self):
        for bit in (0,1,3,4,5,6,10,13,16,17,18,22,23):
            _,_,meter,electrical,_ = hardware_setup("voltage_bias_current","keithley2400",False)
            electrical.begin()
            original = meter.query
            meter.query = lambda cmd: f".001,1e-9,{22528 | (1<<bit)}" if cmd == "READ?" else original(cmd)
            with self.assertRaises(NktError): electrical.read(illuminated=False)
            self.assertTrue(electrical.detector.cleanup()["safe"])

    def test_resistance_function_disabled_before_2400_output(self):
        _,_,meter,electrical,_ = hardware_setup("voltage_bias_current","keithley2400",False)
        meter.values["SENS:FUNC"] = "RES,VOLT:DC"
        electrical.begin()
        self.assertLess(meter.commands.index('SENS:FUNC:OFF "RES"'),meter.commands.index("OUTP 1"))
        electrical.cleanup()

    def test_interlock_asserted_is_not_6221_error(self):
        device, resource, _ = source()
        resource.values["STAT:MEAS:COND"] = "2"
        device.begin()
        device.set_bias(4e-12)
        self.assertTrue(device.cleanup()["safe"])

    def test_last_full_source_state_survives_partial_query_failure(self):
        device, resource, _ = source()
        prior = device.snapshot()
        resource.failure = "STAT:QUES:COND?"
        with self.assertRaises(OSError): device.snapshot()
        self.assertEqual(device.last_confirmed_state,prior)
        self.assertFalse(device.state_current)

    def test_relay_empty_timeout_cannot_consume_late_reply_as_new_query(self):
        host=Resource()
        host.downstream=Resource("2182A")
        host.downstream.query=lambda cmd:""
        clock=Clock()
        relay=Serial2182Transport(VisaTransport(host,role="host"),timeout_s=.2,sleep=clock.sleep,clock=clock)
        with self.assertRaises(TimeoutError): relay.query("*IDN?")
        count=len(host.commands)
        host.pending="late response"
        with self.assertRaises(NktError): relay.query("*IDN?")
        self.assertEqual(len(host.commands),count)

    def test_transport_audit_failure_preserves_primary_and_poison(self):
        r=Resource(); r.failure="*IDN?"
        def bad_audit(row): raise RuntimeError("disk full")
        t=VisaTransport(r,role="test",on_event=bad_audit)
        with self.assertRaisesRegex(OSError,"bus failure"): t.query("*IDN?")
        self.assertTrue(t.poisoned)
        self.assertIn("audit_error",t.transcript[-1])

    def test_resource_factory_is_never_called_without_permission(self):
        factory=Mock()
        with self.assertRaises(NktError):
            with visa_resources({"test":"FAKE::INSTR"},timeout_ms=1000,manager_factory=factory): pass
        factory.assert_not_called()

    def test_gpib_relay_reads_through_eoi_without_leaving_second_newline(self):
        class FramedResource(Resource):
            # The real 6221 returns two LFs at the end of an ENTER response.
            pending_bytes = ""
            def query(self, command):
                response = super().query(command)
                self.pending_bytes += response + ("\n\n" if command == "SYST:COMM:SER:ENT?" else "\n")
                if self.read_termination:
                    answer, _, self.pending_bytes = self.pending_bytes.partition(self.read_termination)
                else:
                    answer, self.pending_bytes = self.pending_bytes, ""
                return answer
            def lock_excl(self, **kwargs): pass
            def close(self): pass
        r = FramedResource()
        r.downstream = Resource("2182A")
        r.downstream.values["SENS:FUNC"] = '"VOLT:DC"'
        manager = Mock()
        manager.open_resource.return_value = r
        with visa_resources({"host":"GPIB0::1::INSTR"}, timeout_ms=1000,
                authorize_connection=True, manager_factory=lambda:manager) as resources:
            relay = Serial2182Transport(resources["host"], sleep=lambda _:None)
            self.assertEqual(relay.query("*IDN?"), r.downstream.values["*IDN"])
            self.assertEqual(r.pending_bytes, "")
            self.assertEqual(relay.query("SENS:FUNC?"), "VOLT:DC")
            self.assertEqual(resources["host"].query("OUTP:STAT?"), "0")
            self.assertTrue(resources["host"].transcript[1]["response"].endswith("\n\n"))
        manager.close.assert_called_once()

    def test_non_gpib_resources_keep_newline_termination(self):
        manager = Mock()
        with visa_resources({"meter":"TCPIP0::example.invalid::INSTR"}, timeout_ms=1000,
                authorize_connection=True, manager_factory=lambda:manager):
            self.assertEqual(manager.open_resource.return_value.read_termination, "\n")


class CldTests(unittest.TestCase):
    def make(self):
        c=cld_config().nkt; clock=Clock(); backend=SimulatedCld(c)
        return CldController(c,backend,clock=clock,sleep=clock.sleep),backend

    def test_interlock_or_bad_protection_never_takes_ownership(self):
        for key,value in (("isTrippedLdOutputProtInterlock",True),("getTempProtMode",0),
                          ("getLdOutpAutoOnState",True),("getLdOutputState",True)):
            controller,backend=self.make()
            backend.state[key]=value
            before=dict(backend.state)
            with self.assertRaises(NktError): controller.begin()
            controller.finish()
            self.assertFalse(controller.owned)
            self.assertEqual(backend.state,before)

    def test_temperature_timeout_prevents_emission_and_can_cleanup(self):
        controller,backend=self.make()
        backend.state["measTemp"]=26.
        with self.assertRaises(TimeoutError): controller.begin()
        with self.assertRaises(NktError): controller.prepare_point(controller.config.points[0])
        self.assertTrue(controller.finish()["off_confirmed"])
        self.assertFalse(backend.state["getTecOutputState"])

    def test_drift_fault_cannot_resume_and_restores_original_tec(self):
        controller,backend=self.make()
        backend.state["getTecOutputState"]=True
        controller.begin(); controller.prepare_point(controller.config.points[0])
        controller.set_emission(True)
        backend.state["measTemp"]=26.
        with self.assertRaises(NktError): controller.verify_point(emitting=True)
        backend.state["measTemp"]=25.
        with self.assertRaises(NktError): controller.prepare_point(controller.config.points[1])
        self.assertTrue(controller.finish()["off_confirmed"])
        self.assertTrue(backend.state["getTecOutputState"])

    def test_failed_off_readback_is_not_confirmed(self):
        controller,backend=self.make()
        controller.begin(); controller.prepare_point(controller.config.points[0]); controller.set_emission(True)
        with patch.object(backend,"snapshot",side_effect=OSError("lost link")):
            result=controller.finish()
        self.assertFalse(result["off_confirmed"])
        self.assertFalse(controller.record["state_current"])
        self.assertTrue(result["errors"])

    def test_dll_signatures_init_without_reset_and_error_closes_session(self):
        c=replace(cld_config().nkt,backend="tl4000",dll_path="fake.dll",address="FAKE::INSTR",
                  expected_serial="FAKE",diode_model="FAKE",diode_wiring_confirmed=True)
        class Dll:
            def __init__(self,fail=False): self.calls=[]; self.fail=fail
            def __getattr__(self,name):
                def call(*args):
                    self.calls.append((name,args))
                    if name=="TL4000_init":
                        self.assertions=(args[1].value,args[2].value)
                        args[3]._obj.value=42
                        return -1 if self.fail else 0
                    return 0
                return call
        dll=Dll()
        driver=TL4000(c,dll=dll,authorize_connection=True,authorize_writes=True)
        self.assertEqual(dll.assertions,(1,0))
        self.assertEqual(driver.session.value,42)
        driver.close()
        self.assertEqual(dll.calls[-1][0],"TL4000_close")
        dll=Dll(True)
        with self.assertRaises(NktError): TL4000(c,dll=dll,authorize_connection=True)
        self.assertEqual(dll.calls[-1][0],"TL4000_close")
        with patch("ctypes.CDLL") as load:
            with self.assertRaises(NktError): TL4000(replace(c,diode_wiring_confirmed=False),authorize_connection=True,authorize_writes=True)
            load.assert_not_called()


class EnvironmentTests(unittest.TestCase):
    def test_vector_limit_and_planned_temperature_jump(self):
        c=environment_config()
        for points in (({"temperature_k":1.6,"bx_t":2.2,"bz_t":2.2},),
                       (c.points[0],{"temperature_k":3.,"bx_t":0.,"bz_t":0.})):
            with self.assertRaises(ValueError): replace(c,points=points).validate()

    def test_controls_setpoint_and_temperature_drift_checks(self):
        env=open_environment(environment_config())
        env.begin(); env.prepare(env.config.points[0])
        for updates in ({"field_setpoint":VectorField(.1,0.)},{"sample_temperature_k":1.7},
                        {"field_control_enabled":False}):
            previous=env.driver._state
            env.driver._state=replace(previous,**updates)
            with self.assertRaises(NktError): env.verify()
            env.driver._state=previous
        self.assertFalse(env.finish(failed=False)["field_zero_confirmed"])

    def test_field_failure_keeps_last_nonzero_and_never_claims_zero(self):
        env=open_environment(environment_config())
        env.begin(); env.prepare(env.config.points[1])
        previous=env.last_confirmed_state
        env.driver.fail_next("request_zero_field")
        result=env.finish(failed=True)
        self.assertFalse(result["field_zero_confirmed"])
        self.assertFalse(result["state_current"])
        self.assertEqual(result["last_confirmed_state"],previous)
        self.assertGreater(result["last_confirmed_state"]["field"]["bz_t"],0)

    def test_full_scan_moves_environment_only_with_electrical_and_light_off(self):
        c=replace(example(),environment=environment_config(),mode="dc_current_voltage",bias_points=(1e-6,))
        clock=Clock(); optical=SimulatedNkt(c.nkt)
        laser=NktController(c.nkt,optical,clock=clock,sleep=clock.sleep)
        electrical=SimulatedElectrical(c,clock=clock)
        env=open_environment(c.environment)
        original=env.prepare
        def prepare(point):
            self.assertFalse(electrical.state["source"]["output_enabled"])
            return original(point)
        env.prepare=prepare
        record=PhotoresponseRunner(c,laser,electrical,environment=env,clock=clock,sleep=clock.sleep).run()
        self.assertTrue(record["completed"],record["error"])
        self.assertEqual(len(record["conditions"]),4)
        self.assertEqual(record["conditions"][-1]["windows"][0]["samples"][0]["environment_after"]["measurement_temperature_k"],2.)


class StorageAndCliTests(unittest.TestCase):
    def test_interrupted_retry_preserves_raw_and_rejects_changed_plan_or_duplicate_acceptance(self):
        with tempfile.TemporaryDirectory() as td:
            store=PhotoStore(Path(td)/"audit.sqlite"); c=example()
            first,_=store.start(c)
            store.event({"phase":"raw_sample","raw":"retained before crash"})
            with self.assertRaises(NktError): store.start(replace(c,repeats=2),resume=first)
            second,attempt=store.start(c,resume=first)
            self.assertEqual(attempt,2)
            record=setup(c)[0].run()
            store.finalize(record)
            with self.assertRaises(NktError): store.start(c,resume=first)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM photo_events WHERE run_id=?",(first,)).fetchone()[0],1)
            self.assertEqual(store.db.execute("SELECT outcome FROM photo_runs WHERE run_id=?",(first,)).fetchone()[0],"interrupted")
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM photo_conditions WHERE accepted=1").fetchone()[0],2)
            store.close()

    def test_process_lock_releases_after_failure(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"run.lock"
            with run_lock(path):
                with self.assertRaises((NktError,OSError)):
                    with run_lock(path): pass
            with run_lock(path): pass

    def test_cld_cli_sqlite_and_accepted_export(self):
        with tempfile.TemporaryDirectory() as td:
            source=(ROOT/"config/photoresponse_cld_simulation.toml").read_text()
            source=source.replace('"../run_data/photoresponse_cld_simulation"','"output"')
            source=source.replace('"../run_data/photoresponse_cld_simulation/audit.sqlite"','"output/audit.sqlite"')
            path=Path(td)/"local.toml"; path.write_text(source)
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                self.assertEqual(main(["simulate","--config",str(path)]),0)
            record=next((Path(td)/"output").glob("*_completed.json"))
            self.assertEqual(len(load_results(record)),6)
            manifest=export_results(record,Path(td)/"results.csv")
            self.assertTrue(manifest["accepted_only"])
            self.assertEqual(manifest["rows"],6)

    def test_incomplete_hardware_templates_reject_without_loading_dll_or_visa(self):
        visa=Mock()
        with patch("ctypes.CDLL") as dll,patch.dict("sys.modules",{"pyvisa":Mock(ResourceManager=visa)}):
            for name in ("photoresponse_hardware.example.toml","photoresponse_cld_hardware.example.toml"):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["run","--config",str(ROOT/"config"/name),
                        "--authorize-connect","--authorize-writes","--confirm-route"]),2)
            dll.assert_not_called(); visa.assert_not_called()

    def test_6221_bench_configuration_and_permission_before_resource_creation(self):
        from attodry_control.photoresponse_commission import test_6221
        with self.assertRaises(NktError): test_6221(ROOT/"config/photoresponse_6221_test.example.toml")

    def test_6221_standalone_preflight_failure_has_no_output_writes(self):
        from attodry_control.photoresponse_commission import test_6221
        class BenchResource(Resource):
            def lock_excl(self, **kwargs): pass
            def close(self): self.closed=True
        for empty_idn in (False,True):
            r=BenchResource(); r.closed=False
            manager=Mock(); manager.open_resource.return_value=r
            text=(ROOT/"config/photoresponse_6221_test.example.toml").read_text()
            text=text.replace('"CHANGE_ME"','"FAKE::6221::INSTR"').replace('"CHANGE_ME_FULL_6221_IDN"',json.dumps(r.values["*IDN"]))
            text=text.replace('settle_s = 0.2','settle_s = 0.0')
            if empty_idn: r.values["*IDN"]=""
            with tempfile.TemporaryDirectory() as td:
                path=Path(td)/"local.toml"; path.write_text(text)
                record=test_6221(path,authorize_connection=True,authorize_writes=True,manager_factory=lambda:manager)
            self.assertEqual(record["completed"],not empty_idn)
            if empty_idn: self.assertEqual(r.commands,["*IDN?"])
            else: self.assertEqual(len(record["points"]),5)
            self.assertTrue(r.closed)
            manager.close.assert_called_once()


if __name__ == "__main__": unittest.main()
