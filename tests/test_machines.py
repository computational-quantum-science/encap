import os
import pytest
from unittest.mock import call
from encap_lib.machines import LocalMachine, SSHMachine

def test_local_machine_run_code(mocker):
    # Mock os.system or run_code_local depending on what LocalMachine.run_code does.
    # Actually, LocalMachine.run_code calls run_code_local, which we can mock.
    mock_run = mocker.patch("encap_lib.machines.run_code_local")
    mock_run.return_value = (["hello"], 0)
    
    local_machine = LocalMachine(local_project_dir="/fake/dir")
    
    # We can test if the command gets wrapped in cd
    out = local_machine.run_code("echo hello", output=True, get_returncode=True)
    
    # Assert
    assert mock_run.call_count == 1
    args, kwargs = mock_run.call_args
    # args[0] should be the command.
    assert "cd /fake/dir" in args[0]
    assert "echo hello" in args[0]
    assert out == (["hello"], 0)

def test_local_machine_rsync_push(mocker):
    # rsync_push sets copy_full_dir appropriately and calls push.
    local_machine = LocalMachine(local_project_dir="/fake/dir")
    
    mock_push = mocker.patch.object(local_machine, "push")
    
    # If name_local ends with "/", copy_full_dir should be False
    local_machine.rsync_push("source_dir/", "target_dir/")
    
    mock_push.assert_called_once_with(
        "source_dir", "target_dir/", directory=True, copy_full_dir=False
    )
    
def test_local_machine_rsync_push_file(mocker):
    local_machine = LocalMachine(local_project_dir="/fake/dir")
    mock_push = mocker.patch.object(local_machine, "push")
    
    # If name_local does not end with "/", copy_full_dir should be True
    local_machine.rsync_push("source_file.txt", "target_dir")
    
    mock_push.assert_called_once_with(
        "source_file.txt", "target_dir", directory=True, copy_full_dir=True
    )

def test_ssh_machine_rsync_push(mocker):
    mock_run = mocker.patch("encap_lib.machines.run_code_local")
    
    ssh_machine = SSHMachine(
        ip="192.168.1.1",
        username="user",
        local_project_dir="/local/dir",
        remote_project_dir="/remote/dir",
        ssh_options="-p 2222",
        rsync_exclude_push=["*.tmp"]
    )
    
    mocker.patch.object(ssh_machine, "run_code")  # Mock mkdir
    
    ssh_machine.rsync_push("source_dir/", "target_dir/")
    
    # Assert that rsync is called with correct options
    assert mock_run.call_count == 1
    command = mock_run.call_args[0][0]
    
    assert "rsync" in command
    assert "--exclude '*.tmp'" in command
    assert "-e \"ssh -p 2222\"" in command
    assert "/local/dir/source_dir/" in command
    assert "user@192.168.1.1:/remote/dir/target_dir" in command
