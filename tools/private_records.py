"""Publish complete, owner-readable JSON records on the local filesystem."""

import json
import os
import tempfile
from pathlib import Path


def sync_directory(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_json(path, record, *, replace=False):
    """Leave the previous record intact if serialization or file sync fails."""
    descriptor, name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(record, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    sync_directory(path.parent)
