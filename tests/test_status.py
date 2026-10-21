import os
import subprocess
import sys
from unittest.mock import Mock

import pytest

from encap_lib.machines import LocalMachine
from encap_lib.status import get_status


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
