import copy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from encap_lib import encap_settings as settings
from encap_lib.backends import ConfigurationError
from encap_lib.run_preparation import resolve_dependencies, stage_source, stage_workspace, write_run_configuration


@pytest.fixture
def layout(tmp_path):
    source = tmp_path / "experiments" / "training"
    source.mkdir(parents=True)
    (source / "run.py").write_text(
        "from pathlib import Path\n"
        "root = Path(__file__).resolve().parent\n"
        "assert Path('../../src/value.txt').read_text() == 'source'\n"
        "assert (root / '../shared/value.txt').read_text() == 'shared'\n"
        "Path('result.txt').write_text('passed')\n"
    )
    (source / ".encap.conf").write_text("dependencies: [../../src, ../shared]\n")
    for name, value in (("src", "source"), ("experiments/shared", "shared"), ("unselected", "private")):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "value.txt").write_text(value)
    return source


def prepare(source, destination, excluded=()):
    config = settings.read_config_file(source / ".encap.conf")
    dependencies = resolve_dependencies(config.get("dependencies", []), source, excluded)
    destination.mkdir(parents=True)
    (destination / ".encap-run").mkdir()
    stage_source(source, destination, excluded)
    write_run_configuration(destination, config, {})
    workspace, experiment_path = stage_workspace(source, destination, dependencies, excluded)
    return workspace, experiment_path


def test_workspace_preserves_layout_and_runs_offline(layout, tmp_path):
    workspace, experiment_path = prepare(layout, tmp_path / "runs" / "trial")
    assert experiment_path == "experiments/training"
    assert not (workspace / "unselected").exists()
    assert not (workspace / experiment_path / ".encap-run").exists()
    (tmp_path / "src" / "value.txt").write_text("changed after snapshot")
    subprocess.run([sys.executable, "run.py"], cwd=workspace / experiment_path, check=True)
    assert (workspace / experiment_path / "result.txt").read_text() == "passed"
    assert not (workspace / "src" / "result.txt").exists()


def test_dependency_origins_survive_inheritance_and_serialization(layout, tmp_path, monkeypatch):
    (tmp_path / ".encap.conf").write_text("dependencies: [src]\n")
    (layout / ".encap.conf").write_text("args: child\n")
    monkeypatch.setattr(settings, "config", {})
    monkeypatch.setattr(settings, "args_config", {})
    settings.load_encap_config_files_recursive(str(layout))
    values = copy.deepcopy(settings.config)["dependencies"]
    monkeypatch.chdir(layout)
    assert resolve_dependencies(values, layout, []) == [tmp_path / "src"]
    settings.write_config_file(tmp_path / "snapshot.yml", settings.config)
    text = (tmp_path / "snapshot.yml").read_text()
    assert "python/object" not in text
    assert yaml.safe_load(text)["dependencies"] == ["src"]
    (layout / ".encap.conf").write_text("dependencies: [../shared]\n")
    settings.config = {}
    settings.load_encap_config_files_recursive(str(layout))
    assert resolve_dependencies(settings.config["dependencies"], layout, []) == [layout.parent / "shared"]


@pytest.mark.parametrize("value", ["src", None, {}, [123], [""]])
def test_invalid_dependency_configuration(tmp_path, value):
    path = tmp_path / ".encap.conf"
    path.write_text(yaml.safe_dump({"dependencies": value}))
    with pytest.raises(ConfigurationError, match="dependencies"):
        settings.read_config_file(path)


def test_missing_excluded_and_symlink_dependencies(layout, tmp_path):
    for value in ("missing", ".env", "link"):
        if value == ".env":
            (layout / value).write_text("secret")
        if value == "link":
            (layout / value).symlink_to(tmp_path / "src", target_is_directory=True)
        values = settings.DependencyPaths([value], str(layout))
        with pytest.raises(ConfigurationError):
            resolve_dependencies(values, layout, [])
    with pytest.raises(ConfigurationError, match="excluded"):
        resolve_dependencies(settings.DependencyPaths(["../../src"], str(layout)), layout, ["src"])
    with pytest.raises(ConfigurationError, match="declaring"):
        resolve_dependencies(["../src"], layout, [])


def test_dependency_files_exclusions_and_empty_directories(layout, tmp_path):
    shared = layout.parent / "shared"
    (shared / ".env").write_text("secret")
    (shared / "cache.large").write_text("excluded")
    (shared / "empty").mkdir()
    (layout / ".encap.conf").write_text("dependencies: [../../src/value.txt, ../shared, ../shared]\n")
    workspace, experiment_path = prepare(layout, tmp_path / "runs" / "trial", ["*.large"])
    assert (workspace / "src" / "value.txt").read_text() == "source"
    assert (workspace / "experiments/shared/empty").is_dir()
    assert not (workspace / "experiments/shared/.env").exists()
    assert not (workspace / "experiments/shared/cache.large").exists()
    assert (workspace / experiment_path / ".encap_history.conf").is_file()


def test_nested_dependency_symlink_is_rejected(layout, tmp_path):
    (tmp_path / "src" / "link").symlink_to(layout / "run.py")
    with pytest.raises(ConfigurationError, match="symlinks"):
        prepare(layout, tmp_path / "runs" / "trial")


def test_overlapping_dependency_keeps_prepared_configuration(layout, tmp_path):
    (layout / ".encap.conf").write_text("dependencies: ['..', '../../src']\n")
    workspace, experiment_path = prepare(layout, tmp_path / "runs" / "trial")
    text = (workspace / experiment_path / ".encap.conf").read_text()
    assert text.startswith("# This file is automatically generated")
    assert not (workspace / "runs").exists()


def test_root_dependencies_preserve_relative_paths_and_deduplicate(layout, tmp_path, monkeypatch):
    (layout / ".encap.conf").write_text(
        'workspace_root: ../..\n'
        'dependencies: ["{root}/src", "{root}/src/value.txt", "../../src", "../shared"]\n'
    )
    config = settings.read_config_file(layout / ".encap.conf")
    monkeypatch.chdir(layout.parent)
    assert config["workspace_root"] == str(tmp_path.resolve())
    paths = resolve_dependencies(config["dependencies"], layout, [], workspace_root=config["workspace_root"])
    assert paths == [tmp_path / "src", tmp_path / "src/value.txt", layout.parent / "shared"]


def test_workspace_root_inherits_and_serializes_as_a_plain_path(layout, tmp_path, monkeypatch):
    (tmp_path / ".encap.conf").write_text('workspace_root: .\ndependencies: ["{root}/src"]\n')
    (layout / ".encap.conf").write_text("args: child\n")
    monkeypatch.setattr(settings, "config", {})
    monkeypatch.setattr(settings, "args_config", {})
    monkeypatch.chdir(layout)
    settings.load_encap_config_files_recursive(str(layout))
    config = copy.deepcopy(settings.config)
    assert resolve_dependencies(config["dependencies"], layout, [], config["workspace_root"]) == [tmp_path / "src"]
    settings.write_config_file(tmp_path / "snapshot.yml", config)
    saved = yaml.safe_load((tmp_path / "snapshot.yml").read_text())
    assert saved["workspace_root"] == str(tmp_path.resolve())
    assert saved["dependencies"] == ["{root}/src"]
    (layout / ".encap.conf").write_text("workspace_root: ..\n")
    settings.config = {}
    settings.load_encap_config_files_recursive(str(layout))
    assert settings.config["workspace_root"] == str(layout.parent.resolve())


@pytest.mark.parametrize("value", [None, True, 12, "", []])
def test_invalid_workspace_root_configuration(layout, tmp_path, value):
    path = tmp_path / ".encap.conf"
    path.write_text(yaml.safe_dump({"workspace_root": value}))
    with pytest.raises(ConfigurationError, match="workspace_root"):
        settings.read_config_file(path)


@pytest.mark.parametrize("value", ["missing", "src/value.txt", "/"])
def test_workspace_root_must_be_an_existing_non_root_directory(layout, tmp_path, value):
    path = tmp_path / ".encap.conf"
    path.write_text(yaml.safe_dump({"workspace_root": value}))
    config = settings.read_config_file(path)
    with pytest.raises(ConfigurationError, match="workspace_root"):
        settings.resolve_workspace_root(config["workspace_root"])


def test_inactive_project_roots_do_not_require_local_directories(tmp_path):
    path = tmp_path / ".encap.conf"
    path.write_text(
        "workspace_root: .\nprojects:\n  /another/project:\n    workspace_root: missing-on-this-machine\n"
    )
    config = settings.read_config_file(path)
    assert settings.resolve_workspace_root(config["workspace_root"]) == tmp_path.resolve()


@pytest.mark.parametrize("value", [
    "{root}", "{root}/", "{root}/../src", "{root}/./src", "{root}//src",
    "{unknown}/src", "prefix/{root}/src", "{root}\\src",
])
def test_invalid_dependency_placeholders(layout, tmp_path, value):
    with pytest.raises(ConfigurationError, match="placeholder|root-relative"):
        resolve_dependencies([value], layout, [], workspace_root=tmp_path)


def test_root_dependencies_require_a_top_level_root(layout):
    with pytest.raises(ConfigurationError, match="workspace_root"):
        resolve_dependencies(["{root}/src"], layout, [])


def test_root_dependencies_keep_exclusion_and_symlink_checks(layout, tmp_path):
    (tmp_path / "src/link").symlink_to(layout / "run.py")
    for path, excluded, message in (
        ("{root}/src/link", [], "symlinks"),
        ("{root}/src", ["src"], "excluded"),
        ("{root}/missing", [], "does not exist"),
    ):
        with pytest.raises(ConfigurationError, match=message):
            resolve_dependencies([path], layout, excluded, workspace_root=tmp_path)
