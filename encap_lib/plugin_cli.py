"""Plugin command dispatch, isolated from Encap's existing machine execution paths."""

import copy
import json
import os
import re
import shlex
import shutil
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from filelock import FileLock

from encap_lib import autopull, encap_settings as settings
from encap_lib.status import print_cloud_status
from encap_lib.run_preparation import (
    resolve_dependencies, saved_source_files, stage_source, stage_workspace, write_run_configuration,
)
from encap_lib.backends import (
    BackendError, ConfigurationError, MonitoringError, PluginConfig, RecordWriter,
    RunHandle, RunRequest, RunStatus, exit_code, validate_pull_exclusions, wait,
)

MANAGEMENT = frozenset(("wait", "tail", "pull", "status", "kill", "watch", "ssh"))


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


def _save_monitoring(writer, handle, expected, **changes):
    with writer.lock:
        if writer.read()["handle"] != expected:
            raise MonitoringError("Run changed while monitoring; reconnect to the saved run with status, wait or tail.")
        saved = asdict(handle)
        writer.update(handle=saved, **changes)
    return saved


def _auto_watch(destination, registration, writer):
    if not registration.supports_autopull or writer.read()["handle"] is None:
        return
    try:
        state = autopull.watch(destination, registration, automatic=True)
    except (BackendError, OSError) as error:
        writer.update(autopull_error=str(error))
        print(
            f"Warning: submission is saved, but automatic pulls could not start: {error}. "
            "Use encap watch with the same target and run name to retry.",
            file=sys.stderr,
        )
        return
    writer.update(autopull_error=None)
    if state["enabled"]:
        print(f"Automatic pulls registered (every {state['interval_seconds']}s; daemon PID {state['pid']}).")
    else:
        print("Automatic pulls are paused. Use encap daemon start to enable them.")


def _missing_handle_message(destination):
    return (
        "Submission is unconfirmed and no recovery handle was saved. "
        "Encap cannot reconcile it automatically.\n"
        "Check the cloud provider's job/workflow history. Only after confirming that no submission "
        f"was accepted, move {shlex.quote(str(destination))} to an unused backup path, preserving "
        "its .encap-run metadata, then rerun your original encap run command. "
        "Do not resubmit while cloud acceptance is uncertain."
    )


def _rerun_configuration(destination, source_dir, record, overrides):
    effective = settings.read_config_file(str(destination / ".encap_history.conf"))
    settings.merge_dicts(effective, settings.read_config_file(str(destination / ".encap.conf")))
    settings.merge_dicts(effective, overrides)
    dependencies = effective.get("dependencies", [])
    configured_dependencies = list(dependencies)
    preparation = record.get("preparation", {})
    if dependencies == preparation.get("configured_dependencies"):
        effective["dependencies"] = preparation["dependencies"]
    elif any(not Path(value).is_absolute() and not value.startswith("{root}/") for value in dependencies):
        for path in reversed(settings.find_encap_config_files(str(source_dir))):
            declared = settings.read_config_file(path).get("dependencies")
            if declared == dependencies:
                effective["dependencies"] = declared
                break
        else:
            raise ConfigurationError(
                "Cannot recover relative dependency origins for this saved run. "
                "Use absolute paths or {root}/ paths in its .encap.conf."
            )
    return effective, configured_dependencies


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
        if len(selected_options) != 1 or pargs.mode not in ("run", "rerun"):
            raise ConfigurationError("Plugin options require run or rerun and exactly one backend")
        selected_name, _ = selected_options[0]
        if pargs.backend is not None and pargs.backend != selected_name:
            raise ConfigurationError("Conflicting backend and plugin options")
        pargs.backend = selected_name
    destination = run_destination(pargs.target, pargs.name) if pargs.target and pargs.name else None
    writer = RecordWriter(destination / ".encap-run") if destination else None
    replacement = None
    rerun = None
    if writer is not None and writer.directory.exists() and pargs.mode == "run":
        record = writer.read()
        _check_options(pargs, {})
        if record["state"] == "not-submitted" and record["handle"] is None:
            replacement = record
            replacement_state = "not submitted"
        else:
            if record["handle"] is None:
                raise MonitoringError(_missing_handle_message(destination))
            if record["state"] == "submission-unconfirmed":
                command = shlex.join(["encap", "status", pargs.target, "-n", pargs.name])
                raise MonitoringError(
                    "Cannot overwrite an unconfirmed submission. Inspect and reconcile it with:\n"
                    f"  {command}\n"
                    "Keep the .encap-run recovery metadata. Do not resubmit while cloud acceptance is uncertain."
                )
            previous = _registration(record["backend"], registrations).factory(PluginConfig())
            expected = copy.deepcopy(record["handle"])
            handle = RunHandle(**record["handle"])
            try:
                status = previous.status(handle)
                if not isinstance(status, RunStatus):
                    raise MonitoringError("Backend returned an invalid status")
                _save_monitoring(writer, handle, expected, computation=asdict(status), monitoring_error=None)
            except BackendError as error:
                writer.update(monitoring_error=str(error))
                raise
            if status.state not in ("succeeded", "failed", "cancelled"):
                raise MonitoringError(f"Cannot overwrite a {status.state} run. Wait for confirmed completion first.")
            replacement = writer.read()
            replacement_state = status.state
    if writer is not None and writer.directory.exists() and pargs.mode == "rerun":
        rerun = writer.read()
        _check_options(pargs, {})
        registration = _registration(rerun["backend"], registrations)
        if not registration.supports_rerun:
            raise ConfigurationError(f"Backend {registration.name} does not support rerun")
        if pargs.backend is not None and pargs.backend != rerun["backend"]:
            raise ConfigurationError("Rerun must use the saved run's backend")
        if rerun["handle"] is None:
            raise MonitoringError("Run has no submission handle; use run to retry its original submission.")
        if not all((destination / path).is_file() for path in (".encap.conf", ".encap_history.conf")):
            raise ConfigurationError("Rerun requires the saved experiment's .encap.conf and .encap_history.conf")
    if writer is not None and writer.directory.exists() and pargs.mode not in ("run", "rerun"):
        record = writer.read()
        _check_options(pargs, {})
        if pargs.mode not in MANAGEMENT:
            raise ConfigurationError(f"{pargs.mode} is not supported for plugin runs")
        if pargs.backend is not None:
            raise ConfigurationError("Saved runs select their backend from the record; omit backend selectors")
        registration = _registration(record["backend"], registrations)
        if pargs.mode == "ssh" and not registration.supports_ssh:
            raise ConfigurationError(f"Backend {registration.name} does not support ssh")
        if record["handle"] is None:
            if record["state"] == "not-submitted":
                raise MonitoringError(
                    "Run was not submitted. Rerun your original encap run command with the same options. "
                    "Answer y to the local overwrite prompt and, if shown, the cloud deletion prompt. "
                    "To keep old results, choose a different run name with -n."
                )
            raise MonitoringError(_missing_handle_message(destination))
        if pargs.mode == "watch":
            source = Path(pargs.target).expanduser().absolute()
            source_dir = (
                source if destination == source.parent / "0encap_folder" / source.name / pargs.name else source.parent
            )
            state = autopull.watch(destination, registration, source_dir=source_dir)
            print(f"Watching {destination}; automatic pulls every {state['interval_seconds']}s, PID {state['pid']}.")
            return True
        namespace = PluginConfig()
        if getattr(pargs, "rsync_exclude_pull", None) is not None:
            namespace.rsync_exclude_pull = validate_pull_exclusions(pargs.rsync_exclude_pull)
        backend = registration.factory(namespace)
        expected = copy.deepcopy(record["handle"])
        handle = RunHandle(**record["handle"])
        try:
            if pargs.mode == "wait":
                def save_status(status):
                    nonlocal expected
                    expected = _save_monitoring(
                        writer, handle, expected, computation=asdict(status), monitoring_error=None,
                    )
                status = wait(backend, handle, on_status=save_status)
                print(json.dumps(status.details, sort_keys=True))
                if status.cleanup_pending:
                    print("Computation finished; infrastructure cleanup is still pending.")
                raise SystemExit(exit_code(status))
            if pargs.mode == "status":
                status = backend.status(handle)
                expected = _save_monitoring(
                    writer, handle, expected, computation=asdict(status), monitoring_error=None,
                )
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
            elif pargs.mode == "ssh":
                backend.ssh(handle, instance=pargs.instance, tunnel_through_iap=pargs.tunnel_through_iap)
            _save_monitoring(writer, handle, expected)
        except BackendError as error:
            writer.update(monitoring_error=str(error))
            raise
        return True

    if pargs.mode == "ssh":
        raise ConfigurationError("ssh requires a saved plugin run; no .encap-run record was found for this target and name")

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
    listing = pargs.mode == "status" and pargs.name is None
    if rerun is not None:
        source = Path(pargs.target).expanduser().absolute()
        source_dir = (
            Path(rerun["preparation"]["source_dir"]) if "preparation" in rerun else
            source if destination == source.parent / "0encap_folder" / source.name / pargs.name else source.parent
        )
        effective, configured_dependencies = _rerun_configuration(destination, source_dir, rerun, overrides)
        settings.config = copy.deepcopy(effective)
    elif pargs.target or listing:
        source = Path(pargs.target or os.getcwd()).expanduser().absolute()
        # Use Encap's loader and hierarchy; project-scoped global defaults precede local files.
        settings.config = effective
        settings.load_encap_config_files_recursive(str(source if source.is_dir() else source.parent), overrides=overrides)
        effective = copy.deepcopy(settings.config)
    else:
        settings.merge_dicts(effective, overrides)
    if rerun is None:
        configured_dependencies = list(effective.get("dependencies", []))
    name = (
        rerun["backend"] if rerun is not None else
        pargs.backend if pargs.backend is not None else
        effective.get("backend", replacement["backend"] if replacement else None)
    )
    if listing:
        if name is None and pargs.vm is None and not settings.using_slurm and not settings.using_pueue:
            candidates = [
                candidate for candidate, registration in registrations.items()
                if registration.list_runs is not None and candidate in effective.get("plugins", {})
            ]
            if len(candidates) > 1:
                raise ConfigurationError("Multiple listing backends are configured; select one with --backend")
            if candidates:
                name = candidates[0]
        if name is not None:
            registration = _registration(name, registrations)
            _check_options(pargs, {})
            if registration.list_runs is None:
                raise ConfigurationError(f"Backend {name} does not support cloud listing; specify a saved run with -n")
            namespace = effective.get("plugins", {}).get(name, PluginConfig())
            if not isinstance(namespace, PluginConfig):
                namespace = PluginConfig(namespace)
            statuses = registration.list_runs(namespace)
            if not isinstance(statuses, list) or any(not isinstance(status, RunStatus) for status in statuses):
                raise MonitoringError("Backend returned an invalid run listing")
            if getattr(pargs, "status_json", False):
                print(json.dumps({"backend": name, "runs": [asdict(status) for status in statuses]}, indent=2, sort_keys=True))
            else:
                print_cloud_status(name, statuses, namespace)
            return True
    if name is None:
        if pargs.mode == "watch":
            raise ConfigurationError("watch requires a saved plugin run with automatic-pull support")
        if getattr(pargs, "status_json", False):
            raise ConfigurationError("--json requires a plugin backend")
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
    if pargs.mode != "run" and rerun is None:
        raise ConfigurationError(f"{pargs.mode} requires a saved plugin run; only run creates one")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", pargs.name):
        raise ConfigurationError("Plugin run names must contain only letters, digits, dots, hyphens and underscores")
    if rerun is None and not source.exists():
        raise ConfigurationError(f"Source does not exist: {source}")
    if rerun is None:
        source_dir = source if source.is_dir() else source.parent
    experiment_dir = destination if rerun is not None else source_dir
    try:
        entrypoint = (
            get_script_name(str(experiment_dir), effective.get("script_name", "run.*"))
            if rerun is not None or source.is_dir() else source.name
        )
    except AssertionError as error:
        raise ConfigurationError(str(error)) from error
    entry = experiment_dir / entrypoint
    if not entry.is_file() or entry.is_symlink() or not entry.resolve().is_relative_to(experiment_dir.resolve()):
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
        if rerun is not None:
            if writer.read() != rerun:
                raise BackendError("Run record changed while preparing rerun. Retry after checking its status.")
        else:
            writer = RecordWriter.reserve(destination, registration, pargs.name)
        try:
            if rerun is None:
                stage_source(source, destination if source.is_dir() else destination / source.name, excluded)
            if not (destination / entrypoint).is_file():
                raise ConfigurationError("Entrypoint is excluded from run preparation")
            effective["backend"] = name
            effective["script_name"] = entrypoint
            run_overrides = copy.deepcopy(overrides)
            run_overrides.update(backend=name, script_name=entrypoint)
            write_run_configuration(destination, effective, run_overrides)
            workspace, experiment_path = stage_workspace(
                source_dir, destination, dependencies, excluded,
                force=rerun is not None and registration.supports_dependencies,
            )
            workspace_origin = source_dir.resolve()
            for _ in Path(experiment_path).parts:
                workspace_origin = workspace_origin.parent
            request = RunRequest(destination, entrypoint, arguments, dict(environment), pargs.name, writer.directory,
                                 effective.get("interpreter"),
                                 workspace_folder=workspace, experiment_path=experiment_path,
                                 workspace_origin=workspace_origin,
                                 source_path=source,
                                 workspace_dependencies=tuple(
                                     path.relative_to(workspace_origin).as_posix() for path in dependencies
                                 ))
            protected = (
                rerun.get("preparation", {}).get("source_files") if rerun is not None
                else saved_source_files(destination, destination)
            )
            preparation = {
                "source_dir": str(source_dir),
                "configured_dependencies": configured_dependencies,
                "dependencies": [str(path) for path in dependencies],
            }
            if protected is not None:
                preparation["source_files"] = protected
            writer.update(preparation=preparation)
            if rerun is not None:
                handle = backend.rerun(request, RunHandle(**rerun["handle"]), writer)
            else:
                handle = backend.submit(request, writer)
            writer.save_handle(handle, confirmed=True)
        except (BackendError, OSError, KeyboardInterrupt) as error:
            writer.update(submission_error=f"{type(error).__name__}: {error}")
            raise
        finally:
            _auto_watch(destination, registration, writer)
    print(f"Submitted {pargs.name} through {name}. Record: {writer.path}")
    print("Following logs; outputs will be pulled on completion. Ctrl-C detaches without cancelling execution.")
    expected = asdict(handle)
    try:
        _tail_pull(backend, handle, destination)
        _save_monitoring(writer, handle, expected, monitoring_error=None)
    except BackendError as error:
        writer.update(monitoring_error=str(error))
        raise
    return True
