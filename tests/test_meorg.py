"""`pytest` tests for `utils/meorg.py`."""

import re
from pathlib import Path

import hpcpy.constants
import pytest
from hpcpy.client.pbs import PBSClient

import benchcab.utils.meorg as bm
from benchcab import internal
from benchcab.config import read_optional_key
from benchcab.utils import load_package_data

FLUXSITE_JOB_ID = "100.gadi-pbs"
AU_TUM = internal.MEORG_EXPERIMENT_ID_MAP["AU-Tum"]


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


def run_dir():
    """Return the analysis run directory under the current work directory."""
    return Path.cwd() / "runs" / "fluxsite" / "analysis" / "meorg"


def test_defaults():
    """The analysis is off by default, and uses the stable module."""
    defaults = internal.MEORG_ANALYSIS_DEFAULTS
    assert defaults["enabled"] is False
    assert defaults["module_use"] == "/g/data/vk83/modules"
    assert defaults["module"] == "r-meorg/1.0.7_0"
    assert defaults["cache"] is None
    assert defaults["cache_ro"] == []
    assert "model_output_id" not in defaults
    assert "experiment_id" not in defaults


def test_cache_default(config, monkeypatch):
    """The cache defaults to a per-user directory under the project scratch."""
    monkeypatch.setenv("USER", "abc123")
    assert bm.get_cache(config) == "/scratch/tm70/abc123/meorg-cache"
    config["fluxsite"]["meorg_analysis"]["cache"] = "/g/data/tm70/cache"
    assert bm.get_cache(config) == "/g/data/tm70/cache"


def test_run_id(mock_cwd, monkeypatch):
    """The run id names the experiment, the work directory and the UTC time."""
    (mock_cwd / "bench_example").mkdir()
    monkeypatch.chdir(mock_cwd / "bench_example")
    run_id = bm.get_run_id("AU-Tum")
    assert re.fullmatch(r"benchcab-AU-Tum-bench_example-\d{8}T\d{6}Z", run_id)


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


def test_gadi_chain(config, submissions):
    """With `enabled: true`, jobs B, R and U are chained with `afterok`."""
    config["fluxsite"]["meorg_analysis"]["enabled"] = True
    assert bm.do_meorg(config, "runs/fluxsite/outputs", "", FLUXSITE_JOB_ID)
    (cmd_b, job_b), (cmd_r, job_r), (cmd_u, job_u) = submissions
    assert f"-W depend=afterok:{FLUXSITE_JOB_ID}" in cmd_b
    assert "-W depend=afterok:1.gadi-pbs" in cmd_r
    assert "-W depend=afterok:2.gadi-pbs" in cmd_u
    assert "#PBS -q copyq" in job_b
    assert "-q normal" in cmd_r
    assert "#PBS -q normal" in job_r
    assert "#PBS -q copyq" in job_u
    assert run_dir().is_dir()


def test_gadi_job_b(config, submissions):
    """Job B creates the record only, then writes input.json."""
    config["fluxsite"]["meorg_analysis"] |= {"enabled": True, "cache_ro": ["/a", "/b"]}
    bm.do_meorg(config, "runs/fluxsite/outputs", "")
    job_b = submissions[0][1]
    assert meorg_commands(job_b) == [
        "output query",
        "file delete_all",
        "output create",
        "experiment update",
        "benchmark update",
        "analysis input",
    ]
    assert "sleep" not in job_b
    run_id = re.search(r"--run-id (\S+)", job_b).group(1)
    assert run_id.startswith(f"benchcab-AU-Tum-{Path.cwd().name}-")
    assert (
        f"$MEORG_BIN analysis input $MODEL_OUTPUT_ID {AU_TUM['experiment']}"
        f" --run-id {run_id} --cache /scratch/tm70/abc123/meorg-cache"
        f" --cache-ro /a --cache-ro /b -o {run_dir()}/input.json"
        " --model-output-files \"$DATA_DIR/*.nc\""
    ) in job_b
    assert f'echo "$MODEL_OUTPUT_ID" > {run_dir()}/model_output_id' in job_b


def test_gadi_job_r(config, submissions):
    """Job R loads the module and runs meorg-run with the same run id."""
    config["fluxsite"]["meorg_analysis"]["enabled"] = True
    bm.do_meorg(config, "runs/fluxsite/outputs", "")
    run_id = re.search(r"--run-id (\S+)", submissions[0][1]).group(1)
    cmd_r, job_r = submissions[1]
    assert "-l storage=gdata/ks32+gdata/vk83" in cmd_r
    assert "#PBS -l ncpus=12\n#PBS -l mem=48GB\n" in job_r
    assert "module use /g/data/vk83/modules\nmodule load r-meorg/1.0.7_0\n" in job_r
    assert f"export MEORG_MODULE=r-meorg/1.0.7_0 MEORG_RUN_ID={run_id}\n" in job_r
    assert f"meorg-run --input {run_dir()}/input.json --run-dir {run_dir()}" in job_r


def test_gadi_job_u(config, submissions):
    """Job U submits the result, then uploads the model output files."""
    config["fluxsite"]["meorg_analysis"]["enabled"] = True
    bm.do_meorg(config, "runs/fluxsite/outputs", "")
    job_u = submissions[2][1]
    assert f"MODEL_OUTPUT_ID=$(cat {run_dir()}/model_output_id)" in job_u
    assert meorg_commands(job_u) == ["analysis submit-result", "file upload"]
    assert (
        f"$MEORG_BIN analysis submit-result $MODEL_OUTPUT_ID {AU_TUM['experiment']}"
        f" {run_dir()} --orchestrator benchcab"
    ) in job_u
    assert (
        "$MEORG_BIN file upload runs/fluxsite/outputs/*.nc -n 1 $MODEL_OUTPUT_ID"
        in job_u
    )


def test_gadi_overrides(config, submissions):
    """The test-only ids replace the model output lookup and the experiment."""
    del config["meorg_output_name"]
    config["fluxsite"]["meorg_analysis"] |= {
        "model_output_id": "mo123",
        "experiment_id": "exp456",
    }
    bm.do_meorg(config, "runs/fluxsite/outputs", "", gadi=True)
    job_b, job_u = submissions[0][1], submissions[2][1]
    assert meorg_commands(job_b) == [
        "file delete_all",
        "experiment update",
        "analysis input",
    ]
    assert "MODEL_OUTPUT_ID=mo123\n" in job_b
    assert "$MEORG_BIN experiment update $MODEL_OUTPUT_ID exp456\n" in job_b
    assert "analysis input $MODEL_OUTPUT_ID exp456 " in job_b
    assert "submit-result $MODEL_OUTPUT_ID exp456 " in job_u


def test_gadi_needs_an_output_name(config, submissions):
    """Without an output name or a model output id, nothing is submitted."""
    del config["meorg_output_name"]
    assert not bm.do_meorg(config, "runs/fluxsite/outputs", "", gadi=True)
    assert submissions == []


def test_gadi_dry_run(config, submissions, monkeypatch):
    """A dry run renders all three jobs and returns their qsub commands."""
    commands = []
    submit = bm.submit_gadi_analysis

    def record(*args):
        commands.extend(submit(*args))
        return commands

    monkeypatch.setattr(bm, "submit_gadi_analysis", record)
    assert bm.do_meorg(config, "outputs", "", gadi=True, dry_run=True)
    assert submissions == []
    assert [cmd.split()[0] for cmd in commands] == ["qsub"] * 3
    assert "depend" not in commands[0]
    assert "-W depend=afterok:<job 1>" in commands[1]
    assert "-W depend=afterok:<job 2>" in commands[2]
    for cmd in commands:
        assert Path(cmd.split()[-1]).is_file()
