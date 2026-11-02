"""Plugin command dispatch, isolated from Encap's existing machine execution paths."""

import copy
import json
import os
import re
import shlex
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path
from filelock import FileLock

from encap_lib import encap_settings as settings
from encap_lib.run_preparation import resolve_dependencies, stage_source, stage_workspace, write_run_configuration
from encap_lib.backends import (
    BackendError, ConfigurationError, MonitoringError, PluginConfig, RecordWriter,
    RunHandle, RunRequest, RunStatus, exit_code, validate_pull_exclusions, wait,
)

MANAGEMENT = frozenset(("wait", "tail", "pull", "status", "kill"))


def run_destination(target, name):
    source = Path(target).expanduser().absolute()
    candidates = (source.parent / "0encap_folder" / source.name / str(name), source.with_suffix("") / str(name))
    saved = [path for path in candidates if (path / ".encap-run").exists()]
    if len(saved) > 1:
        raise BackendError("Both file and folder plugin records exist for this target; use an unambiguous target")
    if saved:
        return saved[0]
    if source.is_dir():
        return candidates[0]
    return candidates[1]


def _check_options(pargs, effective):
    conflicting = []
    for key, value in vars(pargs).items():
        if key in ("vm", "i", "slurm", "pueue", "copy_name") or key.startswith(("sl_", "pu_")):
            if value is not None and value is not False:
                conflicting.append(key)
    if effective.get("i", 1) != 1 or settings.using_slurm or settings.using_pueue:
        conflicting.append("multiple instances/scheduler")
    if pargs.force_push or pargs.dryrun:
        conflicting.append("force-push/dryrun")
    if conflicting:
        raise ConfigurationError("Plugin runs do not support: " + ", ".join(conflicting))


def _registration(name, registrations):
    if not isinstance(name, str) or name not in registrations:
        raise ConfigurationError(
            f"Backend {name!r} is not installed. Install its plugin in the same Python environment as Encap."
        )
    return registrations[name]


def _tail_pull(backend, handle, destination):
    backend.tail(handle)
    print(f"Pulling outputs to {destination}", flush=True)
    backend.pull(handle, destination)


def dispatch(pargs, registrations, get_script_name):
    """Return True when handled. Saved plugin runs never consult current provider settings."""
    selected_options = []
    for name, registration in registrations.items():
        values = {
            option.key: getattr(pargs, f"plugin_{name}_{option.key}")
            for option in registration.cli_options
            if getattr(pargs, f"plugin_{name}_{option.key}", None) is not None
        }
        if values:
            selected_options.append((name, values))
    if selected_options:
        if len(selected_options) != 1 or pargs.mode != "run":
            raise ConfigurationError("Plugin options require run and exactly one backend")
        selected_name, _ = selected_options[0]
        if pargs.backend is not None and pargs.backend != selected_name:
            raise ConfigurationError("Conflicting backend and plugin options")
        pargs.backend = selected_name
    destination = run_destination(pargs.target, pargs.name) if pargs.target and pargs.name else None
    writer = RecordWriter(destination / ".encap-run") if destination else None
    replacement = None
    if writer is not None and writer.directory.exists() and pargs.mode == "run":
        record = writer.read()
        _check_options(pargs, {})
        if record["state"] == "not-submitted" and record["handle"] is None:
            replacement = record
            replacement_state = "not submitted"
        else:
            if record["state"] == "submission-unconfirmed" or record["handle"] is None:
                raise MonitoringError("Cannot overwrite an unconfirmed submission. Reconcile it before submitting again.")
            previous = _registration(record["backend"], registrations).factory(PluginConfig())
            handle = RunHandle(**record["handle"])
            try:
                status = previous.status(handle)
                if not isinstance(status, RunStatus):
                    raise MonitoringError("Backend returned an invalid status")
                writer.update(handle=asdict(handle), computation=asdict(status), monitoring_error=None)
            except BackendError as error:
                writer.update(monitoring_error=str(error))
                raise
            if status.state not in ("succeeded", "failed", "cancelled"):
                raise MonitoringError(f"Cannot overwrite a {status.state} run. Wait for confirmed completion first.")
            replacement = writer.read()
            replacement_state = status.state
    if writer is not None and writer.directory.exists() and pargs.mode != "run":
        record = writer.read()
        _check_options(pargs, {})
        if pargs.mode not in MANAGEMENT:
            raise ConfigurationError(f"{pargs.mode} is not supported for plugin runs")
        if pargs.backend is not None:
            raise ConfigurationError("Saved runs select their backend from the record; omit backend selectors")
        registration = _registration(record["backend"], registrations)
        if record["handle"] is None:
            if record["state"] == "not-submitted":
                raise MonitoringError("Run was not submitted; use run to retry.")
            raise MonitoringError("Submission is unconfirmed and no recovery handle was saved. Do not resubmit this run.")
        namespace = PluginConfig()
        if getattr(pargs, "rsync_exclude_pull", None) is not None:
            namespace.rsync_exclude_pull = validate_pull_exclusions(pargs.rsync_exclude_pull)
        backend = registration.factory(namespace)
        handle = RunHandle(**record["handle"])
        try:
            if pargs.mode == "wait":
                def save_status(status):
                    writer.update(handle=asdict(handle), computation=asdict(status), monitoring_error=None)
                status = wait(backend, handle, on_status=save_status)
                print(json.dumps(status.details, sort_keys=True))
                if status.cleanup_pending:
                    print("Computation finished; infrastructure cleanup is still pending.")
                raise SystemExit(exit_code(status))
            if pargs.mode == "status":
                status = backend.status(handle)
                writer.update(handle=asdict(handle), computation=asdict(status), monitoring_error=None)
                result = {
                    "state": status.state, "exit_code": status.exit_code, "details": status.details,
                    "outputs_finalized": status.outputs_finalized, "cleanup_pending": status.cleanup_pending,
                }
                retrieval = writer.read_retrieval()
                if retrieval is not None:
                    result["retrieval"] = retrieval
                print(json.dumps(result, indent=2))
                if status.state == "unknown":
                    raise MonitoringError(f"Remote state is unknown: {status.details}")
            elif pargs.mode == "tail":
                _tail_pull(backend, handle, destination)
            elif pargs.mode == "pull":
                backend.pull(handle, destination)
            elif pargs.mode == "kill":
                backend.cancel(handle)
                print("Cancellation requested; remote cleanup remains provider-managed.")
            writer.update(handle=asdict(handle))
        except BackendError as error:
            writer.update(monitoring_error=str(error))
            raise
        return True

    effective = copy.deepcopy(pargs.base_config)
    overrides = copy.deepcopy(settings.args_config)
    if selected_options:
        selected_name, values = selected_options[0]
        settings.merge_dicts(overrides, {
            "plugins": {selected_name: PluginConfig(values, os.getcwd())},
        })
    if pargs.i is None:
        overrides.pop("i", None)
    project = effective.get("projects", {}).get(os.getcwd(), {})
    if not isinstance(project, dict):
        raise ConfigurationError("Project configuration must be a mapping")
    for key in ("backend", "plugins", "backup_on_overwrite", "dependencies", "workspace_root",
                "rsync_exclude_pull"):
        if key in project:
            value = {key: project[key]}
            settings.merge_dicts(effective, value)
    if pargs.target:
        source = Path(pargs.target).expanduser().absolute()
        # Use Encap's loader and hierarchy; project-scoped global defaults precede local files.
        settings.config = effective
        settings.load_encap_config_files_recursive(str(source if source.is_dir() else source.parent), overrides=overrides)
        effective = copy.deepcopy(settings.config)
    else:
        settings.merge_dicts(effective, overrides)
    name = pargs.backend if pargs.backend is not None else effective.get("backend", replacement["backend"] if replacement else None)
    if name is None:
        if effective.get("dependencies"):
            raise ConfigurationError("dependencies require a supporting execution backend, such as Dataproc")
        settings.merge_dicts(settings.config, copy.deepcopy(settings.args_config))
        return False
    registration = _registration(name, registrations)
    _check_options(pargs, effective)
    if "backend_instances" in effective:
        raise ConfigurationError(
            "backend_instances is no longer supported; configure instances in the selected plugin's namespace"
        )
    backup_on_overwrite = effective.get("backup_on_overwrite", False)
    if type(backup_on_overwrite) is not bool:
        raise ConfigurationError("backup_on_overwrite must be true or false")
    if pargs.mode != "run":
        raise ConfigurationError(f"{pargs.mode} requires a saved plugin run; only run creates one")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", pargs.name):
        raise ConfigurationError("Plugin run names must contain only letters, digits, dots, hyphens and underscores")
    if not source.exists():
        raise ConfigurationError(f"Source does not exist: {source}")
    source_dir = source if source.is_dir() else source.parent
    try:
        entrypoint = get_script_name(str(source_dir), effective.get("script_name", "run.*")) if source.is_dir() else source.name
    except AssertionError as error:
        raise ConfigurationError(str(error)) from error
    entry = source_dir / entrypoint
    if not entry.is_file() or entry.is_symlink() or not entry.resolve().is_relative_to(source_dir.resolve()):
        raise ConfigurationError(f"Entrypoint must be a regular file within the source directory: {entrypoint}")
    arguments = effective.get("args", "")
    if not isinstance(arguments, str):
        raise ConfigurationError("args must be a shell-quoted argument string")
    try:
        arguments = shlex.split(arguments)
    except ValueError as error:
        raise ConfigurationError(f"Invalid experiment arguments: {error}") from error
    environment = effective.get("environment", {})
    if (not isinstance(environment, dict) or
            any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
                or not isinstance(v, str) or "\x00" in v for k, v in environment.items())):
        raise ConfigurationError("environment must map valid variable names to string values without NUL bytes")
    if any("\x00" in argument for argument in arguments):
        raise ConfigurationError("Arguments must not contain NUL bytes")
    excluded = []
    for key in ("rsync_exclude", "rsync_exclude_push"):
        values = effective.get(key, [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ConfigurationError(f"{key} must be a list of patterns")
        excluded.extend(values)
    workspace_root = (
        settings.resolve_workspace_root(effective["workspace_root"]) if "workspace_root" in effective else None
    )
    dependencies = resolve_dependencies(
        effective.get("dependencies", []), source_dir, excluded, workspace_root=workspace_root,
    )
    if dependencies and not registration.supports_dependencies:
        raise ConfigurationError(f"Backend {name} does not support dependencies")
    namespace = effective.get("plugins", {}).get(name, PluginConfig())
    if not isinstance(namespace, PluginConfig):
        namespace = PluginConfig(namespace)
    namespace.workspace_root = workspace_root
    namespace.rsync_exclude_pull = validate_pull_exclusions(effective.get("rsync_exclude_pull", []))
    backend = registration.factory(namespace)
    if replacement is not None and not pargs.yes:
        action = (
            "Overwrite it and preserve the old run in a backup?"
            if backup_on_overwrite else "Overwrite it? The previous local run will be deleted."
        )
        answer = input(
            f"The experiment {destination} already exists ({replacement_state}). "
            f"{action} y/n: "
        )
        if answer.strip().lower() != "y":
            print("Run unchanged; no new computation submitted.")
            return True
    destination.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(destination.parent / f".{destination.name}.submission.lock")):
        if replacement is not None:
            if writer.read() != replacement:
                raise BackendError("Run record changed while preparing overwrite. Retry after checking its status.")
            if backup_on_overwrite:
                backup = destination.with_name(f"{destination.name}.backup-{uuid.uuid4().hex[:12]}")
                destination.rename(backup)
                print(f"Previous run preserved at: {backup}")
            else:
                shutil.rmtree(destination)
        writer = RecordWriter.reserve(destination, registration, pargs.name)
    try:
        stage_source(source, destination if source.is_dir() else destination / source.name, excluded)
        if not (destination / entrypoint).is_file():
            raise ConfigurationError("Entrypoint is excluded from run preparation")
        effective["backend"] = name
        effective["script_name"] = entrypoint
        run_overrides = copy.deepcopy(overrides)
        run_overrides.update(backend=name, script_name=entrypoint)
        write_run_configuration(destination, effective, run_overrides)
        workspace, experiment_path = stage_workspace(source_dir, destination, dependencies, excluded)
        workspace_origin = source_dir.resolve()
        for _ in Path(experiment_path).parts:
            workspace_origin = workspace_origin.parent
        request = RunRequest(destination, entrypoint, arguments, dict(environment), pargs.name, writer.directory,
                             effective.get("interpreter"),
                             workspace_folder=workspace, experiment_path=experiment_path,
                             workspace_origin=workspace_origin,
                             workspace_dependencies=tuple(
                                 path.relative_to(workspace_origin).as_posix() for path in dependencies
                             ))
        handle = backend.submit(request, writer)
        writer.save_handle(handle, confirmed=True)
    except (BackendError, OSError, KeyboardInterrupt) as error:
        writer.update(submission_error=f"{type(error).__name__}: {error}")
        raise
    print(f"Submitted {pargs.name} through {name}. Record: {writer.path}")
    print("Following logs; outputs will be pulled on completion. Ctrl-C detaches without cancelling execution.")
    try:
        _tail_pull(backend, handle, destination)
        writer.update(handle=asdict(handle), monitoring_error=None)
    except BackendError as error:
        writer.update(monitoring_error=str(error))
        raise
    return True
