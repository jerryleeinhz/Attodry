from contextlib import redirect_stdout
from datetime import UTC, datetime
import io
import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.monitor import run
from attodry_control.storage import RunStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MonitorCliTests(unittest.TestCase):
    def test_monitor_cli_returns_json_summary(self) -> None:
        temporary = tempfile.NamedTemporaryFile(
            dir=PROJECT_ROOT, suffix=".sqlite", delete=False
        )
        temporary.close()
        path = Path(temporary.name)
        self.addCleanup(self._remove_database, path)
        with RunStore(path) as store:
            store.create_run(
                "run-001", {"mode": "simulation"}, created_at_utc=datetime.now(UTC)
            )

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = run(["--database", str(path), "--run-id", "run-001"])

        self.assertEqual(exit_code, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["run_id"], "run-001")
        self.assertEqual(summary["total_conditions"], 0)

    @staticmethod
    def _remove_database(path: Path) -> None:
        for suffix in ("", "-shm", "-wal"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                candidate.unlink()


if __name__ == "__main__":
    unittest.main()
