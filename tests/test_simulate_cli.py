from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from attodry_control.simulate import run
from attodry_control.storage import RunMonitor


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SimulationCliTests(unittest.TestCase):
    def test_cli_runs_complete_audited_retry_without_hardware(self) -> None:
        temporary = tempfile.NamedTemporaryFile(
            dir=PROJECT_ROOT, suffix=".sqlite", delete=False
        )
        temporary.close()
        path = Path(temporary.name)
        self.addCleanup(self._remove_database, path)
        output = io.StringIO()
        with redirect_stdout(output):
            code = run(
                [
                    "--database",
                    str(path),
                    "--run-id",
                    "cli-simulation",
                    "--inject-first-unlock",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("accepted_conditions=1", output.getvalue())
        self.assertIn("rejected_attempts=1", output.getvalue())
        with RunMonitor(path) as monitor:
            summary = monitor.summary("cli-simulation")
        self.assertEqual(summary.status, "complete")
        self.assertEqual(summary.total_attempts, 2)

    @staticmethod
    def _remove_database(path: Path) -> None:
        for suffix in ("", "-shm", "-wal"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                candidate.unlink()


if __name__ == "__main__":
    unittest.main()
