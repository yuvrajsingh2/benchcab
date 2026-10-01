"""Utility methods for interacting with the ME.org client."""

import os
from datetime import datetime, timezone
from pathlib import Path

from hpcpy import get_client
from meorg_client.client import Client as MeorgClient
from meorg_client.utilities import is_dev_mode


import benchcab.utils as bu
from benchcab import internal
from benchcab.internal import MEORG_CLIENT, MEORG_PROFILE, MEORG_EXPERIMENT_ID_MAP
from benchcab.utils import interpolate_file_template
from benchcab.utils.fs import mkdir


def do_meorg(
    config: dict,
    upload_dir: str,
    benchcab_bin: str,
    benchcab_job_id: str = None,
    gadi: bool = None,
    dry_run: bool = False,
):
    """Perform the upload of model outputs to modelevaluation.org.

    Parameters
    ----------
    config : dict
        The master config dictionary
    upload_dir : str
        Absolute path to the data dir for upload
    benchcab_bin : str
        Path to the benchcab bin, from which to infer the client bin
    benchcab_job_id : str, optional
        Job id the first job waits on with `afterok`, by default None
    gadi : bool, optional
        Run the analysis on Gadi (jobs B, R and U) instead of on the me.org
        worker, by default `fluxsite.meorg_analysis.enabled`
    dry_run : bool, optional
        Render the Gadi job scripts without submitting them, by default False

    Returns
    -------
    bool
        True if successful, False otherwise

    """
    logger = bu.get_logger()

    meorg_output_name = config.get("meorg_output_name")
    num_threads = MEORG_CLIENT["num_threads"]
    settings = config["fluxsite"]["meorg_analysis"]
    if gadi is None:
        gadi = settings["enabled"]

    # Check if a model output id has been assigned
    if meorg_output_name is None and not (gadi and settings.get("model_output_id")):
        logger.info("No meorg_output_name resolved in configuration.")
        logger.info("NOT uploading to modelevaluation.org")
        return False

    # Allow the user to specify an absolute path to the meorg bin in config
    meorg_bin = config.get("meorg_bin", False)

    # Otherwise infer the path from the benchcab installation
    if meorg_bin == False:
        logger.debug(f"Inferring meorg bin from {benchcab_bin}")
        bin_segments = benchcab_bin.split("/")
        bin_segments[-1] = "meorg"
        meorg_bin = "/".join(bin_segments)

    logger.debug(f"meorg_bin = {meorg_bin}")

    # Now, check if that actually exists
    if os.path.isfile(meorg_bin) == False:
        logger.error(f"No meorg_client executable found at {meorg_bin}")
        logger.error("NOT uploading to modelevaluation.org")
        return False

    # Also only run if the client is initialised
    if MeorgClient().is_initialised(dev=is_dev_mode()) == False:

        logger.warn(
            "A meorg_output_name has been supplied, but the meorg_client is not initialised."
        )
        logger.warn(
            "To initialise, run `meorg initialise` in the installation environment."
        )
        logger.warn(
            "Once initialised, the outputs from this run can be uploaded with the following command:"
        )
        logger.warn(
            f"meorg file upload {upload_dir}/*.nc -n {num_threads} --attach_to {meorg_output_name}"
        )
        logger.warn("Then the analysis can be triggered with:")
        logger.warn(f"meorg analysis start {meorg_output_name}")
        return False

    # Finally, attempt the upload!
    else:

        experiment = config["fluxsite"]["experiment"]
        logger.info("Uploading outputs to modelevaluation.org")

        mo = {
            "state_selection": "default",
            "parameter_selection": "automated",
            "is_bundle": True,
            "name": meorg_output_name,
        }
        model_exp_id = MEORG_EXPERIMENT_ID_MAP[experiment]["experiment"]
        model_benchmark_ids = MEORG_EXPERIMENT_ID_MAP[experiment]["benchmarks"]

        # Test-only override, for example for a me.org test server. The
        # benchmarks of that experiment are set on me.org already.
        if gadi and settings.get("experiment_id"):
            model_exp_id = settings["experiment_id"]
            model_benchmark_ids = []

        job_b = dict(
            # Interpolate into the job script
            mo=mo,
            model_prof_id=MEORG_PROFILE["id"],
            model_exp_ids=[model_exp_id],
            model_benchmark_ids=model_benchmark_ids,
            data_dir=upload_dir,
            cache_delay=MEORG_CLIENT["cache_delay"],
            mem=MEORG_CLIENT["mem"],
            num_threads=MEORG_CLIENT["num_threads"],
            walltime=MEORG_CLIENT["walltime"],
            storage=MEORG_CLIENT["storage"],
            project=config["project"],
            modules=config["modules"],
            purge_outputs=True,
            meorg_bin=meorg_bin,
        )

        if gadi:
            jobs = submit_gadi_analysis(config, job_b, benchcab_job_id, dry_run)
            action = "rendered, not submitted" if dry_run else "submitted"
            for name, job in zip("BRU", jobs):
                logger.info(f"Analysis job {name} {action}: {job}")
            return True

        # Submit the outputs
        client = get_client()
        meorg_jobid = client.submit(
            bu.get_installed_root() / "data" / "meorg_jobscript.j2",
            render=True,
            dry_run=False,
            depends_on=benchcab_job_id,
            **job_b,
        )

        logger.info(f"Upload job submitted: {meorg_jobid}")
        return True


def get_run_id(experiment: str) -> str:
    """Return the id of an analysis run, fixed when the jobs are submitted."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"benchcab-{experiment}-{Path.cwd().name}-{timestamp}"


def get_cache(config: dict) -> str:
    """Return the writable cache root for the analysis inputs."""
    user = os.environ.get("USER")
    default = f"/scratch/{config['project']}/{user}/meorg-cache"
    return config["fluxsite"]["meorg_analysis"]["cache"] or default


def submit_gadi_analysis(
    config: dict, job_b: dict, depends_on: str = None, dry_run: bool = False
) -> list:
    """Submit the analysis on Gadi as three jobs chained with `afterok`.

    Job B (copyq) creates the model output record without files and fetches the
    analysis inputs. Job R (normal) runs `meorg-run`. Job U (copyq) submits the
    result, then uploads the model output files.

    Parameters
    ----------
    config : dict
        The master config dictionary
    job_b : dict
        The job script context of the upload job, used for job B
    depends_on : str, optional
        Job id job B waits on, by default None
    dry_run : bool, optional
        Render the job scripts and return the qsub commands, by default False

    Returns
    -------
    list
        The three submitted jobs, or their qsub commands when `dry_run` is True

    """
    settings = config["fluxsite"]["meorg_analysis"]
    run_dir = (internal.FLUXSITE_DIRS["ANALYSIS"] / "meorg").absolute()
    mkdir(run_dir, parents=True, exist_ok=True)

    common = dict(
        project=config["project"],
        run_dir=str(run_dir),
        run_id=get_run_id(config["fluxsite"]["experiment"]),
    )
    # Jobs B and U also read the cache, the outputs and the run directory
    copyq_storage = job_b["storage"] + [
        s for s in settings["storage"] if s not in job_b["storage"]
    ]
    jobs = {
        "meorg_jobscript.j2": dict(
            job_b,
            storage=copyq_storage,
            gadi=True,
            model_output_id=settings.get("model_output_id"),
            cache=get_cache(config),
            cache_ro=settings["cache_ro"],
        ),
        "meorg_analysis_jobscript.j2": dict(
            queue="normal",
            ncpus=settings["ncpus"],
            mem=settings["mem"],
            walltime=settings["walltime"],
            storage=settings["storage"],
            # Prefixed to avoid the `module_use` and `modules` arguments that
            # newer hpcpy versions consume before the template context.
            meorg_module_use=settings["module_use"],
            meorg_module=settings["module"],
        ),
        "meorg_result_jobscript.j2": dict(
            num_threads=job_b["num_threads"],
            mem=job_b["mem"],
            walltime=job_b["walltime"],
            storage=copyq_storage,
            meorg_bin=job_b["meorg_bin"],
            data_dir=job_b["data_dir"],
            experiment_id=job_b["model_exp_ids"][0],
        ),
    }

    client = get_client()
    submitted = []
    for template, context in jobs.items():
        job = client.submit(
            bu.get_installed_root() / "data" / template,
            render=True,
            dry_run=dry_run,
            depends_on=depends_on,
            **(context | common),
        )
        submitted.append(job)
        # A dry run has no job ids, so chain on a placeholder
        depends_on = f"<job {len(submitted)}>" if dry_run else job

    return submitted
