"""File-only external Python exceptions; every executable/script is a fake file."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from attodry_control import combination_ownership as ownership
from attodry_control.external_owner_policy import ExternalOwnerPolicy, _windows_argv


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ExternalOwnerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.app = self.directory / "laser folder"
        self.app.mkdir()
        self.interpreter = self.app / "python.exe"
        self.script = self.app / "laser.py"
        self.interpreter.write_bytes(b"fake interpreter; never executed")
        self.script.write_bytes(b"# fake laser; never executed\n")
        self.command = subprocess.list2cmdline([str(self.interpreter), "-u", str(self.script), "--power", "1"])
        self.entry = {"command": self.command,
                      "interpreter_path": str(self.interpreter), "interpreter_sha256": sha(self.interpreter),
                      "script_path": str(self.script), "script_sha256": sha(self.script)}
        self.record = {"version": 1, "allowed_processes": [self.entry]}
        self.path = self.directory / "approved-external-owner.json"
        self.save()
        self.own = {"pid": os.getpid(), "parent": 10, "name": "python.exe",
                    "command": "python -m attodry_control.combination_cli resume --run-id offline",
                    "created": "2026-10-14T15:01:33.8541390Z"}
        self.laser = {"pid": 61000, "parent": 1000, "name": "python.exe", "command": self.command,
                      "created": "2026-10-14T18:00:01.1234567Z", "executable": str(self.interpreter)}

    def save(self, record=None, *, raw=None):
        self.path.write_text(json.dumps(self.record if record is None else record)
                             if raw is None else raw, encoding="utf-8")
        self.policy_sha = sha(self.path)

    def policy(self, **kwargs):
        return ExternalOwnerPolicy(self.path, self.policy_sha, **kwargs)

    def validate(self, policy, rows):
        return ownership.validate_inventory(policy.filter_inventory(rows), own_pid=os.getpid(), idle_kernels=set())

    def config(self, *, with_lockin=False):
        path = self.directory / "hardware.local.toml"
        path.write_text("# offline ownership fixture\n", encoding="utf-8")
        snapshot = {"config_sha256": sha(path), "source_sha256": {
            p.name: sha(p) for p in Path(ownership.__file__).parent.glob("*.py")}}
        if with_lockin:
            safety = path.with_name("lockin_safety.toml")
            safety.write_text("# fake safety\n", encoding="utf-8")
            snapshot["hardware"] = {"lockin": {"safety_sha256": sha(safety)}}
        return SimpleNamespace(path=path, lockin=object() if with_lockin else None,
                               cryostat=None, smu=None, snapshot=snapshot)

    def test_exact_named_application_is_allowed_with_full_identity_and_pinned_artifacts(self):
        audit = Mock()
        policy = self.policy(audit_sink=audit)
        evidence = self.validate(policy, [self.own, self.laser])
        self.assertEqual(evidence, [self.own])
        approved = policy.current_approvals
        self.assertEqual(approved[0]["process"], self.laser)
        self.assertEqual(approved[0]["policy_sha256"], self.policy_sha)
        self.assertEqual(approved[0]["script_sha256"], sha(self.script))
        self.assertEqual(audit.call_count, 1)
        self.validate(policy, [self.own, self.laser])
        self.assertEqual(audit.call_count, 1)
        approved[0]["process"]["command"] = "mutated returned copy"
        self.assertEqual(policy.current_approvals[0]["process"]["command"], self.command)

    def test_absence_and_exact_command_restart_are_allowed_and_each_instance_is_recorded(self):
        policy = self.policy()
        self.assertEqual(self.validate(policy, [self.own]), [self.own])
        self.assertEqual(policy.current_approvals, [])
        self.validate(policy, [self.own, self.laser])
        second = {**self.laser, "pid": 61001, "created": "2026-10-14T18:00:02Z"}
        self.validate(policy, [self.own, second])
        # A reused PID also needs a genuinely new creation identity.
        third = {**second, "created": "2026-10-14T18:00:03Z"}
        self.validate(policy, [self.own, third])
        self.assertEqual(len(policy.observations), 3)
        self.assertEqual(policy.current_approvals[0]["process"], third)
        self.validate(policy, [self.own])
        self.assertEqual(policy.current_approvals, [])
        self.assertEqual(len(policy.observations), 3)

    def test_inventory_adapter_works_with_original_constructor_without_optional_policy(self):
        rows = [self.own, self.laser]
        policy = self.policy()
        owner = ownership.RecoveryOwner(self.config(), inventory=policy.inventory_adapter(lambda: deepcopy(rows)),
                                        idle_reader=lambda values: set())
        owner.locked = True
        self.assertEqual(owner.check()["pid"], os.getpid())
        self.assertEqual(policy.current_approvals[0]["process"], self.laser)
        rows.append({**self.laser, "pid": 61002, "command": self.command + " --unknown"})
        with self.assertRaisesRegex(ValueError, "unknown Python owner"):
            owner.check()

    def test_optional_constructor_integration_retains_evidence_and_all_original_hash_guards(self):
        rows = [self.own, self.laser]
        config = self.config(with_lockin=True)
        owner = ownership.RecoveryOwner(config, inventory=lambda: deepcopy(rows),
                                        idle_reader=lambda values: set(), external_policy=self.policy())
        owner.locked = True
        evidence = owner.check()
        self.assertEqual(evidence["approved_external_owners"][0]["process"], self.laser)
        self.assertEqual(evidence["external_owner_policy_sha256"], self.policy_sha)
        rows[-1] = {**self.laser, "pid": 61003, "created": "2026-10-14T18:00:04Z"}
        owner.check()
        config.snapshot["source_sha256"] = {}
        with self.assertRaisesRegex(ValueError, "Source changed"):
            owner.check()
        config.snapshot["source_sha256"] = {p.name: sha(p) for p in Path(ownership.__file__).parent.glob("*.py")}
        config.path.with_name("lockin_safety.toml").write_text("# changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Safety configuration changed"):
            owner.check()
        config.path.write_text("# changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Configuration changed"):
            owner.check()

    def test_optional_policy_does_not_allow_untyped_bypass_objects(self):
        with self.assertRaisesRegex(ValueError, "file-pinned policy"):
            ownership.RecoveryOwner(self.config(), external_policy=SimpleNamespace(allow=True))

    def test_unapproved_command_variants_are_kept_for_original_unknown_owner_guard(self):
        cases = (self.command + " --extra", self.command.replace("--power 1", "--power 2"),
                 self.command.replace(str(self.script), str(self.app / "other.py")),
                 'python.exe -u "laser.py"', "python -", "python -c pass")
        for command in cases:
            with self.subTest(command=command):
                policy = self.policy()
                with self.assertRaisesRegex(ValueError, "unknown Python owner"):
                    self.validate(policy, [self.own, {**self.laser, "command": command}])
                self.assertEqual(policy.current_approvals, [])

    def test_same_process_instance_cannot_change_command_parent_name_or_executable(self):
        cases = ({"command": self.command + " --changed"}, {"parent": 2000},
                 {"name": "pythonw.exe"}, {"executable": str(self.app / "other-python.exe")})
        for change in cases:
            with self.subTest(change=change):
                policy = self.policy()
                self.validate(policy, [self.own, self.laser])
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    self.validate(policy, [self.own, {**self.laser, **change}])

    def test_actual_executable_creation_parent_and_process_name_are_required(self):
        cases = ({"executable": None}, {"executable": str(self.app / "other-python.exe")},
                 {"executable": "python.exe"}, {"created": None}, {"created": "yesterday"},
                 {"created": "2026-10-14T18:00:00"}, {"created": "2026-10-14T18:00:00+03:00"},
                 {"parent": None}, {"parent": True}, {"name": "pythonw.exe"})
        for change in cases:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(self.policy(), [self.own, {**self.laser, **change}])

    def test_malformed_inventory_is_fail_closed(self):
        cases = (None, {}, [None], [self.own, {**self.laser, "pid": True}],
                 [self.own, {**self.laser, "pid": -1}], [self.own, self.own],
                 [self.own, {**self.laser, "created": []}],
                 [self.own, {**self.laser, "command": {}}])
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.policy().filter_inventory(rows)

    def test_native_pid_zero_idle_ancestor_is_preserved_without_external_approval(self):
        idle = {"pid": 0, "parent": 0, "name": "System Idle Process", "command": None,
                "executable": None, "created": "2026-10-13T15:33:48.1334140Z"}
        policy = self.policy()
        rows = [idle, self.own, self.laser]
        self.assertEqual(policy.filter_inventory(rows), [idle, self.own])
        evidence = ownership.validate_inventory(policy.filter_inventory(rows), own_pid=os.getpid(), idle_kernels=set())
        self.assertEqual(evidence, [self.own])
        self.assertEqual([entry["process"]["pid"] for entry in policy.current_approvals], [self.laser["pid"]])

    def test_pid_zero_cannot_hide_python_duplicate_unknown_or_nonstandard_idle_rows(self):
        idle = {"pid": 0, "parent": 0, "name": "System Idle Process", "command": None,
                "executable": None, "created": "2026-10-14T00:00:00Z"}
        rows = ([idle, idle, self.own], [self.own, {**self.laser, "pid": 0}],
                *([self.own, {key: value for key, value in idle.items() if key != missing}]
                  for missing in ("created", "command", "executable")),
                *([self.own, {**idle, **change}] for change in (
                    {"name": "Unknown Idle Process"}, {"name": "python.exe"}, {"parent": 1},
                    {"parent": False}, {"command": self.command}, {"command": ""},
                    {"executable": str(self.interpreter)}, {"executable": ""}, {"created": None})))
        for values in rows:
            with self.subTest(rows=values), self.assertRaises(ValueError):
                self.policy().filter_inventory(values)

    def test_policy_interpreter_and_script_bytes_are_rechecked_even_when_application_absent(self):
        for target in (self.path, self.interpreter, self.script):
            with self.subTest(target=target):
                original = target.read_bytes()
                policy = self.policy()
                target.write_bytes(original + b" ")
                with self.assertRaisesRegex(ValueError, "SHA256 changed"):
                    policy.filter_inventory([self.own])
                target.write_bytes(original)
                policy = self.policy()
                target.unlink()
                with self.assertRaises(ValueError):
                    policy.filter_inventory([self.own])
                target.write_bytes(original)

    def test_policy_digest_cannot_be_mismatched_or_noncanonical(self):
        for value in ("0" * 64, self.policy_sha.upper(), "*", None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ExternalOwnerPolicy(self.path, value)

    def test_policy_schema_unknown_fields_duplicate_keys_and_invalid_numbers_are_rejected(self):
        values = ({**self.record, "unexpected": 1}, {**self.record, "version": True},
                  {**self.record, "version": 1.0}, {**self.record, "version": 2},
                  {**self.record, "allowed_processes": []},
                  {**self.record, "allowed_processes": [self.entry, self.entry]},
                  {**self.record, "allowed_processes": [{**self.entry, "allow_directory": True}]},
                  {**self.record, "allowed_processes": [{k: v for k, v in self.entry.items() if k != "script_sha256"}]},
                  {**self.record, "allowed_processes": [{**self.entry, "script_sha256": float("nan")}]})
        for value in values:
            with self.subTest(value=value):
                self.save(value)
                with self.assertRaises(ValueError):
                    self.policy()
        for raw in ('{"version":1,"version":1,"allowed_processes":[]}',
                    '{"version":NaN,"allowed_processes":[]}',
                    '{"version":Infinity,"allowed_processes":[]}',
                    '{"version":-Infinity,"allowed_processes":[]}', "[]", "not json"):
            with self.subTest(raw=raw):
                self.save(raw=raw)
                with self.assertRaises(ValueError):
                    self.policy()

    def test_relative_paths_stdin_inline_modules_and_unbound_scripts_cannot_be_approved(self):
        interpreter, script = str(self.interpreter), str(self.script)
        commands = ([interpreter, "-"], [interpreter, "-c", "pass"],
                    [interpreter, "-m", "laser"], [interpreter, "-X", "dev", script],
                    [interpreter, "-u", "laser.py"], ["python.exe", script],
                    [interpreter, str(self.app / "other.py")])
        entries = [{**self.entry, "command": subprocess.list2cmdline(argv)} for argv in commands]
        entries.extend({**self.entry, key: value} for key, value in (
            ("script_path", "laser.py"), ("script_path", "C:laser.py"),
            ("script_path", "\\laser.py"), ("script_path", str(self.app / "laser.txt")),
            ("interpreter_path", "python.exe"), ("script_sha256", True),
            ("command", '"unbalanced'), ("command", self.command + "\n")))
        for entry in entries:
            with self.subTest(entry=entry):
                self.save({"version": 1, "allowed_processes": [entry]})
                with self.assertRaises(ValueError):
                    self.policy()

    def test_policy_cannot_suppress_controller_analysis_gui_or_monitor_guards(self):
        policy = self.policy()
        # Matching raw command cannot remove the controller or a GUI/Jupyter name.
        for row in ({**self.laser, "pid": os.getpid()},
                    {**self.laser, "name": "attoDRY.exe"},
                    {**self.laser, "name": "jupyter.exe"}):
            with self.subTest(row=row):
                self.assertEqual(policy.filter_inventory([row]), [row])
        with self.assertRaisesRegex(ValueError, "Instrument GUI"):
            self.validate(policy, [self.own, {**self.laser, "name": "attoDRY.exe"}])
        for label in ("jupyter", "ipykernel_launcher", "attodry_control"):
            self.save({"version": 1, "allowed_processes": [
                {**self.entry, "command": self.command + " --label " + label}]})
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "controller or analysis"):
                self.policy()

    def test_known_analysis_identity_is_still_checked_with_external_policy(self):
        kernel = {"pid": 62000, "parent": 1000, "name": "python.exe",
                  "command": "python -m ipykernel_launcher -f C:/runtime/kernel-offline.json",
                  "created": "2026-10-14T18:00:00Z"}
        rows = [self.own, self.laser, kernel]
        owner = ownership.RecoveryOwner(self.config(), inventory=lambda: deepcopy(rows),
                                        idle_reader=lambda values: {"offline"}, external_policy=self.policy())
        owner.locked = True
        owner.check()
        rows[-1]["created"] = "2026-10-14T18:00:01Z"
        with self.assertRaisesRegex(ValueError, "Analysis owner identities changed"):
            owner.check()

    def test_audit_failure_blocks_exception_and_does_not_commit_approval(self):
        policy = self.policy(audit_sink=Mock(side_effect=OSError("offline disk full")))
        with self.assertRaisesRegex(OSError, "disk full"):
            policy.filter_inventory([self.own, self.laser])
        self.assertEqual(policy.current_approvals, [])
        self.assertEqual(policy.observations, [])

    def test_windows_command_parser_preserves_quoted_paths_and_fixed_argument_bytes(self):
        for argv in ([str(self.interpreter), "-u", str(self.script), "--label", "a b"],
                     [str(self.interpreter), str(self.script), 'embedded"quote', "trailing\\", ""],
                     [str(self.interpreter), str(self.script), "\\\\host\\path", 'back\\\\"quote']):
            with self.subTest(argv=argv):
                self.assertEqual(_windows_argv(subprocess.list2cmdline(argv)), argv)

    def test_exact_separate_utf8_option_pair_and_fixed_named_script_command_are_allowed(self):
        command = subprocess.list2cmdline([str(self.interpreter), "-s", "-B", "-X", "utf8",
            str(self.script), "--manifest", str(self.app / "diagnostic-manifest.json"),
            "--config", str(self.app / "optical.local.toml"), "--authorize-connection"])
        self.save({"version": 1, "allowed_processes": [{**self.entry, "command": command}]})
        policy = self.policy()
        actual = {**self.laser, "command": command}
        self.assertEqual(self.validate(policy, [self.own, actual]), [self.own])
        self.assertEqual(policy.current_approvals[0]["process"], actual)
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.validate(policy, [self.own, {**actual, "command": command + " --extra"}])

    def test_other_x_options_and_inline_utf8_spelling_remain_rejected(self):
        for options in (["-X", "dev"], ["-X", "presite=untrusted"], ["-X", "utf8=0"],
                        ["-Xutf8"], ["-X", "UTF8"], ["-X"]):
            with self.subTest(options=options):
                command = subprocess.list2cmdline([str(self.interpreter), *options, str(self.script)])
                self.save({"version": 1, "allowed_processes": [{**self.entry, "command": command}]})
                with self.assertRaisesRegex(ValueError, "unknown Python options"):
                    self.policy()

    def test_process_inventory_requests_actual_os_executable_path(self):
        completed = SimpleNamespace(stdout=json.dumps([self.own]))
        with patch.object(ownership.sys, "platform", "win32"), \
                patch.object(ownership.subprocess, "run", return_value=completed) as run:
            self.assertEqual(ownership.process_inventory(), [self.own])
        self.assertIn("executable=$_.ExecutablePath", run.call_args.args[0][-1])


if __name__ == "__main__":
    unittest.main()
