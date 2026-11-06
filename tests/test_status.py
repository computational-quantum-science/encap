import os
import subprocess
import sys
from dataclasses import asdict
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from encap_lib.machines import LocalMachine
from encap_lib.backends import MonitoringError, RunStatus
from encap_lib.status import get_status, print_cloud_status, print_daemon_status, _sync_age


@pytest.mark.parametrize("width", [80, 120])
def test_cloud_status_table_preserves_long_ids_and_fits_terminal(monkeypatch, capsys, width):
    monkeypatch.setattr("encap_lib.status.shutil.get_terminal_size", lambda fallback: os.terminal_size((width, 24)))
    job_id = "experiment-original_ranking_gcp_20261103_v1-4g4wqjqj5etmm"
    cluster = "ml-cluster-4g4wqjqj5etmm"
    status = RunStatus("running", details={
        "job_id": job_id, "job": {"stateStartTime": "2026-11-03T10:57:09.479978+01:00"},
        "placement": {"clusterName": cluster}, "labels": {"encap-request-id": "not-for-the-table"},
        "link": "https://example.com/job",
    })
    before = asdict(status)
    print_cloud_status("dataproc", [status], {"project": "test-project", "region": "europe-west1"},
                       now=datetime(2026, 11, 3, 11, 57, 10, tzinfo=timezone.utc))
    output = capsys.readouterr().out
    assert "Backend: dataproc | Project: test-project | Region: europe-west1" in output
    assert "| Job ID" in output
    assert "Status" in output
    assert "Elapsed" in output
    assert "Since (UTC)" not in output
    assert "Cluster" in output
    assert "Running" in output
    assert "02h 00m 00s" in output
    assert "2026-11-03" not in output
    lines = output.splitlines()
    assert all(len(line) <= width for line in lines)
    rows = [line.split("|") for line in lines if line.startswith("| ") and "Job ID" not in line]
    assert "".join(row[1].strip() for row in rows) == job_id
    assert "".join(row[4].strip() for row in rows) == cluster
    assert "not-for-the-table" not in output
    assert "https://" not in output
    assert asdict(status) == before


def test_cloud_status_table_handles_missing_optional_details(capsys):
    print_cloud_status("example", [RunStatus("submitted", details={"job_id": "000123"})], {})
    output = capsys.readouterr().out
    assert "Backend: example" in output
    assert "000123" in output
    assert "Submitted" in output
    assert "| -" in output


@pytest.mark.parametrize("timestamp,expected", [
    ("2026-11-04T11:59:59Z", "00h 00m 01s"),
    ("2026-11-04T11:59:00Z", "00h 01m 00s"),
    ("2026-11-04T11:00:00Z", "01h 00m 00s"),
    ("2026-11-04T10:57:09.479978Z", "01h 02m 50s"),
    ("2026-11-04T11:57:09.479978+01:00", "01h 02m 50s"),
    ("2026-11-03T12:00:00Z", "1d 00h 00m 00s"),
    ("2026-11-02T09:55:55Z", "2d 02h 04m 05s"),
    ("2026-11-04T12:00:00Z", "00h 00m 00s"),
    ("2026-11-04T12:00:01Z", "00h 00m 00s"),
    (None, "-"),
])
def test_cloud_status_shows_elapsed_duration(capsys, timestamp, expected):
    status = RunStatus("running", details={"job": {"stateStartTime": timestamp}})
    print_cloud_status("dataproc", [status], {},
                       now=datetime(2026, 11, 4, 12, tzinfo=timezone.utc))
    lines = capsys.readouterr().out.splitlines()
    row = next(line.split("|") for line in lines if line.startswith("| ") and "Elapsed" not in line)
    assert row[3].strip() == expected


@pytest.mark.parametrize("width", [80, 120, 240])
@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize("started", ["2026-11-04T09:57:09Z", "2026-10-01T09:57:09Z"])
def test_cloud_status_table_shows_experiment_and_run_name(monkeypatch, capsys, width, multiple, started):
    monkeypatch.setattr("encap_lib.status.shutil.get_terminal_size", lambda fallback: os.terminal_size((width, 24)))
    source = "experiments/objectives_and_eligibility/quantity_ranking_original_framework"
    name = "original_ranking_gcp_20261103_v1"
    status = RunStatus("running", details={
        "source": source, "name": name, "instance": 7, "multiple_instances": multiple,
        "job_id": "opaque-provider-job-id", "placement": {"clusterName": "ml-cluster-4g4wqjqj5etmm"},
        "job": {"stateStartTime": started},
    })
    print_cloud_status("dataproc", [status], {}, now=datetime(2026, 11, 4, 12, tzinfo=timezone.utc))
    lines = capsys.readouterr().out.splitlines()
    assert "| Experiment" in "\n".join(lines)
    assert "opaque-provider-job-id" not in "\n".join(lines)
    assert all(len(line) <= width for line in lines)
    rows = [line.split("|") for line in lines if line.startswith("| ") and "Experiment" not in line]
    assert "".join(row[1].strip() for row in rows) == source
    assert "".join(row[2].strip() for row in rows) == name + ("_7" if multiple else "")


@pytest.mark.parametrize("timestamp", ["not-a-timestamp", "2026-11-03T10:57:09", 123])
def test_cloud_status_table_reports_invalid_timestamps(timestamp):
    with pytest.raises(MonitoringError, match="timestamp"):
        print_cloud_status("example", [
            RunStatus("running", details={"job": {"stateStartTime": timestamp}}),
        ], {})


@pytest.mark.parametrize("details", [{"job": None}, {"placement": []}])
def test_cloud_status_table_reports_invalid_details(details):
    with pytest.raises(MonitoringError, match="job or placement"):
        print_cloud_status("example", [RunStatus("running", details=details)], {})


@pytest.fixture
def daemon_info():
    return {
        "running": True, "pid": 123, "enabled": True, "interval_seconds": 60,
        "last_cycle_at": "2026-11-03T14:36:52+00:00",
        "log": "/Users/example/.encap/daemon/daemon.log",
        "runs": {
            "/saved/experiment/original_ranking_gcp_20261103_v1": {
                "backend": "dataproc", "revision": 1, "state": "watching",
                "run_state": "running", "error": None,
                "last_synced_at": "2026-11-03T14:36:20+00:00",
                "handle_fingerprint": "internal-fingerprint",
            },
            "/saved/experiment/other_trial": {
                "state": "watching", "run_state": "running",
                "last_synced_at": "2026-11-03T15:35:00+01:00",
            },
        },
    }


@pytest.mark.parametrize("width", [80, 120])
def test_daemon_status_shows_only_necessary_details(daemon_info, monkeypatch, capsys, width):
    monkeypatch.setattr("encap_lib.status.shutil.get_terminal_size", lambda fallback: os.terminal_size((width, 24)))
    before = deepcopy(daemon_info)
    print_daemon_status(daemon_info, now=datetime(2026, 11, 3, 14, 37, tzinfo=timezone.utc))
    output = capsys.readouterr().out
    assert "Daemon: running (PID 123) | Auto-pull: on | Interval: 60s" in output
    assert all(header in output for header in ["Run", "State", "Sync", "Last sync"])
    assert "Running" in output and "OK" in output
    assert "40s ago" in output and "2m ago" in output
    lines = output.splitlines()
    assert all(len(line) <= width for line in lines)
    rows = [line.split("|") for line in lines if line.startswith("| ") and "Last sync" not in line]
    assert "".join(row[1].strip() for row in rows) == "original_ranking_gcp_20261103_v1other_trial"
    for hidden in ["/saved/", "fingerprint", "revision", "2026-11-03", "daemon.log", "Errors"]:
        assert hidden not in output
    assert daemon_info == before


@pytest.mark.parametrize("running,enabled", [(False, False), (False, True), (True, False)])
def test_daemon_status_shows_paused_pulls(daemon_info, capsys, running, enabled):
    daemon_info.update(running=running, enabled=enabled)
    print_daemon_status(daemon_info)
    output = capsys.readouterr().out
    assert "Paused" in output
    assert ("Daemon: running" if running else "Daemon: stopped") in output
    assert ("Auto-pull: on" if enabled else "Auto-pull: paused") in output
    if not running:
        assert "PID" not in output


@pytest.mark.parametrize("state,error,synced,expected", [
    ("pending", None, None, "Waiting"),
    ("complete", None, "2026-11-03T14:36:20Z", "Complete"),
    ("retrying", "Permission denied", None, "Retrying"),
    ("watching", "Permission denied", None, "Error"),
])
def test_daemon_status_shows_sync_states(daemon_info, capsys, state, error, synced, expected):
    entry = next(iter(daemon_info["runs"].values()))
    entry.update(state=state, error=error, last_synced_at=synced)
    daemon_info["runs"] = {"/saved/trial": entry}
    print_daemon_status(daemon_info)
    output = capsys.readouterr().out
    assert expected in output
    if error:
        assert "Permission denied" in output
        assert "encap daemon status --json" in output
    else:
        assert "Errors" not in output


def test_daemon_status_handles_empty_registry(daemon_info, capsys):
    daemon_info["runs"] = {}
    print_daemon_status(daemon_info)
    assert "No watched runs." in capsys.readouterr().out


def test_daemon_status_disambiguates_duplicate_names(daemon_info, capsys):
    daemon_info["runs"] = {"/project_a/experiment/trial": {}, "/project_b/experiment/trial": {}}
    print_daemon_status(daemon_info)
    output = capsys.readouterr().out
    assert "/project_a/experiment/trial" in output
    assert "/project_b/experiment/trial" in output


def test_daemon_status_summarizes_long_errors(daemon_info, capsys):
    entry = next(iter(daemon_info["runs"].values()))
    entry.update(state="retrying", error="Permission denied: " + "details " * 100)
    daemon_info["runs"] = {"/saved/trial": entry}
    print_daemon_status(daemon_info)
    output = capsys.readouterr().out
    assert "Permission denied:" in output
    assert "..." in output
    assert "encap daemon status --json" in output
    assert len(output) < 1000


@pytest.mark.parametrize("timestamp,expected", [
    (None, "-"),
    ("2026-11-03T14:37:00Z", "just now"),
    ("2026-11-03T14:36:59Z", "1s ago"),
    ("2026-11-03T14:36:01Z", "59s ago"),
    ("2026-11-03T14:36:00Z", "1m ago"),
    ("2026-11-03T13:37:00Z", "1h ago"),
    ("2026-11-02T14:37:00Z", "1d ago"),
])
def test_sync_age(timestamp, expected):
    assert _sync_age(timestamp, datetime(2026, 11, 3, 14, 37, tzinfo=timezone.utc)) == expected


@pytest.mark.parametrize("timestamp", ["invalid", "2026-11-03T14:37:00", 123])
def test_daemon_status_reports_invalid_timestamps(daemon_info, timestamp):
    next(iter(daemon_info["runs"].values()))["last_synced_at"] = timestamp
    with pytest.raises(MonitoringError, match="timestamp"):
        print_daemon_status(daemon_info)


def status_machine(system):
    machine = Mock()
    header = "  PID STARTED                      COMMAND"
    processes = [
        "101 Wed Oct 21 10:00:00 2026 python experiment.py",
        "102 Wed Oct 21 10:00:00 2026 tee experiment.log",
        "103 Wed Oct 21 10:00:00 2026 python experiment.py",
        "104 Wed Oct 21 10:00:00 2026 python unrelated.py",
    ]
    environments = [
        "ENCAP_NAME=experiment ENCAP_PROCID=0 ENCAP_LOG=/tmp/a folder/log=value HOME=/tmp",
        "ENCAP_NAME=experiment ENCAP_PROCID=0 HOME=/tmp",
        "ENCAP_NAME=experiment ENCAP_PROCID=1 HOME=/tmp",
        "HOME=/tmp",
    ]
    if system == "Darwin":
        machine.run_code.side_effect = [
            [system],
            [header] + [
                f"{process} {env}"
                for process, env in zip(processes, environments)
            ],
        ]
    else:
        machine.run_code.side_effect = [
            [system],
            [header] + processes,
            [
                env.replace(" HOME=", "\x00HOME=")
                .replace(" ENCAP_", "\x00ENCAP_") + "\x00encap_end"
                for env in environments
            ],
        ]
    return machine


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_status_process_environments(system):
    machine = status_machine(system)

    status = get_status(machine)

    assert set(status) == {"101", "103"}
    assert status["101"]["ENCAP_NAME"] == "experiment"
    assert status["101"]["ENCAP_PROCID"] == "0"
    assert status["101"]["ENCAP_LOG"] == "/tmp/a folder/log=value"
    assert status["101"]["PID"] == "101"
    assert len(status["101"]["TIME"].split(":")) == 3
    command = machine.run_code.call_args_list[1].args[0]
    assert "LC_ALL=C" in command
    if system == "Darwin":
        assert "ps eww" in command
        assert machine.run_code.call_count == 2
    else:
        assert "args" in command
        assert "/proc/101/environ" in machine.run_code.call_args.args[0]


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize("process_id", [1, "1", [1]])
def test_status_filters_run_and_instance(system, process_id):
    status = get_status(
        status_machine(system), run_folder_name="experiment", id=process_id
    )

    assert set(status) == {"103"}
    assert get_status(status_machine(system), run_folder_name="different") == {}


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_status_without_processes(system):
    machine = Mock()
    machine.run_code.side_effect = [[system], ["PID STARTED COMMAND"]]

    assert get_status(machine) == {}
    assert machine.run_code.call_count == 2


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires BSD ps")
def test_macos_status_reads_live_process_environment(tmp_path):
    env = {
        **os.environ,
        "ENCAP_NAME": "encap-status-regression",
        "ENCAP_PROCID": "7",
        "ENCAP_LOG": str(tmp_path / "a folder" / "log=value"),
    }
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        env=env,
        stdin=subprocess.PIPE,
    )
    try:
        status = get_status(
            LocalMachine(local_project_dir=str(tmp_path)),
            run_folder_name=env["ENCAP_NAME"],
            id=7,
        )

        assert str(process.pid) in status
        assert status[str(process.pid)]["ENCAP_LOG"] == env["ENCAP_LOG"]
    finally:
        process.communicate(timeout=10)
