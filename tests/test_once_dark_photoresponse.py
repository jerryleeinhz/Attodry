"""One-time dark lifecycle, legacy defaults, command authorization and offline analysis."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import math
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest
from unittest.mock import patch

from tests.test_optical_window_recovery import OpticalWindowRecoveryTests
from tests.test_optical_background import reading
from tests.test_three_smu_config import OPERATION_TEXT
from optical_helpers import Clock
from attodry_control.optical_background import BackgroundConfig, MatchedLockinBackground
from attodry_control.optical_background_analysis import subtract_dark, dark_mean_variance
from attodry_control.combination_cli import run
from attodry_control.unified_plotting import render_plot
from attodry_control.analysis_observations import channel_quality, repeat_statistics


class OnceDarkIntegrationTests(OpticalWindowRecoveryTests):
    def test_once_gate_change_keeps_raw_without_remeasuring_dark(self):
        smu=tomllib.loads(OPERATION_TEXT)
        smu.pop("smu_bias")
        smu.pop("gate_bottom")
        smu["three_smu_run"].update(delay_s=.03,serpentine=False)
        smu["three_smu_run"]["smu_bias"]={"role":"off","bidirectional":False}
        smu["three_smu_run"]["gate_bottom"]={"role":"off","bidirectional":False}
        smu["three_smu_run"]["gate_top"]=dict(role="sweep",bidirectional=False,
            points=[-.1,.1],zero_readback_tolerance_v=.05)
        self.document.update(smu)
        self.document["optical_background"]=dict(enabled=True,samples=1,max_age_s=1000,capture_mode="once_before_scan")
        self.document["combination_scan"].update(order=["optical","smu","lockin"],
            illumination_policy="continuous_gate_scan",samples_per_condition=1)
        self.write_document()
        result=self.execute("once-gates")
        self.assertEqual(result["status"],"completed",result)
        self.assertEqual(len([p for k,p in self.events() if k=="optical_dark_background"]),1)
        first,second=self.stored_samples()
        self.assertIn("lockin_xy_h1_delta_x_v",first["measurements"])
        self.assertIn("lockin_xy_h1_x_v",second["measurements"])
        self.assertNotIn("lockin_xy_h1_delta_x_v",second["measurements"])
        self.assert_closed_laser_off()

    def test_once_ordinary_window_retry_does_not_recapture(self):
        self.retries()
        self.document["optical_background"]=dict(enabled=True,samples=1,max_age_s=1000,capture_mode="once_before_scan")
        self.write_document()
        fault,_=self.after_guard_injection(self.target_deviation)
        with fault:result=self.execute("once-retry")
        self.assertEqual(result["status"],"completed",result)
        self.assertEqual(len([p for k,p in self.events() if k=="optical_dark_background"]),1)
        self.assertTrue(any(k=="optical_window_rejected" for k,p in self.events()))
        self.assert_closed_laser_off()

    def test_once_across_optical_points_repeats_and_new_runs(self):
        self.document["power_feedback"].update(target_mapping="cartesian", target_powers_w=[5e-6,8e-6])
        self.document["combination_scan"]["repeats"] = 2
        self.document["optical_background"] = dict(enabled=True,samples=2,max_age_s=1000,capture_mode="once_before_scan")
        self.write_document()
        result = self.execute("once")
        self.assertEqual(result["status"], "completed", result)
        baselines=[p for k,p in self.events() if k=="optical_dark_background"]
        self.assertEqual(len(baselines),1)
        samples=self.stored_samples()
        self.assertEqual(len(samples),8)
        ids={next(r for r in s["reads"] if r["module"]=="lockin")["background"]["baseline_id"] for s in samples}
        self.assertEqual(ids,{baselines[0]["baseline_id"]})
        self.assertTrue(all("lockin_xy_h1_delta_x_v" in s["measurements"] for s in samples))
        # A new execution constructs a new background owner, not a global cache.
        result=self.execute("new-run")
        self.assertEqual(result["status"],"completed",result)
        self.assertEqual(len([p for k,p in self.events() if k=="optical_dark_background"]),2)

    def test_once_expiry_keeps_raw_acquisition_and_marks_differences(self):
        self.document["optical_background"] = dict(enabled=True,samples=1,max_age_s=.001,capture_mode="once_before_scan")
        self.write_document()
        result=self.execute("expired")
        self.assertEqual(result["status"],"completed",result)
        self.assertTrue(self.stored_samples())
        for s in self.stored_samples():
            self.assertIn("lockin_xy_h1_x_v",s["measurements"])
            self.assertNotIn("lockin_xy_h1_delta_x_v",s["measurements"])
            bg=next(r for r in s["reads"] if r["module"]=="lockin")["background"]
            self.assertTrue(bg["expired"])
            self.assertFalse(bg["valid_for_analysis"])

    def test_cli_run_authorizes_optics_without_flags_or_prompt(self):
        self.write_document()
        with patch("attodry_control.combination_hardware.run_hardware_combination",return_value={"status":"completed"}) as runner, patch("builtins.input") as prompt, redirect_stdout(io.StringIO()):
            result=run(["run","--json","--config",str(self.path),"--database",str(self.database),"--run-id","cli-optical"])
        self.assertEqual(result,0)
        self.assertTrue(runner.call_args.kwargs["authorize_optical"])
        self.assertTrue(runner.call_args.kwargs["confirm_optical_route"])
        self.assertEqual(runner.call_args.args[0].snapshot["launch"]["authorization_method"],"run_command")
        prompt.assert_not_called()


def row(run_id, index, x, y, *, dark=False, invalid_xx=False):
    r={"source_path":"fixture.sqlite","run_id":run_id,"condition_id":"condition-0","sample_index":index,
       "attempt_index":0,"repeat_index":0,"accepted":True,"simulated":False,
       "finished_at_utc":f"2026-10-17T12:{0 if dark else 1:02d}:{index:02d}+00:00",
       "analysis_context":{"lockin":{"models":["SR830","SR830"]},"optical":{}},
       "requested.lockin_frequency_hz":8,
       "status.optical":{"readback":{"nkt":{"source":{"emission_state":0 if dark else 3}}}},
       "status.lockin":{"samples":[]}}
    if not dark:r["measured.optical_power_w"]=3e-6
    settings={role:{"phase_deg":0,"reference":1 if role=="lockin_xx" else 0,
                    "sensitivity_full_scale_v":1,"harmonic":1} for role in ("lockin_xx","lockin_xy")}
    sample={"harmonic":1,"settings_verified":True,"selected_roles":["xx","xy"],
            "settings_before":{"roles":settings},"settings_after":{"roles":deepcopy(settings)},
            "valid_for_analysis_by_role":{"lockin_xx":not invalid_xx,"lockin_xy":True}}
    for role in ("xx","xy"):
        prefix="measured.lockin_"+role+"_h1_"
        r.update({prefix+"x_v":x,prefix+"y_v":y,prefix+"amplitude_v":math.hypot(x,y),prefix+"phase_deg":math.degrees(math.atan2(y,x))})
        sample["lockin_"+role]={"lia_status":{"raw":4 if role=="xx" and invalid_xx else 0},"error_status":0,
                                "reading":{"harmonic":1}}
    r["status.lockin"]["samples"]=[sample]
    return r


class PhotoresponseTests(unittest.TestCase):
    def rows(self):
        return [row("dark",0,.01,.02,dark=True),row("dark",1,.03,.02,dark=True),
                row("light",0,.05,.06),row("light",1,.05,.06)]

    def test_independent_subtraction_provenance_and_shared_error(self):
        rows=self.rows();original=deepcopy(rows)
        values=subtract_dark(rows,mode="separate",dark_run_id="dark",phase_confirmed=True)
        self.assertEqual(rows,original)
        key="measured.lockin_xy_h1_delta_x_v"
        self.assertAlmostEqual(values[0][key],.03)
        self.assertAlmostEqual(values[0]["measured.lockin_xy_h1_delta_amplitude_v"],.05)
        self.assertAlmostEqual(dark_mean_variance(values,key),.0001)
        stats=repeat_statistics(values,x="measured.optical_power_w",y=key,mode="mean_sem")
        self.assertAlmostEqual(stats[0]["sem"],.01)  # not divided by sqrt(two light samples)
        self.assertEqual(channel_quality(values[0],key)[0],"clear")
        self.assertEqual(values[0]["status.lockin"]["background"]["dark_run_id"],"dark")
        self.assertNotIn("measured.optical_power_w",rows[0])

    def test_invalid_xx_does_not_exclude_xy_and_no_stale_delta(self):
        rows=self.rows()
        for r in rows:
            r["status.lockin"]["samples"][0]["lockin_xx"]["lia_status"]["raw"]=4
        rows[-1]["measured.lockin_xx_h1_delta_x_v"]=999
        values=subtract_dark(rows,mode="separate",dark_run_id="dark",phase_confirmed=True)
        self.assertNotIn("measured.lockin_xx_h1_delta_x_v",values[-1])
        self.assertIn("measured.lockin_xy_h1_delta_x_v",values[-1])

    def test_missing_off_phase_context_and_age_are_not_silently_accepted(self):
        with self.assertRaisesRegex(ValueError,"Confirm"):
            subtract_dark(self.rows(),mode="separate",dark_run_id="dark")
        rows=self.rows();rows[0]["status.optical"]={}
        with self.assertRaisesRegex(ValueError,"OFF"):
            subtract_dark(rows,mode="separate",dark_run_id="dark",phase_confirmed=True)
        for change in ("age","context","settings"):
            rows=self.rows()
            if change=="context":rows[-1]["requested.gate_bottom_v"]=1
            if change=="settings":rows[-1]["status.lockin"]["samples"][0]["settings_after"]["roles"]["lockin_xy"]["phase_deg"]=2
            result=subtract_dark(rows,mode="separate",dark_run_id="dark",phase_confirmed=True,max_age_s=1 if change=="age" else 600)
            self.assertNotIn("measured.lockin_xy_h1_delta_x_v",result[-1])

    def test_render_selected_dark_and_exportable_corrected_rows(self):
        fig,data=render_plot(self.rows(),dict(mode="curve",x="measured.optical_power_w",
            y="measured.lockin_xy_h1_x_v",photoresponse="separate",dark_run_id="dark",
            dark_phase_confirmed=True,statistics="mean_sem",curve_style="points"))
        self.assertEqual(data["report"]["plotted_row_count"],2)
        self.assertIn("delta_x_v",data["summary_rows"][0]["channel"])
        import matplotlib.pyplot as plt
        plt.close(fig)

    def test_once_context_mismatch_keeps_raw_and_no_reacquire(self):
        c=Clock();bg=MatchedLockinBackground(BackgroundConfig(True,2,10,"once_before_scan"),clock=c)
        bg.capture([reading(),reading()],{"gate":0})
        self.assertFalse(bg.needs_capture)
        result=bg.apply(reading(),{"gate":1})
        self.assertTrue(result["clean"])
        self.assertFalse(result["background"]["context_matches"])
        self.assertFalse(any("delta_" in k for k in result["measurements"]))
        bg.invalidate("phase reset")
        self.assertFalse(bg.needs_capture)
        result=bg.apply(reading(),{"gate":0})
        self.assertTrue(result["clean"])
        self.assertFalse(result["background"]["valid_for_analysis"])

    def test_invalid_dark_setting_sample_is_excluded_with_reason(self):
        rows=self.rows()
        rows[0]["status.lockin"]["samples"][0]["settings_verified"]=False
        values=subtract_dark(rows,mode="separate",dark_run_id="dark",phase_confirmed=True)
        channel=values[0]["status.lockin"]["background"]["channels"]["xy:h1"]
        self.assertEqual(channel["dark_count"],1)
        self.assertEqual(len(channel["dark_context_rejections"]),1)
        self.assertIsNone(dark_mean_variance(values,"measured.lockin_xy_h1_delta_x_v"))

    def test_zero_complex_difference_has_no_phase_or_phase_error(self):
        rows=self.rows()
        for r in rows[2:]:
            for role in ("xx","xy"):
                r[f"measured.lockin_{role}_h1_x_v"]=.02
                r[f"measured.lockin_{role}_h1_y_v"]=.02
        values=subtract_dark(rows,mode="separate",dark_run_id="dark",phase_confirmed=True)
        key="measured.lockin_xy_h1_delta_phase_deg"
        self.assertNotIn(key,values[0])
        self.assertIsNone(dark_mean_variance(values,key))

    def test_ambiguous_dark_source_and_manual_exclusion(self):
        rows=self.rows()
        rows[0]["source_path"]="other.sqlite"
        with self.assertRaisesRegex(ValueError,"exactly one"):
            subtract_dark(rows,mode="separate",dark_run_id="dark",phase_confirmed=True)
        rows=self.rows()
        rows[0]["_manual_excluded"]=True
        values=subtract_dark(rows,mode="separate",dark_run_id="dark",phase_confirmed=True)
        self.assertAlmostEqual(values[0]["measured.lockin_xy_h1_delta_x_v"],.02)

    def test_checkout_module_entry_beats_foreign_pythonpath(self):
        root=Path(__file__).resolve().parents[1]
        code="import attodry_control, attodry_control.combination_cli as c; print(c.__file__); assert attodry_control.__version__"
        result=subprocess.run([sys.executable,"-s","-B","-c",code],cwd=root,
            env={**os.environ,"PYTHONPATH":str(root/"tests")},capture_output=True,text=True,check=True)
        self.assertIn(str(root/"src"/"attodry_control"),result.stdout)


if __name__=="__main__":unittest.main()
