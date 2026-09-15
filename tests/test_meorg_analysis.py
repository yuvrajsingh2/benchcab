"""`pytest` tests for `utils/meorg_analysis.py`."""

from pathlib import Path

import pytest

from benchcab.internal import MEORG_EXPERIMENT_ID_MAP
from benchcab.utils.meorg_analysis import MEORG_BENCHMARK_NAMES, build_analysis_input

MET_FILES = {
    "AU-Tum": "AU-Tum_2002-2017_OzFlux_Met.nc",
    "AU-How": "AU-How_2003-2017_OzFlux_Met.nc",
}
BENCHMARK_FILES = {
    "1lin": "Emp1lin_{site}.nc",
    "3km27": "3km27_{site}_robust_output.nc",
    "LSTM": "{site}_lstm_output.nc",
}
OUTPUT_DIR = Path("/scratch/tm70/u/bcex/runs/fluxsite/outputs")


class MockTask:
    """Minimal stand-in for `fluxsite.FluxsiteTask`."""

    def __init__(self, met_forcing_file, model_id, sci_conf_id):
        """Store the fields `build_analysis_input` reads."""
        self.met_forcing_file = met_forcing_file
        self.model_id = model_id
        self.sci_conf_id = sci_conf_id

    def get_output_filename(self):
        """Return the task output file name."""
        stem = self.met_forcing_file.split(".")[0]
        return f"{stem}_R{self.model_id}_S{self.sci_conf_id}_out.nc"


def make_tasks(site_ids, n_models=2, n_sci=2):
    """Return the cross product of models, sites and science configurations."""
    return [
        MockTask(MET_FILES[site_id], model_id, sci_conf_id)
        for model_id in range(n_models)
        for site_id in site_ids
        for sci_conf_id in range(n_sci)
    ]


@pytest.fixture()
def cache_root(tmp_path):
    """Build the staged input cache layout with empty NetCDF file names."""
    for site_id, met_file in MET_FILES.items():
        site_dir = tmp_path / "datasets" / site_id
        site_dir.mkdir(parents=True)
        (site_dir / met_file).touch()
        (site_dir / met_file.replace("_Met.nc", "_Flux.nc")).touch()
        for name, pattern in BENCHMARK_FILES.items():
            bench_dir = tmp_path / "benchmarks" / name
            bench_dir.mkdir(parents=True, exist_ok=True)
            (bench_dir / pattern.format(site=site_id)).touch()
    return tmp_path


@pytest.fixture()
def au_tum(cache_root):
    """Return the payload for AU-Tum, 2 realisations and 2 science configurations."""
    return build_analysis_input(
        config={
            "project": "tm70",
            "fluxsite": {"experiment": "AU-Tum"},
            "meorg_output_name": "123-my-branch-a1b2c3",
        },
        tasks=make_tasks(["AU-Tum"]),
        cache_root=cache_root,
        run_id="run-1",
        output_dir=OUTPUT_DIR,
    )


def counts(payload):
    """Return a map of (type, number) to file count."""
    result = {}
    for entry in payload["files"]:
        result[entry["type"], entry["number"]] = (
            result.get((entry["type"], entry["number"]), 0) + 1
        )
    return result


def test_role_counts_and_envelope(au_tum):
    """One site gives 4 bundle files, 1 DataSet unit, and 3 benchmarks twice over."""
    assert counts(au_tum) == {
        ("ModelOutput", 1): 4,
        ("DataSet", 1): 2,
        ("Benchmark", 1): 1,
        ("Benchmark", 2): 1,
        ("Benchmark", 3): 1,
        ("ModelOutput", 2): 1,
        ("ModelOutput", 3): 1,
        ("ModelOutput", 4): 1,
    }
    assert au_tum["_id"] == "run-1"
    assert au_tum["files"][0]["setId"] == "benchcab:run-1"
    assert au_tum["config"] == {
        "schema_version": 1,
        "analysis": {},
        "runtime": {
            "parallel_multisite_plots": True,
            "load_workers": None,
            "single_site_plot_workers": None,
            "multisite_workers": None,
        },
    }


def test_bundle_is_first_and_every_path_is_netcdf(au_tum):
    """The bundle is listed first, and every path is absolute and NetCDF."""
    assert [e["filename"] for e in au_tum["files"][:4]] == [
        "AU-Tum_2002-2017_OzFlux_Met_R0_S0_out.nc",
        "AU-Tum_2002-2017_OzFlux_Met_R0_S1_out.nc",
        "AU-Tum_2002-2017_OzFlux_Met_R1_S0_out.nc",
        "AU-Tum_2002-2017_OzFlux_Met_R1_S1_out.nc",
    ]
    assert all(e["name"] == "123-my-branch-a1b2c3" for e in au_tum["files"][:4])
    assert all(
        Path(e["path"]).is_absolute() and ".nc" in e["path"] for e in au_tum["files"]
    )


def test_benchmark_ids_and_duplicate_model_output_role(au_tum):
    """Benchmark units carry the ME.org ids in order, and repeat as ModelOutputs."""
    ids = MEORG_EXPERIMENT_ID_MAP["AU-Tum"]["benchmarks"]
    for number, (name, set_id) in enumerate(zip(MEORG_BENCHMARK_NAMES, ids), 1):
        unit = [e for e in au_tum["files"] if e["type"] == "Benchmark"]
        unit = [e for e in unit if e["number"] == number]
        dupe = [e for e in au_tum["files"] if e["type"] == "ModelOutput"]
        dupe = [e for e in dupe if e["number"] == number + 1]
        assert [(e["name"], e["setId"]) for e in unit] == [(name, set_id)]
        assert [e["path"] for e in dupe] == [e["path"] for e in unit]


def test_two_sites_give_two_dataset_units(cache_root):
    """Each site is its own DataSet unit, numbered from 1."""
    payload = build_analysis_input(
        config={"project": "tm70", "fluxsite": {"experiment": "five-site-test"}},
        tasks=make_tasks(["AU-Tum", "AU-How"]),
        cache_root=cache_root,
        run_id="run-2",
    )
    datasets = [e for e in payload["files"] if e["type"] == "DataSet"]
    assert sorted({e["number"] for e in datasets}) == [1, 2]
    assert {e["name"] for e in datasets} == {"AU-Tum", "AU-How"}
    assert len(datasets) == len(MET_FILES) * 2
    benchmarks = [e for e in payload["files"] if e["type"] == "Benchmark"]
    assert len(benchmarks) == len(MEORG_BENCHMARK_NAMES) * len(MET_FILES)
