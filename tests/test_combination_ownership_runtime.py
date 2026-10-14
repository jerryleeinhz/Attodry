"""Offline Jupyter launcher/runtime ownership graphs; no real server or mutex."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from attodry_control import combination_ownership as ownership


SERVER = 3256
WRAPPER = 12836
LAUNCHER = 12840
KERNELS = ((18204, "kernel-aaa-111"), (18300, "kernel-bbb-222"))


def process(pid, parent, name, command):
    return {"pid": pid, "parent": parent, "name": name, "command": command,
            "created": "2026-10-14T09:01:03.9189030Z"}


def process_tree():
    return [
        process(os.getpid(), 0, "python.exe",
                "python -m attodry_control.combination_cli resume --run-id offline"),
        process(WRAPPER, 1000, "jupyter.exe", '"C:/env/Scripts/jupyter.exe" lab'),
        process(LAUNCHER, WRAPPER, "python.exe",
                '"C:/env/python.exe" "C:/env/Scripts/jupyter.exe" lab'),
        process(SERVER, LAUNCHER, "python.exe",
                '"C:/env/python.exe" "C:/env/Scripts/jupyter-lab.exe"'),
        *(process(pid, SERVER, "python.exe",
                  f'"C:/env/python.exe" -m ipykernel_launcher -f C:/runtime/{identity}.json')
          for pid, identity in KERNELS),
    ]


class JupyterOwnershipRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.runtime = self.directory / "jupyter" / "runtime"
        self.runtime.mkdir(parents=True)
        self.rows = process_tree()
        self.runtime_record = {"pid": SERVER, "url": "http://127.0.0.1:8888/",
                               "token": "offline-token-never-persisted"}
        self.write_runtime()
        self.kernels = [{"id": identity.removeprefix("kernel-"), "execution_state": "idle"}
                        for _, identity in KERNELS]
        self.requests = []

    def write_runtime(self, record=None, *, pid=SERVER):
        (self.runtime / f"jpserver-{pid}.json").write_text(
            json.dumps(self.runtime_record if record is None else record), encoding="utf-8")

    def response(self, request, timeout):
        self.assertEqual(timeout, 5)
        self.requests.append(request)
        return io.StringIO(json.dumps(self.kernels))

    def idle(self, rows=None):
        with patch.dict(os.environ, {"APPDATA": str(self.directory)}), \
                patch.object(ownership, "urlopen", side_effect=self.response):
            return ownership._idle_jupyter(self.rows if rows is None else rows)

    def config(self):
        path = self.directory / "hardware.local.toml"
        path.write_text("# offline ownership fixture\n", encoding="utf-8")
        return SimpleNamespace(path=path, lockin=None, cryostat=None, smu=None,
            snapshot={"config_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in Path(ownership.__file__).parent.glob("*.py")}})

    def test_only_actual_server_runtime_is_read_wrappers_and_two_idle_kernels_are_allowed(self):
        idle = self.idle()
        self.assertEqual(idle, {"aaa-111", "bbb-222"})
        self.assertEqual(idle.allowed_server_ids, {SERVER, WRAPPER, LAUNCHER})
        evidence = ownership.validate_inventory(self.rows, own_pid=os.getpid(), idle_kernels=idle)
        self.assertEqual({row["pid"] for row in evidence}, {row["pid"] for row in self.rows})
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].full_url, "http://127.0.0.1:8888/api/kernels")
        self.assertEqual(self.requests[0].get_header("Authorization"),
                         "token offline-token-never-persisted")
        self.assertNotIn(self.runtime_record["token"], json.dumps(evidence))
        self.assertFalse((self.runtime / f"jpserver-{WRAPPER}.json").exists())
        self.assertFalse((self.runtime / f"jpserver-{LAUNCHER}.json").exists())

    def test_busy_kernel_blocks_initial_handoff(self):
        self.kernels[1]["execution_state"] = "busy"
        with self.assertRaisesRegex(ValueError, "idle"):
            self.idle()

    def test_local_authentication_disables_environment_proxy_and_refuses_redirects(self):
        request = ownership.Request("http://127.0.0.1:8888/api/kernels",
                                    headers={"Authorization": "token offline-token"})
        opener = Mock()
        expected = object()
        opener.open.return_value = expected
        with patch.dict(os.environ, {"HTTP_PROXY": "http://example.invalid:8080",
                                     "HTTPS_PROXY": "http://example.invalid:8080"}), \
                patch.object(ownership, "build_opener", return_value=opener) as build:
            self.assertIs(ownership.urlopen(request, timeout=5), expected)
        proxy, redirect = build.call_args.args
        self.assertIsInstance(proxy, ownership.ProxyHandler)
        self.assertEqual(proxy.proxies, {})
        self.assertIsInstance(redirect, ownership._NoRedirect)
        opener.open.assert_called_once_with(request, timeout=5)
        for destination in ("http://example.invalid/collect", "http://127.0.0.1:8888/login"):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(ValueError, "refuses HTTP redirects"):
                    redirect.redirect_request(request, None, 302, "Found", {}, destination)

    def test_orphan_wrapper_and_unknown_jupyter_executable_are_blocked(self):
        for row in (
            process(60000, 1000, "jupyter.exe", "C:/env/Scripts/jupyter.exe lab"),
            process(60000, 1000, "jupyter-instrument.exe", "C:/unknown/jupyter-instrument.exe"),
        ):
            with self.subTest(command=row["command"]):
                with self.assertRaisesRegex(ValueError, "Unknown Jupyter"):
                    self.idle([*self.rows, row])

    def test_unknown_python_is_not_a_server_merely_because_it_has_an_ancestor(self):
        row = process(60000, SERVER, "python.exe", "python unsafe_worker.py")
        rows = [*self.rows, row]
        idle = self.idle(rows)
        with self.assertRaisesRegex(ValueError, "unknown Python owner"):
            ownership.validate_inventory(rows, own_pid=os.getpid(), idle_kernels=idle)

    def test_runtime_process_mismatch_and_nonlocal_or_nonhttp_urls_are_blocked_before_rest(self):
        cases = ({**self.runtime_record, "pid": SERVER + 1},
                 {**self.runtime_record, "url": "http://example.invalid:8888/"},
                 {**self.runtime_record, "url": "https://127.0.0.1:8888/"})
        for record in cases:
            with self.subTest(record={k: v for k, v in record.items() if k != "token"}):
                self.write_runtime(record)
                with self.assertRaises(ValueError):
                    self.idle()
        self.assertEqual(self.requests, [])

    def test_missing_server_runtime_does_not_turn_launchers_into_owners(self):
        (self.runtime / f"jpserver-{SERVER}.json").unlink()
        with self.assertRaisesRegex(ValueError, "without a verified idle process tree"):
            self.idle()
        self.assertEqual(self.requests, [])

    def test_malformed_or_unknown_kernel_state_cannot_be_treated_as_idle(self):
        for kernels in ({"id": "aaa-111"}, [{"id": "aaa-111"}],
                        [{"id": "aaa-111", "execution_state": "starting"}]):
            with self.subTest(kernels=kernels):
                self.kernels = kernels
                with self.assertRaisesRegex(ValueError, "idle"):
                    self.idle()

    def test_plain_idle_kernel_ids_do_not_authorize_unverified_servers(self):
        with self.assertRaisesRegex(ValueError, "unknown Python owner"):
            ownership.validate_inventory(self.rows, own_pid=os.getpid(),
                                         idle_kernels={"aaa-111", "bbb-222"})

    def test_handoff_reads_idle_once_then_preserves_analysis_identity_while_plotting(self):
        inventory = Mock(side_effect=lambda: deepcopy(self.rows))
        idle_reader = Mock(side_effect=lambda rows: self.idle(rows))
        owner = ownership.RecoveryOwner(self.config(), inventory=inventory, idle_reader=idle_reader)
        # Supply a mock already-held lease; this test never calls kernel32.
        owner.locked = True
        first = owner.check()
        self.kernels[0]["execution_state"] = "busy"  # Same known analysis may plot after handoff.
        self.assertEqual(owner.check(), first)
        self.assertEqual(idle_reader.call_count, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(self.runtime_record["token"], json.dumps(owner.last_evidence))
        monitor = process(60000, 1000, "python.exe",
                          "python -m attodry_control.combination_cli monitor --run-id offline")
        self.rows.append(monitor)
        self.assertEqual(owner.check()["pid"], os.getpid())
        self.assertEqual(idle_reader.call_count, 1)

    def test_analysis_pid_creation_command_and_new_kernel_changes_are_blocked_after_handoff(self):
        for mutate in (
            lambda rows: rows[-1].update(created="2026-10-14T12:00:00Z"),
            lambda rows: rows[-1].update(command=rows[-1]["command"] + " --changed"),
            lambda rows: rows.pop(),
            lambda rows: rows.append(process(60000, SERVER, "python.exe",
                "python -m ipykernel_launcher -f C:/runtime/kernel-aaa-111.json")),
        ):
            with self.subTest(mutation=mutate):
                rows = deepcopy(self.rows)
                owner = ownership.RecoveryOwner(self.config(), inventory=lambda: rows,
                    idle_reader=lambda values: self.idle(values))
                owner.locked = True
                owner.check()
                mutate(rows)
                with self.assertRaisesRegex(ValueError, "identities changed"):
                    owner.check()


if __name__ == "__main__":
    unittest.main()
