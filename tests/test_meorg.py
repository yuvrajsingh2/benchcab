"""`pytest` tests for `utils/meorg.py`."""

import re
from pathlib import Path

import hpcpy.constants
import pytest
from hpcpy.client.pbs import PBSClient

import benchcab.utils.meorg as bm
from benchcab.config import read_optional_key
from benchcab.utils import load_package_data

FLUXSITE_JOB_ID = "100.gadi-pbs"


class InitialisedClient:
    """Stand-in for a `meorg_client` with credentials in `~/.meorg`."""

    def is_initialised(self):
        """Report the client as initialised."""
        return True


@pytest.fixture()
def meorg_bin(mock_cwd):
    """Return the path of an empty `meorg` executable."""
    path = mock_cwd / "bin" / "meorg"
    path.parent.mkdir()
    path.touch()
    return path


@pytest.fixture()
def config(meorg_bin):
    """Return a config with defaults, as `read_config` gives it."""
    return read_optional_key(
        {
            "project": "tm70",
            "modules": [],
            "realisations": [],
            "fluxsite": {"experiment": "AU-Tum"},
            "meorg_output_name": "123-my-branch_A1B2C3",
            "meorg_bin": str(meorg_bin),
        }
    )


@pytest.fixture()
def submissions(monkeypatch, mock_cwd):
    """Mock `qsub` and record each submission as (command, rendered script)."""
    monkeypatch.setenv("USER", "abc123")
    monkeypatch.setattr(hpcpy.constants, "JOB_SCRIPT_DIR", mock_cwd / "job_scripts")
    monkeypatch.setattr(bm, "MeorgClient", InitialisedClient)
    monkeypatch.setattr(bm, "get_client", PBSClient)
    recorded = []

    def qsub(self, cmd, env=None):
        script = Path(cmd.split()[-1]).read_text()
        recorded.append((cmd, script))
        return f"{len(recorded)}.gadi-pbs"

    monkeypatch.setattr(PBSClient, "_shell", qsub)
    return recorded


def meorg_commands(script):
    """Return the `meorg` subcommands a job script runs, in order."""
    return re.findall(r"\$MEORG_BIN ([\w-]+ [\w-]+)", script)


def test_worker_path_unchanged(config, submissions, meorg_bin):
    """With `enabled: false`, one job uploads and starts the worker analysis."""
    assert bm.do_meorg(config, "runs/fluxsite/outputs", "", FLUXSITE_JOB_ID)
    [(cmd, script)] = submissions
    assert f"-W depend=afterok:{FLUXSITE_JOB_ID}" in cmd
    script = script.replace(str(meorg_bin), "/opt/benchcab/bin/meorg")
    assert script == load_package_data("test/meorg_jobscript_worker.sh")
    assert meorg_commands(script) == [
        "output query",
        "file delete_all",
        "output create",
        "experiment update",
        "file upload",
        "benchmark update",
        "analysis start",
    ]
