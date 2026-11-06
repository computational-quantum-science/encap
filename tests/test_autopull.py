from unittest.mock import Mock

import pytest
from filelock import FileLock

from encap_lib import autopull
from encap_lib.backends import (
    BackendError, ConfigurationError, RecordWriter, Registration, RunHandle, RunStatus,
)


@pytest.fixture
def registered(tmp_path, monkeypatch):
    root = tmp_path / "daemon"
    root.mkdir()
    monkeypatch.setattr(autopull, "directory", lambda: root)
    backend = Mock()
    backend.status.return_value = RunStatus("running", cleanup_pending=True)
    registration = Registration("fake", Mock(return_value=backend), supports_autopull=True)
    folder = tmp_path / "experiment" / "trial"
    folder.parent.mkdir()
    writer = RecordWriter.reserve(folder, registration, "trial")
    writer.save_handle(RunHandle({"request": "one"}), confirmed=True)
    writer.update(preparation={"source_files": []})
    autopull._write(root / "runs.json", {
        "version": 1, "enabled": True,
        "runs": {str(folder): {"revision": 1, "state": "watching"}},
    })
    return root, folder, registration, backend, writer


def test_active_run_syncs_without_changing_handle_or_verifying_completion(registered):
    root, folder, registration, backend, writer = registered
    before = writer.read()
    autopull.cycle(root, {"fake": registration})
    result = autopull.status(root)["runs"][str(folder)]
    backend.sync.assert_called_once_with(RunHandle({"request": "one"}), folder)
    backend.pull.assert_not_called()
    assert result["state"] == "watching" and result["last_synced_at"]
    assert writer.read() == before


def test_completed_runs_get_final_pull_and_resume_after_rerun(registered):
    root, folder, registration, backend, writer = registered
    backend.status.return_value = RunStatus("succeeded", 0, outputs_finalized=True, cleanup_pending=False)
    autopull.cycle(root, {"fake": registration})
    assert autopull.status(root)["runs"][str(folder)]["state"] == "complete"
    assert backend.sync.call_args.kwargs == {"final": True}
    backend.reset_mock()
    autopull.cycle(root, {"fake": registration})
    backend.sync.assert_not_called()
    writer.save_handle(RunHandle({"request": "two"}), confirmed=True)
    autopull.cycle(root, {"fake": registration})
    assert backend.sync.call_count == 2
    backend.sync.assert_called_with(RunHandle({"request": "two"}), folder, final=True)


@pytest.mark.parametrize("operation", ["sync", "status", "final_sync"])
def test_pull_and_monitoring_errors_are_visible_and_retried(registered, operation):
    root, folder, registration, backend, _ = registered
    backend.status.return_value = RunStatus("failed", 1, cleanup_pending=False)
    if operation == "final_sync":
        def fail_final(*args, **kwargs):
            if kwargs.get("final"):
                raise BackendError("offline")
        backend.sync.side_effect = fail_final
    else:
        getattr(backend, operation).side_effect = BackendError("offline")
    autopull.cycle(root, {"fake": registration})
    result = autopull.status(root)["runs"][str(folder)]
    assert result["state"] == "retrying" and "offline" in result["error"]
    if operation != "sync":
        assert result["last_synced_at"]
    if operation == "final_sync":
        backend.sync.side_effect = None
    else:
        getattr(backend, operation).side_effect = None
    autopull.cycle(root, {"fake": registration})
    assert autopull.status(root)["runs"][str(folder)]["state"] == "complete"


def test_one_unreachable_run_does_not_block_others(registered):
    root, folder, registration, backend, _ = registered
    other = folder.parent / "other"
    writer = RecordWriter.reserve(other, registration, "other")
    writer.save_handle(RunHandle({"request": "two"}), confirmed=True)
    state = autopull._state(root)
    state["runs"][str(other)] = {"revision": 1, "state": "watching"}
    autopull._write(root / "runs.json", state)
    backend.sync.side_effect = [BackendError("offline"), None]
    autopull.cycle(root, {"fake": registration})
    runs = autopull.status(root)["runs"]
    assert runs[str(folder)]["state"] == "retrying"
    assert runs[str(other)]["state"] == "watching"


def test_new_registration_is_not_overwritten_by_an_older_cycle(registered):
    root, folder, registration, backend, _ = registered
    def reregister(*args):
        state = autopull._state(root)
        state["runs"][str(folder)] = {"revision": 2, "state": "watching", "new": True}
        autopull._write(root / "runs.json", state)
    backend.sync.side_effect = reregister
    autopull.cycle(root, {"fake": registration})
    assert autopull.status(root)["runs"][str(folder)]["new"]
    assert "last_synced_at" not in autopull.status(root)["runs"][str(folder)]


def test_submission_lock_defers_synchronization(registered):
    root, folder, registration, backend, _ = registered
    with FileLock(str(folder.parent / f".{folder.name}.submission.lock")):
        autopull.cycle(root, {"fake": registration})
    backend.sync.assert_not_called()
    assert autopull.status(root)["runs"][str(folder)]["state"] == "pending"


def test_watch_registers_existing_run_and_respects_disabled_autostart(registered, monkeypatch):
    root, folder, registration, _, _ = registered
    autopull.stop(root)
    info = autopull.watch(folder, registration, automatic=True)
    assert not info["running"] and not info["enabled"]
    assert info["runs"][str(folder)]["revision"] == 2
    unsupported = Registration("fake", Mock())
    with pytest.raises(ConfigurationError, match="does not support"):
        autopull.watch(folder, unsupported)


def test_missing_backend_or_run_is_reported_without_recreating_it(registered):
    root, folder, registration, _, writer = registered
    autopull.cycle(root, {})
    assert "unavailable" in autopull.status(root)["runs"][str(folder)]["error"]
    writer.path.unlink()
    autopull.cycle(root, {"fake": registration})
    assert "Cannot read run record" in autopull.status(root)["runs"][str(folder)]["error"]
    assert not writer.path.exists()


def test_reused_pid_is_never_signalled(registered, monkeypatch):
    root, _, _, _, _ = registered
    autopull._write(root / "pid.json", {"pid": 123, "created": 10})
    process = Mock()
    process.create_time.return_value = 11
    monkeypatch.setattr(autopull.psutil, "Process", Mock(return_value=process))
    assert not autopull.stop(root)["running"]
    process.terminate.assert_not_called()
    process.kill.assert_not_called()


def test_invalid_registry_does_not_appear_healthy(registered):
    root, _, _, _, _ = registered
    path = root / "runs.json"
    path.write_text("{broken")
    with pytest.raises(BackendError, match="Cannot read"):
        autopull.status(root)


def test_real_daemon_start_idempotence_and_stop(tmp_path):
    root = tmp_path / "service"
    try:
        first = autopull.start(root)
        assert first["running"] and first["enabled"]
        second = autopull.start(root)
        assert second["pid"] == first["pid"]
        assert first["interval_seconds"] == 60
    finally:
        stopped = autopull.stop(root)
    assert not stopped["running"] and not stopped["enabled"]
    assert (root / "daemon.log").exists()
    assert not list(root.glob("*.tmp"))


def test_failed_start_reports_log_location(registered, monkeypatch):
    root, _, _, _, _ = registered
    child = Mock()
    child.poll.return_value = 2
    child.returncode = 2
    monkeypatch.setattr(autopull.subprocess, "Popen", Mock(return_value=child))
    with pytest.raises(BackendError, match="exited with 2; see"):
        autopull.start(root)


def test_registration_recovers_legacy_source_manifest_and_preserves_handles(registered, tmp_path):
    root, folder, registration, _, writer = registered
    source = tmp_path / "source"
    source.mkdir()
    (source / "run.py").write_text("original")
    (folder / "run.py").write_text("saved edit")
    (folder / "results.csv").write_text("outputs are not sources")
    writer.update(preparation={})
    before = writer.read()["handle"]
    autopull.register(folder, registration, source_dir=source)
    record = writer.read()
    assert record["preparation"]["source_files"] == ["run.py"]
    assert record["handle"] == before
    assert str(folder) in autopull._state(root)["runs"]


def test_unknown_backend_status_is_reported_instead_of_stopping_service(registered):
    root, folder, registration, backend, _ = registered
    backend.status.return_value = None
    autopull.cycle(root, {"fake": registration})
    result = autopull.status(root)["runs"][str(folder)]
    assert result["state"] == "retrying"
    assert "invalid computation status" in result["error"]
