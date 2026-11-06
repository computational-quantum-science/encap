"""Shared configuration preparation, separate from computation execution."""

import copy
import fnmatch
import os
import shutil
from pathlib import Path

from encap_lib import encap_settings as settings
from encap_lib import git_tracker
from encap_lib.backends import ConfigurationError


SOURCE_EXCLUDED = frozenset((
    ".git", ".env", ".venv", "venv", ".output", "runs", "0encap_folder", ".encap-run",
    ".gcp", ".encap-native", "__pycache__", ".pytest_cache", ".ruff_cache", ".DS_Store",
))

def saved_source_files(source_dir, run_folder):
    source_dir, run_folder = Path(source_dir), Path(run_folder)
    if not source_dir.is_dir():
        raise ConfigurationError(f"Original source is unavailable and no saved source manifest exists: {source_dir}")
    saved = []
    for folder, directories, files in os.walk(source_dir):
        directories[:] = sorted(name for name in directories if name not in SOURCE_EXCLUDED)
        for name in files:
            path = Path(folder) / name
            relative = path.relative_to(source_dir)
            if name not in SOURCE_EXCLUDED and path.is_file() and (run_folder / relative).is_file():
                saved.append(relative.as_posix())
    return sorted(saved)


def _excluded(path, relative, patterns):
    return any(part in (".encap-run", ".encap-native", "0encap_folder") for part in relative.parts) or any(
        fnmatch.fnmatch(relative.as_posix(), pattern.rstrip("/"))
        or fnmatch.fnmatch(path.name, pattern.rstrip("/")) for pattern in patterns
    )


def resolve_dependencies(values, source_dir, excluded, workspace_root=None):
    if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ConfigurationError("dependencies must be a list of nonempty path strings")
    paths = []
    for value in values:
        if value.startswith("{root}/"):
            relative = value[len("{root}/"):]
            if (any(part in ("", ".", "..") for part in relative.split("/"))
                    or any(char in relative for char in "{}\\")):
                raise ConfigurationError(f"Invalid root-relative dependency path: {value!r}")
            if workspace_root is None:
                raise ConfigurationError("{root} dependencies require top-level workspace_root")
            root = settings.resolve_workspace_root(str(workspace_root))
            path = root / relative
        elif "{" in value or "}" in value:
            raise ConfigurationError(f"Unknown or misplaced dependency placeholder: {value!r}; use {{root}}/path")
        else:
            path = Path(value).expanduser()
        if not path.is_absolute():
            origin = getattr(values, "origin", None)
            if origin is None:
                raise ConfigurationError("Relative dependencies require a declaring configuration file")
            path = Path(origin) / path
        path = Path(os.path.abspath(path))
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ConfigurationError(f"Dependency symlinks are unsupported: {path}")
        if not path.is_file() and not path.is_dir():
            raise ConfigurationError(f"Dependency does not exist or is not a regular file/directory: {path}")
        relative = Path(os.path.relpath(path, source_dir))
        if any(part in SOURCE_EXCLUDED for part in path.parts) or _excluded(path, relative, excluded):
            raise ConfigurationError(f"Dependency is excluded from source packaging: {path}")
        if path not in paths:
            paths.append(path)
    return paths


def stage_source(source, destination, excluded):
    def ignore(directory, names):
        ignored = []
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(source)
            if path == destination or _excluded(path, relative, excluded):
                ignored.append(name)
            elif path.is_symlink():
                raise ConfigurationError(f"Source symlinks are unsupported: {path}")
            elif not path.is_file() and not path.is_dir():
                raise ConfigurationError(f"Source must contain regular files and directories: {path}")
        return ignored
    if source.is_symlink():
        raise ConfigurationError(f"Source symlinks are unsupported: {source}")
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignore)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def stage_workspace(source_dir, run_folder, dependencies, excluded, force=False):
    if not dependencies and not force:
        return None, "."
    source_dir = Path(os.path.abspath(source_dir))
    root = Path(os.path.commonpath([source_dir, *[path.parent if path.is_file() else path for path in dependencies]]))
    workspace = run_folder / ".encap-run" / "workspace"
    workspace.mkdir()
    patterns = [*excluded, *sorted(SOURCE_EXCLUDED)]
    for dependency in dependencies:
        stage_source(dependency, workspace / dependency.relative_to(root), patterns)
    experiment_path = source_dir.relative_to(root)
    stage_source(run_folder, workspace / experiment_path, patterns)
    return workspace, experiment_path.as_posix()


def write_run_configuration(run_folder, config, overrides):
    run_folder = Path(run_folder)
    if "git-track" in config:
        config["git-track"] = git_tracker.get_current_commit_hashes(copy.deepcopy(config["git-track"]))
    if "git-track-force" in config:
        config["git-track-force"] = git_tracker.get_current_commit_hashes(
            copy.deepcopy(config["git-track-force"]), force=True, verbose=settings.debug,
            commit_message=f"{run_folder} automatic commit from encap.",
        )
    settings.write_config_file(
        str(run_folder / ".encap_history.conf"), config,
        comment="This file is automatically generated by encap. Effective encap configuration file from last run that has no effect on future runs.",
    )
    config_path = run_folder / ".encap.conf"
    run_config = settings.read_config_file(str(config_path)) if config_path.exists() else {}
    settings.merge_dicts(run_config, copy.deepcopy(overrides))
    settings.write_config_file(
        str(config_path), run_config,
        comment="This file is automatically generated by encap. This file will patch the global configuration file if the experiment is rerun.",
    )
