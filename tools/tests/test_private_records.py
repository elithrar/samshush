import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import private_records  # noqa: E402


class PrivateRecordTests(unittest.TestCase):
    def test_only_complete_private_records_become_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operation.json"
            started = {"completed": False}
            finished = {"completed": True}
            private_records.write_json(path, started)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                private_records.write_json(path, finished)
            with self.assertRaises(TypeError):
                private_records.write_json(path, {"result": object()}, replace=True)
            with (
                patch.object(private_records.os, "fsync", side_effect=OSError("full")),
                self.assertRaises(OSError),
            ):
                private_records.write_json(path, finished, replace=True)
            self.assertEqual(json.loads(path.read_text()), started)
            self.assertEqual(list(path.parent.iterdir()), [path])
            private_records.write_json(path, finished, replace=True)
            self.assertEqual(json.loads(path.read_text()), finished)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
