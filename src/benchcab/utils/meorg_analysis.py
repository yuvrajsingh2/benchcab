"""Build and submit an r-meorg analysis job for a fluxsite run."""

from pathlib import Path

from hpcpy import get_client

import benchcab.utils as bu
from benchcab import __version__, internal
from benchcab.internal import MEORG_EXPERIMENT_ID_MAP

# Benchmark unit labels, in the order of the ids in MEORG_EXPERIMENT_ID_MAP.
MEORG_BENCHMARK_NAMES = ("1lin", "3km27", "LSTM")


def _entry(file_type: str, number: int, name: str, set_id: str, path: Path) -> dict:
    """Return one `input.json` `files[]` entry."""
    return {
        "type": file_type,
        "number": number,
        "name": name,
        "setId": set_id,
        "setModified": None,
        "mimetype": "application/x-netcdf",
        "filename": path.name,
        "path": str(path),
    }


def _site_id(met_forcing_file: str) -> str:
    """Return the FLUXNET site id for a met forcing file name."""
    return met_forcing_file.split("_", maxsplit=1)[0]


def build_analysis_input(
    config: dict,
    tasks: list,
    cache_root: Path,
    run_id: str,
    *,
    output_dir: Path = None,
    fluxsite_job_id: str = None,
) -> dict:
    """Build the r-meorg `input.json` payload for a fluxsite run.

    Parameters
    ----------
    config : dict
        The master config dictionary.
    tasks : list
        Fluxsite tasks, each with `met_forcing_file` and `get_output_filename()`.
    cache_root : Path
        Holds `datasets/<site id>/*.nc` and `benchmarks/<benchmark name>/*.nc`.
    run_id : str
        Stable identifier for this analysis run.
    output_dir : Path, optional
        Fluxsite output directory, by default the one under the current directory.
    fluxsite_job_id : str, optional
        PBS job id of the fluxsite job, recorded as provenance.

    Returns
    -------
    dict
        The analysis input payload, ready to serialise as `input.json`.

    """
    cache_root = Path(cache_root)
    output_dir = Path(
        output_dir if output_dir is not None else internal.FLUXSITE_DIRS["OUTPUT"]
    ).absolute()
    experiment = config["fluxsite"]["experiment"]

    # 1. The fluxsite bundle is ModelOutput unit 1 and is listed first.
    files = [
        _entry(
            "ModelOutput",
            1,
            config.get("meorg_output_name") or run_id,
            f"benchcab:{run_id}",
            output_dir / task.get_output_filename(),
        )
        for task in tasks
    ]

    # 2. One DataSet unit per site, holding the Met and Flux files.
    site_ids = list(dict.fromkeys(_site_id(task.met_forcing_file) for task in tasks))
    for number, site_id in enumerate(site_ids, start=1):
        for path in sorted((cache_root / "datasets" / site_id).glob("*.nc")):
            files.append(
                _entry("DataSet", number, site_id, f"benchcab:{site_id}", path)
            )

    # 3. Benchmark units 1..3, and the same files again as ModelOutput 2..4.
    benchmark_ids = MEORG_EXPERIMENT_ID_MAP[experiment]["benchmarks"]
    for index, (name, set_id) in enumerate(zip(MEORG_BENCHMARK_NAMES, benchmark_ids)):
        paths = [
            path
            for site_id in site_ids
            for path in sorted(
                (cache_root / "benchmarks" / name).glob(f"*{site_id}*.nc")
            )
        ]
        files += [_entry("Benchmark", index + 1, name, set_id, p) for p in paths]
        files += [_entry("ModelOutput", index + 2, name, set_id, p) for p in paths]

    return {
        "_id": run_id,
        "provenance": {
            "orchestrator": "benchcab",
            "benchcabVersion": __version__,
            "experiment": experiment,
            "meorgExperimentId": MEORG_EXPERIMENT_ID_MAP[experiment]["experiment"],
            "schedulerJobId": fluxsite_job_id,
            "workDir": str(Path.cwd()),
        },
        "files": files,
        "config": {
            "schema_version": 1,
            "analysis": {},
            "runtime": {"parallel_multisite_plots": True},
        },
    }


def submit_analysis(
    config: dict,
    input_path: Path,
    run_dir: Path,
    depends_on: str = None,
    dry_run: bool = False,
):
    """Submit the r-meorg analysis job through hpcpy.

    Parameters
    ----------
    config : dict
        The master config dictionary.
    input_path : Path
        Absolute path to the written `input.json`.
    run_dir : Path
        Absolute path to the run directory the runner writes into.
    depends_on : str, optional
        Job id this job waits on with `afterok`, by default None.
    dry_run : bool, optional
        Render the job script and return the qsub command, by default False.

    Returns
    -------
    hpcpy.job.Job or str
        The submitted job, or the qsub command when `dry_run` is True.

    """
    settings = config["fluxsite"]["meorg_analysis"]
    return get_client().submit(
        bu.get_installed_root() / "data" / "meorg_analysis_jobscript.j2",
        render=True,
        dry_run=dry_run,
        depends_on=depends_on,
        queue="normal",
        walltime=settings["walltime"],
        storage=settings["storage"],
        project=config["project"],
        ncpus=settings["ncpus"],
        mem=settings["mem"],
        # Prefixed to avoid the `module_use` and `modules` arguments that newer
        # hpcpy versions consume before the template context.
        meorg_module_use=settings["module_use"],
        meorg_module=settings["module"],
        runner=settings["runner"],
        input_json=str(input_path),
        run_dir=str(run_dir),
    )
