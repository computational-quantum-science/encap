import argparse
import importlib.machinery
import importlib.util
import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from encap_lib import backends, encap_settings as settings, native_wait, plugin_cli
from encap_lib.backends import (
    BackendError, CLIOption, ConfigurationError, MonitoringError, PluginConfig, RecordWriter,
    Registration, RunHandle, RunStatus,
)


def test_discovery_and_selector_collisions(monkeypatch):
    registration = Registration("fake", Mock(), ("-fake",), plugin_version="1.0")
    entries = Mock()
    entries.select.return_value = [SimpleNamespace(name="fake", load=lambda: registration)]
    monkeypatch.setattr(backends.metadata, "entry_points", lambda: entries)
    found = backends.discover()
    parser = argparse.ArgumentParser()
    backends.register_selectors(parser, found)
    assert parser.parse_args(["-fake"]).backend == "fake"
    with pytest.raises(SystemExit):
        parser.parse_args(["-fake", "--backend", "fake"])
    parser = argparse.ArgumentParser()
    parser.add_argument("-fake")
    with pytest.raises(ConfigurationError, match="conflicts"):
        backends.register_selectors(parser, found)
    for registrations, message in (
        ([registration, registration], "Duplicate backend"),
        ([Registration("other", Mock(), ("-fake",)), registration], "duplicate plugin selector"),
        ([Registration("fake", Mock(), api_version=999)], "Install compatible"),
        ([Registration("fake", Mock(), ("-fake",), cli_options=(
            CLIOption(("-fake",), "setting"),
        ))], "duplicate plugin selector"),
        ([Registration("fake", Mock(), cli_options=(
            CLIOption(("-first",), "setting"), CLIOption(("-second",), "setting"),
        ))], "duplicate plugin option"),
    ):
        entries.select.return_value = [SimpleNamespace(name=item.name, load=lambda item=item: item) for item in registrations]
        with pytest.raises(ConfigurationError, match=message):
            backends.discover()


@pytest.mark.parametrize("state,code,expected", [
    ("succeeded", None, 0), ("failed", 23, 23), ("failed", 0, 1),
    ("failed", 300, 1), ("failed", True, 1), ("cancelled", None, 130),
])
def test_wait_terminal_states(state, code, expected):
    backend = Mock()
    backend.status.side_effect = [RunStatus("running"), RunStatus(state, code)]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(backends.time, "sleep", lambda interval: None)
        assert backends.exit_code(backends.wait(backend, RunHandle({}))) == expected
    backend.pull.assert_not_called()
    backend.tail.assert_not_called()
    backend.cancel.assert_not_called()


@pytest.mark.parametrize("status", [RunStatus("unknown"), RunStatus("succeeded", outputs_finalized=False)])
def test_wait_never_infers_success(status):
    backend = Mock()
    backend.status.return_value = status
    with pytest.raises(MonitoringError):
        backends.wait(backend, RunHandle({}))


def test_wait_interrupt_and_connection_error():
    backend = Mock()
    for error in (KeyboardInterrupt(), MonitoringError("offline")):
        backend.status.side_effect = error
        with pytest.raises(type(error)):
            backends.wait(backend, RunHandle({}))
    backend.cancel.assert_not_called()


def test_records_are_versioned_atomic_and_recoverable(tmp_path):
    registration = Registration("fake", Mock(), plugin_version="2")
    destination = tmp_path / "run"
    writer = RecordWriter.reserve(destination, registration, "run")
    assert writer.read()["state"] == "submission-unconfirmed"
    handle = RunHandle({"request_id": "pre-submission-recovery"})
    writer.save_handle(handle)
    reconnected = RecordWriter(writer.directory)
    assert reconnected.read()["handle"]["data"] == handle.data
    assert not list(writer.directory.glob("*.tmp"))
    with pytest.raises(BackendError, match="already exists"):
        RecordWriter.reserve(destination, registration, "run")
    with pytest.raises(BackendError, match="JSON"):
        writer.save_handle(RunHandle({"invalid": object()}))
    assert reconnected.read()["handle"]["data"] == handle.data
    writer.update(record_version=999)
    with pytest.raises(BackendError, match="Incompatible"):
        writer.read()


def test_config_origins_survive_merge(tmp_path, monkeypatch):
    parent = tmp_path / ".encap.conf"
    child_dir = tmp_path / "experiment"
    child_dir.mkdir()
    child = child_dir / ".encap.conf"
    parent.write_text("backend: fake\nplugins:\n  fake:\n    input: data\n    other: original\n")
    child.write_text("plugins:\n  fake:\n    other: changed\n")
    monkeypatch.setattr(settings, "config", {})
    monkeypatch.setattr(settings, "args_config", {})
    settings.load_encap_config_files_recursive(str(child_dir))
    namespace = settings.config["plugins"]["fake"]
    assert namespace.resolve_path("input") == tmp_path / "data"
    assert namespace.resolve_path("other") == child_dir / "changed"
    settings.write_config_file(str(tmp_path / "snapshot"), settings.config)
    assert "python/object" not in (tmp_path / "snapshot").read_text()


def load_cli():
    path = Path(__file__).resolve().parents[1] / "encap"
    loader = importlib.machinery.SourceFileLoader("encap_plugin_test_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def cli(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "config", {"file_extension": {"py": "python -u"}})
    monkeypatch.setattr(settings, "args_config", {})
    monkeypatch.setattr(settings, "using_slurm", False)
    monkeypatch.setattr(settings, "using_pueue", False)
    factory = Mock()
    backend = factory.return_value
    backend.submit.return_value = RunHandle({"operation": "123"})
    registration = Registration("fake", factory, ("-fake",), cli_options=(
        CLIOption(("-fake_setting",), "setting"),
        CLIOption(("-fake_number",), "number", parse=int),
    ))
    monkeypatch.setattr(backends, "discover", lambda: {"fake": registration})
    monkeypatch.setattr(cli, "get_machine", Mock(side_effect=AssertionError("must not construct machine")))
    monkeypatch.setattr(cli, "LocalMachine", Mock(side_effect=AssertionError("must not construct machine")))
    (tmp_path / "experiment").mkdir()
    (tmp_path / "experiment" / "run.py").write_text("print('hello')")
    return cli, factory, backend


def invoke(cli, monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["encap", *arguments])
    return cli.main()


@pytest.mark.parametrize("arguments,expected", [
    ([], ["configured/"]),
    (["--rsync-exclude-pull"], []),
    (["--rsync-exclude-pull", "*.pkl", "models/"], ["*.pkl", "models/"]),
    (["--rsync-exclude-pull", "*.pkl", "--rsync-exclude-pull", "models/"], ["*.pkl", "models/"]),
])
def test_shared_pull_exclusions_reach_plugin_and_saved_configuration(cli, monkeypatch, arguments, expected):
    module, factory, backend = cli
    source = Path("experiment")
    (source / ".encap.conf").write_text("rsync_exclude_pull: ['configured/']\n")
    (source / "model.pkl").write_bytes(b"source model")
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake", *arguments)
    namespace = factory.call_args.args[0]
    assert namespace.rsync_exclude_pull == expected
    assert "rsync_exclude_pull" not in namespace
    request = backend.submit.call_args.args[0]
    assert (request.run_folder / "model.pkl").read_bytes() == b"source model"
    saved = yaml.safe_load((request.run_folder / ".encap_history.conf").read_text())
    assert saved["rsync_exclude_pull"] == expected


@pytest.mark.parametrize("mode", ["pull", "tail"])
@pytest.mark.parametrize("arguments,expected", [
    ([], None), (["--rsync-exclude-pull"], []),
    (["--rsync-exclude-pull", "*.pkl"], ["*.pkl"]),
])
def test_saved_pull_exclusion_override_does_not_reload_or_modify_policy(
    cli, tmp_path, monkeypatch, mode, arguments, expected,
):
    module, factory, backend = cli
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    writer = RecordWriter(tmp_path / "0encap_folder/experiment/trial/.encap-run")
    original = writer.read()["handle"]
    (tmp_path / "experiment/.encap.conf").write_text("broken: [configuration")
    invoke(module, monkeypatch, mode, "experiment", "-n", "trial", *arguments)
    assert factory.call_args.args[0] == {}
    assert factory.call_args.args[0].rsync_exclude_pull == expected
    assert writer.read()["handle"] == original


@pytest.mark.parametrize("value", ["not-a-list", [""], [1], ["line\nbreak"]])
def test_invalid_shared_pull_exclusions_fail_before_preparing_run(cli, monkeypatch, value):
    module, factory, backend = cli
    Path("experiment/.encap.conf").write_text(yaml.safe_dump({"rsync_exclude_pull": value}))
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    assert result.value.code == 2
    factory.assert_not_called()
    backend.submit.assert_not_called()
    assert not Path("0encap_folder").exists()


@pytest.mark.parametrize("mode", ["wait", "status", "kill", "copy", "rerun", "tar", "untar"])
def test_pull_exclusion_cli_rejects_unrelated_modes(cli, monkeypatch, mode):
    module, factory, backend = cli
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, mode, "experiment", "-n", "trial", "--rsync-exclude-pull", "*.pkl")
    assert result.value.code == 2
    factory.assert_not_called()
    backend.submit.assert_not_called()


def test_project_pull_exclusions_reach_plugin(cli, tmp_path, monkeypatch):
    module, factory, _ = cli
    settings.config["rsync_exclude_pull"] = ["global/"]
    settings.config["projects"] = {str(tmp_path): {"rsync_exclude_pull": ["project/"]}}
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    assert factory.call_args.args[0].rsync_exclude_pull == ["project/"]


def test_main_pull_override_reaches_ssh_without_changing_matching(cli, tmp_path, monkeypatch):
    from encap_lib.encap_lib import get_machine

    module, _, _ = cli
    settings.config["projects"] = {str(tmp_path): {"ssh": {"worker": {
        "ip": "host", "user": "user", "dir": "/remote", "rsync_exclude_pull": ["machine/"],
    }}}}
    monkeypatch.setattr(sys, "argv", [
        "encap", "pull", "experiment", "-n", "trial",
        "--rsync-exclude-pull", "*.pkl", "it's.pkl",
    ])
    module.parse_arguments()
    machine = get_machine("worker", str(tmp_path))
    assert machine.rsync_exclude_pull == ["*.pkl", "it's.pkl"]
    command = Mock()
    monkeypatch.setattr("encap_lib.machines.run_code_local", command)
    machine.pull("remote", "local")
    shell = command.call_args.args[0]
    assert "--exclude " + shlex.quote("*.pkl") in shell
    assert "--exclude " + shlex.quote("it's.pkl") in shell


def test_retrieval_receipts_are_atomic_and_separate_from_recovery(tmp_path, monkeypatch):
    writer = RecordWriter.reserve(tmp_path / "run", Registration("fake", Mock()), "run")
    original = writer.path.read_bytes()
    details = {"state": "succeeded", "scope": "partial", "omitted": [{"path": "model.pkl"}],
               "previous_omissions": []}
    writer.save_retrieval("gs://bucket/output/", details)
    receipt = writer.directory / "retrieval.json"
    saved = receipt.read_bytes()
    replace = backends.os.replace

    def fail_receipt(source, destination):
        if destination == receipt:
            raise OSError("disk unavailable")
        replace(source, destination)

    monkeypatch.setattr(backends.os, "replace", fail_receipt)
    with pytest.raises(BackendError, match="Cannot save retrieval"):
        writer.save_retrieval("gs://bucket/output/", dict(details, scope="full", omitted=[]))
    assert receipt.read_bytes() == saved
    assert writer.path.read_bytes() == original
    assert not list(writer.directory.glob("record-*.tmp"))


@pytest.mark.parametrize("contents", [
    "broken json", "[]", '{"version": true, "sources": {}}',
    '{"version": 1, "sources": {"gs://bucket/": {"state": "succeeded", "scope": "full", '
    '"omitted": [1], "previous_omissions": []}}}',
])
def test_invalid_retrieval_receipts_are_not_silently_ignored(tmp_path, contents):
    (tmp_path / "retrieval.json").write_text(contents)
    with pytest.raises(BackendError, match="retrieval"):
        RecordWriter(tmp_path).read_retrieval()


def test_status_reports_retrieval_separately_from_remote_success(cli, tmp_path, monkeypatch, capsys):
    module, _, backend = cli
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    writer = RecordWriter(tmp_path / "0encap_folder/experiment/trial/.encap-run")
    original = writer.read()
    writer.save_retrieval("gs://bucket/output/", {
        "state": "succeeded", "scope": "partial",
        "omitted": [{"path": "model.pkl"}], "previous_omissions": [],
    })
    assert writer.read() == original
    backend.status.return_value = RunStatus("succeeded", outputs_finalized=True)
    capsys.readouterr()
    invoke(module, monkeypatch, "status", "experiment", "-n", "trial")
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "succeeded"
    assert status["outputs_finalized"] is True
    assert status["retrieval"]["sources"]["gs://bucket/output/"]["scope"] == "partial"


def test_plugin_lifecycle_uses_saved_handle(cli, tmp_path, monkeypatch, capsys):
    module, factory, backend = cli
    (tmp_path / "experiment" / ".encap.conf").write_text("plugins:\n  fake:\n    bucket: original\n")
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake", "-a", '--value "two words"')
    assert factory.call_args.args[0] == {"bucket": "original"}
    request = backend.submit.call_args.args[0]
    assert request.arguments == ["--value", "two words"]
    assert request.entrypoint == "run.py"
    assert request.run_folder == tmp_path / "0encap_folder" / "experiment" / "trial"
    assert (request.run_folder / "run.py").read_text() == "print('hello')"
    assert (request.run_folder / ".encap.conf").is_file()
    assert (request.run_folder / ".encap_history.conf").is_file()
    destination = tmp_path / "0encap_folder" / "experiment" / "trial"
    assert request.record_dir == destination / ".encap-run"
    backend.tail.assert_called_once_with(RunHandle({"operation": "123"}))
    backend.pull.assert_called_once_with(RunHandle({"operation": "123"}), destination)
    # Reconnect must work without even parsing changed/broken provider config or source.
    (tmp_path / "experiment" / ".encap.conf").write_text("not: [valid")
    backend.status.return_value = RunStatus("succeeded", outputs_finalized=True, cleanup_pending=True)
    for command in ("tail", "pull", "status", "kill"):
        invoke(module, monkeypatch, command, "experiment", "-n", "trial")
    assert factory.call_args.args[0] == {}
    assert backend.pull.call_count == 3
    backend.pull.assert_called_with(RunHandle({"operation": "123"}), destination)
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "wait", "experiment", "-n", "trial")
    assert result.value.code == 0
    assert "cleanup is still pending" in capsys.readouterr().out
    backend.status.return_value = RunStatus("running")
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    assert result.value.code == 2


@pytest.mark.parametrize("flag", ["-vm", "-sl", "-pu", "-i", "-dr", "-fp"])
def test_plugin_conflicting_native_options(cli, monkeypatch, flag):
    module, factory, backend = cli
    extra = {"-vm": ["host"], "-i": ["2"]}.get(flag, [])
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake", flag, *extra)
    assert result.value.code == 2
    backend.submit.assert_not_called()
    factory.assert_not_called()


@pytest.mark.parametrize("text", ["value", "[0, 2, 3]", "range(2, 5)", "0"])
def test_plugin_options_reach_namespace_without_interpretation(cli, monkeypatch, text):
    module, factory, backend = cli
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake_setting", text)
    request = backend.submit.call_args.args[0]
    assert factory.call_args.args[0]["setting"] == text
    assert factory.call_args.args[0].resolve_path("setting") == Path.cwd() / text
    assert not hasattr(request, "instances")
    for name in (".encap.conf", ".encap_history.conf"):
        saved = yaml.safe_load((request.run_folder / name).read_text())
        assert saved["plugins"]["fake"]["setting"] == text
        assert "backend_instances" not in saved


@pytest.mark.parametrize("text", ["not-a-number", "[]"])
def test_plugin_defined_parser_rejects_invalid_options(cli, monkeypatch, text):
    module, factory, backend = cli
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake_number", text)
    assert result.value.code == 2
    factory.assert_not_called()
    backend.submit.assert_not_called()


def test_plugin_option_rejects_native_i(cli, monkeypatch):
    module, factory, backend = cli
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake_setting", "value", "-i", "2")
    assert result.value.code == 2
    backend.submit.assert_not_called()


def test_plugin_namespace_options_reach_factory(cli, monkeypatch, tmp_path):
    module, factory, backend = cli
    (tmp_path / "experiment" / ".encap.conf").write_text("backend: fake\nplugins:\n  fake:\n    setting: [2, 5]\n")
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial")
    assert factory.call_args.args[0]["setting"] == [2, 5]


def test_plugin_options_merge_and_override_inherited_namespace(cli, monkeypatch, tmp_path):
    module, factory, backend = cli
    (tmp_path / ".encap.conf").write_text("plugins:\n  fake:\n    setting: inherited\n    number: 2\n")
    (tmp_path / "experiment" / ".encap.conf").write_text("plugins:\n  fake:\n    number: 3\n")
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake_number", "4")
    assert factory.call_args.args[0] == {"setting": "inherited", "number": 4}
    request = backend.submit.call_args.args[0]
    history = yaml.safe_load((request.run_folder / ".encap_history.conf").read_text())
    assert history["plugins"]["fake"] == {"setting": "inherited", "number": 4}


def test_plugin_options_require_new_run(cli, monkeypatch):
    module, factory, backend = cli
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "status", "experiment", "-n", "trial", "-fake_setting", "value")
    assert result.value.code == 2
    factory.assert_not_called()
    backend.submit.assert_not_called()


def test_removed_core_option_requires_explicit_migration(cli, monkeypatch, tmp_path):
    module, factory, backend = cli
    (tmp_path / "experiment" / ".encap.conf").write_text("backend: fake\nbackend_instances: 2\n")
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial")
    assert result.value.code == 2
    factory.assert_not_called()
    backend.submit.assert_not_called()
    assert not (tmp_path / "0encap_folder").exists()


def test_uncertain_submission_preserves_intent(cli, tmp_path, monkeypatch):
    module, factory, backend = cli
    def submit(request, writer):
        writer.save_handle(RunHandle({"request_id": "known-before-request"}))
        raise MonitoringError("response lost")
    backend.submit.side_effect = submit
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    assert result.value.code == 2
    writer = RecordWriter(tmp_path / "0encap_folder" / "experiment" / "trial" / ".encap-run")
    assert writer.read()["state"] == "submission-unconfirmed"
    invoke(module, monkeypatch, "tail", "experiment", "-n", "trial")
    backend.tail.assert_called_once_with(RunHandle({"request_id": "known-before-request"}))
    assert backend.submit.call_count == 1


@pytest.mark.parametrize("automatic", [False, True])
def test_pre_submission_failure_can_be_retried(cli, tmp_path, monkeypatch, automatic):
    module, _, backend = cli
    def submit(request, writer):
        writer.update(state="not-submitted")
        raise BackendError("Cloud run unchanged; deletion was not approved")
    backend.submit.side_effect = submit
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    assert result.value.code == 2
    writer = RecordWriter(tmp_path / "0encap_folder/experiment/trial/.encap-run")
    assert writer.read()["state"] == "not-submitted"
    assert writer.read()["handle"] is None
    assert "deletion was not approved" in writer.read()["submission_error"]
    backend.submit.side_effect = None
    backend.status.side_effect = AssertionError("no remote submission to inspect")
    prompt = Mock(return_value="y")
    monkeypatch.setattr("builtins.input", prompt)
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", *(["-y"] if automatic else []))
    assert prompt.call_count == (0 if automatic else 1)
    if not automatic:
        assert "(not submitted)" in prompt.call_args.args[0]
    assert writer.read()["state"] == "submitted"
    assert backend.submit.call_count == 2
    backend.status.assert_not_called()
    backend.cancel.assert_not_called()


@pytest.mark.parametrize("mode", ["status", "wait", "tail", "pull", "kill"])
def test_not_submitted_run_has_no_remote_management(cli, tmp_path, monkeypatch, capsys, mode):
    module, _, backend = cli
    writer = RecordWriter.reserve(
        tmp_path / "0encap_folder/experiment/trial", Registration("fake", Mock()), "trial",
    )
    writer.update(state="not-submitted")
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, mode, "experiment", "-n", "trial")
    assert result.value.code == 2
    assert "not submitted" in capsys.readouterr().err
    backend.submit.assert_not_called()
    backend.status.assert_not_called()
    backend.tail.assert_not_called()
    backend.pull.assert_not_called()
    backend.cancel.assert_not_called()


def test_missing_plugin_and_unsupported_mode(cli, tmp_path, monkeypatch):
    module, factory, backend = cli
    (tmp_path / "experiment" / ".encap.conf").write_text("backend: missing\n")
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial")
    assert result.value.code == 2
    (tmp_path / "experiment" / ".encap.conf").write_text("backend: fake\n")
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "rerun", "experiment", "-n", "trial")
    assert result.value.code == 2
    backend.submit.assert_not_called()


@pytest.mark.parametrize("layout", ["native", "legacy"])
@pytest.mark.parametrize("kind,state,expected", [
    ("slurm", "7|COMPLETED|0:0", 0), ("slurm", "7|FAILED|4:0", 4),
    ("slurm", "7|CANCELLED by 1|0:15", 130), ("slurm", "7|OUT_OF_MEMORY|0:9", 137),
    ("pueue", {"Done": {"result": "Success"}}, 0),
    ("pueue", {"Done": {"result": {"Failed": 8}}}, 8),
    ("pueue", {"Done": {"result": "Killed"}}, 130),
    ("process", "0", 0), ("process", "19", 19),
])
def test_native_wait_actual_completion_mechanisms(kind, state, expected, layout):
    machine = Mock()
    tracking = json.dumps({"version": 1, "kind": kind, "identifiers": [{"id": "7", "instance": "0"}]})
    response = json.dumps({"tasks": {"7": {"status": state}}}) if kind == "pueue" else state
    machine.run_code.side_effect = [([layout, tracking], 0), ([response], 0)]
    assert native_wait.wait(machine, "experiment") == expected
    if kind == "slurm":
        assert "sacct -j 7" in machine.run_code.call_args.args[0]
    elif kind == "pueue":
        assert machine.run_code.call_args.args[0] == "pueue status --json"
    else:
        assert "kill -0 7" in machine.run_code.call_args.args[0]
        expected_path = "experiment/.encap-exit" if layout == "legacy" else "experiment/.encap-native/exit"
        assert expected_path in machine.run_code.call_args.args[0]
    assert all("tail" not in call.args[0] for call in machine.run_code.call_args_list)


@pytest.mark.parametrize("result", ["unknown", "garbage"])
def test_native_wait_disappearance_is_not_success(result):
    machine = Mock()
    machine.run_code.side_effect = [
        (["native", json.dumps({"version": 1, "kind": "process", "identifiers": [{"id": "9", "instance": "0"}]})], 0),
        ([result], 0),
    ]
    with pytest.raises(MonitoringError):
        native_wait.wait(machine, "run")


@pytest.mark.parametrize("kind,pid", [("process", "7"), ("slurm", "slurm:7"), ("pueue", None)])
def test_native_tail_uses_tracking(kind, pid):
    machine = Mock()
    tracking = json.dumps({"version": 1, "kind": kind, "identifiers": [{"id": "7", "instance": "2"}]})
    machine.run_code.return_value = (["native", tracking], 0)
    assert native_wait.tail_target(machine, "run") == (pid, "log_2")
    assert machine.run_code.call_count == 1


def test_native_tracking_precedence_and_quoted_paths(tmp_path):
    import subprocess
    from encap_lib.machines import LocalMachine
    folder = tmp_path / "run with 'quotes'"
    folder.mkdir()
    machine = LocalMachine(local_project_dir=str(tmp_path))
    legacy = {"version": 1, "kind": "process", "identifiers": [{"id": "99", "instance": "0"}]}
    (folder / ".encap-native.json").write_text(json.dumps(legacy))
    (folder / ".encap-exit").write_text("0\n")
    (folder / "pid").write_text("99\n")
    native_wait.save_tracking(machine, folder.name, "process", [{"id": "7", "instance": "2"}])
    result = subprocess.run(
        ["bash", "-c", "false; " + native_wait.completion_code(2)], cwd=folder, capture_output=True,
    )
    assert result.returncode == 1
    assert native_wait.wait(machine, folder.name) == 1
    assert native_wait.tail_target(machine, folder.name) == ("7", "log_2")
    assert (folder / ".encap-native" / "exit_2").read_text() == "1\n"
    assert not list((folder / ".encap-native").glob("*.tmp"))
    (folder / ".encap-native" / "tracking.json").write_text("invalid json")
    with pytest.raises(MonitoringError, match="Invalid native run tracking"):
        native_wait.wait(machine, folder.name)
    (folder / ".encap-native" / "tracking.json").unlink()
    assert native_wait.wait(machine, folder.name) == 0
    assert native_wait.tail_target(machine, folder.name) == ("99", "log")


@pytest.mark.parametrize("pid", ["7", "slurm:7", None])
def test_native_tail_legacy_pid_without_tracking(tmp_path, pid):
    from encap_lib.machines import LocalMachine
    machine = LocalMachine(local_project_dir=str(tmp_path))
    if pid is not None:
        (tmp_path / "pid").write_text(pid + "\n")
    assert native_wait.tail_target(machine, ".") == (pid, "log")
    with pytest.raises(MonitoringError, match="No supported completion tracking"):
        native_wait.wait(machine, ".")


def test_native_tracking_read_errors_are_not_legacy_fallback(tmp_path):
    from encap_lib.machines import LocalMachine
    machine = LocalMachine(local_project_dir=str(tmp_path))
    (tmp_path / ".encap-native").mkdir()
    (tmp_path / ".encap-native" / "tracking.json").mkdir()
    (tmp_path / "pid").write_text("7\n")
    with pytest.raises(MonitoringError, match="Cannot monitor computation"):
        native_wait.tail_target(machine, ".")
    machine = Mock()
    machine.run_code.return_value = (["ssh connection lost"], 255)
    with pytest.raises(MonitoringError, match="connection lost"):
        native_wait.read_tracking(machine, "run")


def test_native_tracking_write_failure_is_explicit(tmp_path):
    from encap_lib.machines import LocalMachine
    (tmp_path / ".encap-native").write_text("not a directory")
    with pytest.raises(MonitoringError, match="Cannot monitor computation"):
        native_wait.save_tracking(LocalMachine(local_project_dir=str(tmp_path)), ".", "process",
                                  [{"id": "7", "instance": "0"}])


def test_project_global_namespace_precedes_experiment_config(cli, tmp_path, monkeypatch):
    module, factory, backend = cli
    global_file = tmp_path / "global.yml"
    global_file.write_text(
        f"projects:\n  {tmp_path}:\n    backend: fake\n    plugins:\n      fake:\n        input: global-data\n        value: global\n"
    )
    settings.config = settings.read_config_file(str(global_file))
    (tmp_path / "experiment" / ".encap.conf").write_text("plugins:\n  fake:\n    value: experiment\n")
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial")
    namespace = factory.call_args.args[0]
    assert namespace["value"] == "experiment"
    assert namespace.resolve_path("input") == tmp_path / "global-data"


def test_reconnect_after_source_removed_and_monitoring_error(cli, tmp_path, monkeypatch):
    module, factory, backend = cli
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    (tmp_path / "experiment" / "run.py").unlink()
    (tmp_path / "experiment").rmdir()
    backend.status.side_effect = MonitoringError("connection unavailable")
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "wait", "experiment", "-n", "trial")
    assert result.value.code == 2
    record = RecordWriter(tmp_path / "0encap_folder" / "experiment" / "trial" / ".encap-run").read()
    assert record["monitoring_error"] == "connection unavailable"
    assert record["state"] == "submitted"
    backend.cancel.assert_not_called()


def test_cli_wait_interrupt_never_cancels(cli, monkeypatch):
    module, factory, backend = cli
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    backend.status.side_effect = KeyboardInterrupt
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "wait", "experiment", "-n", "trial")
    assert result.value.code == 130
    backend.cancel.assert_not_called()


def test_native_wait_scheduler_missing_or_offline_is_error():
    tracking = json.dumps({"version": 1, "kind": "slurm", "identifiers": [{"id": "7", "instance": "0"}]})
    machine = Mock()
    machine.run_code.side_effect = [(["native", tracking], 0), ([], 0), ([], 0)]
    with pytest.raises(MonitoringError, match="absent"):
        native_wait.wait(machine, "run")
    machine.run_code.side_effect = [(["native", tracking], 0), (["ssh connection lost"], 255)]
    with pytest.raises(MonitoringError, match="connection lost"):
        native_wait.wait(machine, "run")


def test_native_pueue_wait_handles_modern_queued_status(monkeypatch):
    tracking = json.dumps({"version": 1, "kind": "pueue", "identifiers": [{"id": "7", "instance": "0"}]})
    machine = Mock()
    machine.run_code.side_effect = [
        (["native", tracking], 0),
        ([json.dumps({"tasks": {"7": {"status": {"Queued": {"enqueued_at": "now"}}}}})], 0),
        ([json.dumps({"tasks": {"7": {"status": {"Done": {"result": "Success"}}}}})], 0),
    ]
    monkeypatch.setattr(native_wait.time, "sleep", Mock())
    assert native_wait.wait(machine, "run") == 0
    native_wait.time.sleep.assert_called_once()


@pytest.mark.parametrize("configuration", [
    "i: 4\n", "environment:\n  INVALID=NAME: value\n", "script_name: missing.py\n",
])
def test_invalid_plugin_request_rejected_before_submission(cli, tmp_path, monkeypatch, configuration):
    module, factory, backend = cli
    (tmp_path / "experiment" / ".encap.conf").write_text(configuration)
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    assert result.value.code == 2
    backend.submit.assert_not_called()


def test_saved_record_with_missing_plugin_is_actionable(cli, tmp_path, monkeypatch):
    module, factory, backend = cli
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    backend.tail.reset_mock()
    monkeypatch.setattr(backends, "discover", lambda: {})
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "tail", "experiment", "-n", "trial")
    assert result.value.code == 2
    backend.tail.assert_not_called()


@pytest.mark.parametrize("error,expected_code", [
    (KeyboardInterrupt(), 130), (MonitoringError("log connection lost"), 2),
])
def test_run_tails_only_after_confirmed_record_and_preserves_submission(cli, tmp_path, monkeypatch, error, expected_code):
    module, factory, backend = cli
    writer = RecordWriter(tmp_path / "0encap_folder" / "experiment" / "trial" / ".encap-run")
    def tail(handle):
        record = writer.read()
        assert record["state"] == "submitted"
        assert record["handle"]["data"] == handle.data
        raise error
    backend.tail.side_effect = tail
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    assert result.value.code == expected_code
    record = writer.read()
    assert record["state"] == "submitted"
    assert "submission_error" not in record
    if isinstance(error, MonitoringError):
        assert record["monitoring_error"] == "log connection lost"
    backend.cancel.assert_not_called()
    backend.pull.assert_not_called()
    backend.submit.assert_called_once()


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("backup_enabled", [False, True])
def test_overwrite_finished_run_respects_backup_setting(cli, tmp_path, monkeypatch, state, automatic, backup_enabled):
    module, factory, backend = cli
    if backup_enabled:
        (tmp_path / "experiment" / ".encap.conf").write_text("backup_on_overwrite: true\n")
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    destination = tmp_path / "0encap_folder" / "experiment" / "trial"
    (destination / "result.txt").write_text("old output")
    backend.status.return_value = RunStatus(state)
    backend.submit.return_value = RunHandle({"operation": "new"})
    prompt = Mock(return_value="y")
    monkeypatch.setattr("builtins.input", prompt)
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", *(["-y"] if automatic else []))
    assert prompt.call_count == (0 if automatic else 1)
    if not automatic:
        assert ("preserve the old run in a backup" in prompt.call_args.args[0]) == backup_enabled
    backups = list(destination.parent.glob("trial.backup-*"))
    assert len(backups) == int(backup_enabled)
    if backup_enabled:
        assert (backups[0] / "result.txt").read_text() == "old output"
        assert RecordWriter(backups[0] / ".encap-run").read()["handle"]["data"] == {"operation": "123"}
    assert not (destination / "result.txt").exists()
    assert RecordWriter(destination / ".encap-run").read()["handle"]["data"] == {"operation": "new"}
    assert backend.submit.call_count == 2
    backend.cancel.assert_not_called()
    assert backend.pull.call_count == 2


@pytest.mark.parametrize("value", ["'false'", "1", "null", "[]"])
def test_invalid_backup_setting_does_not_overwrite(cli, tmp_path, monkeypatch, value):
    module, factory, backend = cli
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    destination = tmp_path / "0encap_folder" / "experiment" / "trial"
    (destination / "result.txt").write_text("old output")
    backend.status.return_value = RunStatus("failed")
    (tmp_path / "experiment" / ".encap.conf").write_text(f"backup_on_overwrite: {value}\n")
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-y")
    assert result.value.code == 2
    assert (destination / "result.txt").read_text() == "old output"
    backend.submit.assert_called_once()


@pytest.mark.parametrize("scope", ["global", "project"])
@pytest.mark.parametrize("local_override", [False, True])
def test_backup_setting_inherits_and_respects_local_override(cli, tmp_path, monkeypatch, scope, local_override):
    module, factory, backend = cli
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    destination = tmp_path / "0encap_folder" / "experiment" / "trial"
    (destination / "result.txt").write_text("old output")
    if scope == "global":
        settings.config["backup_on_overwrite"] = True
    else:
        settings.config["projects"] = {str(tmp_path): {"backup_on_overwrite": True}}
    if local_override:
        (tmp_path / "experiment" / ".encap.conf").write_text("backup_on_overwrite: false\n")
    backend.status.return_value = RunStatus("failed")
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-y")
    backups = list(destination.parent.glob("trial.backup-*"))
    assert len(backups) == int(not local_override)
    if backups:
        assert (backups[0] / "result.txt").read_text() == "old output"
    assert not (destination / "result.txt").exists()


def test_declining_overwrite_preserves_existing_run(cli, tmp_path, monkeypatch):
    module, factory, backend = cli
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    backend.status.return_value = RunStatus("failed")
    monkeypatch.setattr("builtins.input", Mock(return_value="n"))
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial")
    assert backend.submit.call_count == 1
    destination = tmp_path / "0encap_folder" / "experiment" / "trial"
    assert destination.exists()
    assert not list(destination.parent.glob("trial.backup-*"))


@pytest.mark.parametrize("state", [
    "submitted", "running", "unknown", "unconfirmed", "offline", "not-submitted-with-handle",
])
def test_unsafe_overwrite_is_rejected_even_with_yes(cli, tmp_path, monkeypatch, state):
    module, factory, backend = cli
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    destination = tmp_path / "0encap_folder" / "experiment" / "trial"
    if state == "unconfirmed":
        RecordWriter(destination / ".encap-run").update(state="submission-unconfirmed")
    elif state == "not-submitted-with-handle":
        RecordWriter(destination / ".encap-run").update(state="not-submitted")
        backend.status.return_value = RunStatus("running")
    elif state == "offline":
        backend.status.side_effect = MonitoringError("offline")
    else:
        backend.status.return_value = RunStatus(state)
    prompt = Mock(side_effect=AssertionError("must not prompt"))
    monkeypatch.setattr("builtins.input", prompt)
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-y")
    assert result.value.code == 2
    assert backend.submit.call_count == 1
    assert destination.exists()
    assert not list(destination.parent.glob("trial.backup-*"))
    backend.cancel.assert_not_called()


def test_overwrite_detects_changed_record_before_renaming(cli, tmp_path, monkeypatch):
    module, factory, backend = cli
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    destination = tmp_path / "0encap_folder" / "experiment" / "trial"
    backend.status.return_value = RunStatus("failed")
    def confirm(message):
        RecordWriter(destination / ".encap-run").update(state="submission-unconfirmed")
        return "y"
    monkeypatch.setattr("builtins.input", confirm)
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial")
    assert result.value.code == 2
    assert backend.submit.call_count == 1
    assert destination.exists()
    assert not list(destination.parent.glob("trial.backup-*"))


@pytest.mark.parametrize("mode", ["run", "tail"])
def test_auto_pull_follows_tail_and_surfaces_download_failure(cli, tmp_path, monkeypatch, mode):
    module, factory, backend = cli
    if mode == "tail":
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
        backend.reset_mock()
    destination = tmp_path / "0encap_folder" / "experiment" / "trial"
    def pull(handle, target):
        backend.tail.assert_called_once_with(handle)
        assert target == destination
        assert RecordWriter(target / ".encap-run").read()["state"] == "submitted"
        raise MonitoringError("download unavailable")
    backend.pull.side_effect = pull
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, mode, "experiment", "-n", "trial", *(["-fake"] if mode == "run" else []))
    assert result.value.code == 2
    record = RecordWriter(destination / ".encap-run").read()
    assert record["monitoring_error"] == "download unavailable"
    assert "submission_error" not in record
    backend.cancel.assert_not_called()


@pytest.mark.parametrize("error", [KeyboardInterrupt(), MonitoringError("log connection lost")])
def test_reattached_tail_skips_pull_when_interrupted_or_failed(cli, monkeypatch, error):
    module, factory, backend = cli
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    backend.pull.reset_mock()
    backend.tail.side_effect = error
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "tail", "experiment", "-n", "trial")
    assert result.value.code == (130 if isinstance(error, KeyboardInterrupt) else 2)
    backend.pull.assert_not_called()
    backend.cancel.assert_not_called()


def test_wait_does_not_auto_pull_after_submission(cli, monkeypatch):
    module, factory, backend = cli
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    backend.pull.reset_mock()
    backend.tail.reset_mock()
    backend.status.return_value = RunStatus("succeeded")
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "wait", "experiment", "-n", "trial")
    assert result.value.code == 0
    backend.pull.assert_not_called()
    backend.tail.assert_not_called()


@pytest.mark.parametrize("file_mode", [False, True])
def test_submit_receives_prepared_run_folder_and_configuration(cli, tmp_path, monkeypatch, file_mode):
    module, factory, backend = cli
    source = tmp_path / "experiment"
    (source / "helper.py").write_text("VALUE = 42\n")
    (source / ".encap.conf").write_text("args: '--value configured'\nplugins:\n  fake:\n    bucket: original\n")
    original = (source / ".encap.conf").read_text()
    target = "experiment/run.py" if file_mode else "experiment"
    expected = source / "run" / "trial" if file_mode else tmp_path / "0encap_folder" / "experiment" / "trial"
    def submit(request, writer):
        assert request.run_folder == expected
        assert request.record_dir == expected / ".encap-run"
        assert request.workspace_origin == source.resolve()
        assert not hasattr(request, "source_dir")
        assert not hasattr(request, "source_files")
        assert not hasattr(request, "exclude")
        assert (expected / "run.py").read_text() == "print('hello')"
        assert (expected / "helper.py").exists() is (not file_mode)
        config = yaml.safe_load((expected / ".encap.conf").read_text())
        history = yaml.safe_load((expected / ".encap_history.conf").read_text())
        assert config["backend"] == history["backend"] == "fake"
        assert config["script_name"] == history["script_name"] == request.entrypoint == "run.py"
        assert config["args"] == history["args"] == "--value command-line"
        assert history["plugins"]["fake"]["bucket"] == "original"
        assert writer.read()["api_version"] == backends.API_VERSION
        return RunHandle({"operation": "prepared"})
    backend.submit.side_effect = submit
    invoke(module, monkeypatch, "run", target, "-n", "trial", "-fake", "-a", "--value command-line")
    assert (source / ".encap.conf").read_text() == original


def test_staging_applies_exclusions_and_never_copies_remote_records(cli, tmp_path, monkeypatch):
    module, factory, backend = cli
    source = tmp_path / "experiment"
    (source / ".encap.conf").write_text("rsync_exclude_push: ['*.large', scratch/]\n")
    (source / "input.large").write_text("not staged")
    (source / "scratch").mkdir()
    (source / "scratch" / "data").touch()
    (source / ".encap-run").mkdir()
    (source / ".encap-run" / "private").write_text("not source")
    (source / "nested").mkdir()
    (source / "nested" / "empty").touch()
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    staged = backend.submit.call_args.args[0].run_folder
    assert not (staged / "input.large").exists()
    assert not (staged / "scratch").exists()
    assert not (staged / ".encap-run" / "private").exists()
    assert (staged / "nested" / "empty").stat().st_size == 0


def test_git_tracking_is_resolved_before_plugin_submit(cli, tmp_path, monkeypatch):
    from encap_lib import run_preparation
    module, factory, backend = cli
    (tmp_path / "experiment" / ".encap.conf").write_text("git-track: ['tracked-repo']\n")
    tracker = Mock(return_value={"tracked-repo": "commit-hash"})
    monkeypatch.setattr(run_preparation.git_tracker, "get_current_commit_hashes", tracker)
    def submit(request, writer):
        tracker.assert_called_once_with(["tracked-repo"])
        history = yaml.safe_load((request.run_folder / ".encap_history.conf").read_text())
        assert history["git-track"] == {"tracked-repo": "commit-hash"}
        return RunHandle({})
    backend.submit.side_effect = submit
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")


def test_preparation_failure_never_submits(cli, tmp_path, monkeypatch):
    module, factory, backend = cli
    (tmp_path / "experiment" / "link").symlink_to(tmp_path / "experiment" / "run.py")
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    assert result.value.code == 2
    backend.submit.assert_not_called()
    backend.tail.assert_not_called()


@pytest.mark.parametrize("file_mode", [False, True])
def test_dependency_workspace_reaches_supporting_plugin(cli, tmp_path, monkeypatch, file_mode):
    module, factory, backend = cli
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "data.txt").write_text("snapshot")
    (tmp_path / "experiment" / ".encap.conf").write_text("dependencies: [../src]\n")
    registration = Registration("fake", factory, ("-fake",), supports_dependencies=True)
    monkeypatch.setattr(backends, "discover", lambda: {"fake": registration})
    target = "experiment/run.py" if file_mode else "experiment"
    invoke(module, monkeypatch, "run", target, "-n", "trial", "-fake")
    request = backend.submit.call_args.args[0]
    assert request.experiment_path == "experiment"
    assert request.workspace_folder == request.record_dir / "workspace"
    assert request.workspace_origin == tmp_path.resolve()
    assert request.workspace_dependencies == ("src",)
    assert (request.workspace_folder / "src/data.txt").read_text() == "snapshot"
    assert (request.workspace_folder / "experiment/.encap_history.conf").is_file()
    backend.pull.assert_called_once_with(backend.submit.return_value, request.run_folder)


@pytest.mark.parametrize("scope", ["global", "project", "local"])
def test_top_level_workspace_root_reaches_plugin_and_snapshot(cli, tmp_path, monkeypatch, scope):
    module, factory, backend = cli
    (tmp_path / "src").mkdir()
    (tmp_path / "src/value.txt").write_text("root dependency")
    root = str(tmp_path.resolve())
    text = 'dependencies: ["{root}/src"]\n'
    if scope == "local":
        text += "workspace_root: ..\n"
    elif scope == "global":
        settings.config["workspace_root"] = root
    else:
        settings.config["projects"] = {str(tmp_path): {"workspace_root": root}}
    (tmp_path / "experiment/.encap.conf").write_text(text)
    registration = Registration("fake", factory, ("-fake",), supports_dependencies=True)
    monkeypatch.setattr(backends, "discover", lambda: {"fake": registration})
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    namespace = factory.call_args.args[0]
    assert namespace.workspace_root == tmp_path.resolve()
    assert "workspace_root" not in namespace
    request = backend.submit.call_args.args[0]
    assert request.workspace_dependencies == ("src",)
    assert (request.workspace_folder / "src/value.txt").read_text() == "root dependency"
    history = yaml.safe_load((request.run_folder / ".encap_history.conf").read_text())
    assert history["workspace_root"] == root
    assert history["dependencies"] == ["{root}/src"]


def test_root_dependency_without_core_root_never_prepares_or_submits(cli, tmp_path, monkeypatch):
    module, factory, backend = cli
    (tmp_path / "experiment/.encap.conf").write_text('dependencies: ["{root}/src"]\n')
    registration = Registration("fake", factory, ("-fake",), supports_dependencies=True)
    monkeypatch.setattr(backends, "discover", lambda: {"fake": registration})
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    assert result.value.code == 2
    factory.assert_not_called()
    backend.submit.assert_not_called()
    assert not (tmp_path / "0encap_folder").exists()


@pytest.mark.parametrize("backend_flag", [[], ["-fake"]])
def test_dependencies_reject_unsupported_backends(cli, tmp_path, monkeypatch, backend_flag):
    module, factory, backend = cli
    (tmp_path / "src").mkdir()
    (tmp_path / "experiment" / ".encap.conf").write_text("dependencies: [../src]\n")
    with pytest.raises(SystemExit) as result:
        invoke(module, monkeypatch, "run", "experiment", "-n", "trial", *backend_flag)
    assert result.value.code == 2
    backend.submit.assert_not_called()
    assert not (tmp_path / "0encap_folder").exists()


@pytest.mark.parametrize("api_version", [1, 2])
def test_previous_api_saved_runs_remain_manageable(cli, tmp_path, monkeypatch, api_version):
    module, factory, backend = cli
    invoke(module, monkeypatch, "run", "experiment", "-n", "trial", "-fake")
    writer = RecordWriter(tmp_path / "0encap_folder" / "experiment" / "trial" / ".encap-run")
    writer.update(api_version=api_version)
    backend.status.return_value = RunStatus("succeeded")
    invoke(module, monkeypatch, "status", "experiment", "-n", "trial")
    assert writer.read()["api_version"] == api_version
    backend.status.assert_called_once_with(RunHandle({"operation": "123"}))
