import os
import subprocess
import shutil
import importlib.util
import sys
import pytest

def load_encap(tmp_path):
    encap_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "encap"))
    
    encap_py_path = tmp_path / "encap_main.py"
    shutil.copy(encap_path, encap_py_path)
    
    sys.path.insert(0, str(tmp_path))
    
    # Reload modules to prevent state leakage
    if "encap_main" in sys.modules:
        del sys.modules["encap_main"]
        
    import encap_main
    from encap_lib import encap_settings
    import importlib
    importlib.reload(encap_settings)
    
    return encap_main

def test_file_mode(tmp_path, mocker, monkeypatch):
    encap = load_encap(tmp_path)
    
    # Mock tail_pull so it doesn't hang on Mac
    mocker.patch.object(encap, 'tail_pull', return_value=None)
    
    # Setup test file
    test_file = tmp_path / "test_script.py"
    test_file.write_text("import os; print('hello from file mode'); print(f'ENCAP_LOG={os.environ.get(\"ENCAP_LOG\")}')")
    
    # Change cwd to tmp_path and patch sys.argv
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["encap", "run", "test_script.py", "-n", "test_file_run"])
    
    encap.main()
    
    # Check if run folder is created
    run_folder = tmp_path / "test_script" / "test_file_run"
    assert run_folder.exists()
    
    # Check log output
    log_file = run_folder / "log"
    assert log_file.exists()
    # It takes a bit of time for bash to write the log in background
    # But wait, encap.main() returns immediately because tail_pull is mocked!
    # So we might need to sleep slightly to let the background job finish
    import time
    time.sleep(0.5)
    
    log_content = log_file.read_text()
    assert "hello from file mode" in log_content
    assert f"ENCAP_LOG={log_file.absolute()}" in log_content

def test_folder_mode(tmp_path, mocker, monkeypatch):
    encap = load_encap(tmp_path)
    mocker.patch.object(encap, 'tail_pull', return_value=None)
    
    # Setup test folder
    test_dir = tmp_path / "my_module"
    test_dir.mkdir()
    
    test_script = test_dir / "run.py"
    test_script.write_text("print('hello from folder mode')")
    
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["encap", "run", "my_module", "-n", "test_folder_run"])
    
    encap.main()
    
    run_folder = tmp_path / "0encap_folder" / "my_module" / "test_folder_run"
    assert run_folder.exists()
    
    import time
    time.sleep(0.5)
    
    log_file = run_folder / "log"
    assert log_file.exists()
    assert "hello from folder mode" in log_file.read_text()


@pytest.mark.parametrize("code", [0, 7])
def test_local_wait_preserves_real_exit_code(tmp_path, mocker, monkeypatch, code):
    encap = load_encap(tmp_path)
    mocker.patch.object(encap, 'tail_pull', return_value=None)
    test_script = tmp_path / "wait_script.py"
    test_script.write_text(f"import sys; print('finished'); sys.exit({code})")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["encap", "run", "wait_script.py", "-n", "trial"])
    encap.main()
    from encap_lib import native_wait
    original_wait = native_wait.wait
    mocker.patch.object(native_wait, "wait", side_effect=lambda machine, folder: original_wait(machine, folder, interval=0.01))
    monkeypatch.setattr(sys, "argv", ["encap", "wait", "wait_script.py", "-n", "trial"])
    with pytest.raises(SystemExit) as result:
        encap.main()
    assert result.value.code == code
    folder = tmp_path / "wait_script" / "trial"
    assert (folder / ".encap-native" / "exit").read_text().strip() == str(code)
    assert (folder / ".encap-native" / "tracking.json").is_file()
    assert not (folder / "pid").exists()
    assert not list(folder.glob(".encap-exit*"))
    assert not (folder / ".encap-native.json").exists()


def test_local_instances_rerun_and_tail_use_native_metadata(tmp_path, mocker, monkeypatch):
    import json
    from encap_lib import native_wait
    from encap_lib.machines import LocalMachine
    encap = load_encap(tmp_path)
    from encap_lib import encap_settings as settings
    monkeypatch.setattr(settings, "config", dict(settings.config))
    monkeypatch.setattr(settings, "args_config", {})
    tail = mocker.patch.object(encap, "tail_pull", return_value=None)
    source = tmp_path / "instances.py"
    source.write_text("import os, sys; sys.exit(int(os.environ['ENCAP_PROCID']))")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "encap", "run", "instances.py", "-n", "trial", "-i", "[2, 5]", "-int", sys.executable,
    ])
    encap.main()
    folder = tmp_path / "instances" / "trial"
    machine = LocalMachine(local_project_dir=str(tmp_path))
    assert native_wait.wait(machine, "instances/trial", interval=0.01) == 2
    tracking, _ = native_wait.read_tracking(machine, "instances/trial")
    assert [item["instance"] for item in tracking["identifiers"]] == ["2", "5"]
    assert (folder / ".encap-native" / "exit_5").read_text().strip() == "5"
    assert not (folder / "pid").exists()
    (folder / "pid").write_text("99999\n")
    (folder / ".encap-native.json").write_text(json.dumps({
        "version": 1, "kind": "process", "identifiers": [{"id": "99999", "instance": "0"}],
    }))
    (folder / ".encap-exit").write_text("0\n")
    monkeypatch.setattr(sys, "argv", ["encap", "tail", "instances.py", "-n", "trial"])
    encap.main()
    assert tail.call_args.args[2] == tracking["identifiers"][0]["id"]
    assert tail.call_args.kwargs["log_file_name"] == "log_2"
    assert native_wait.wait(machine, "instances/trial", interval=0.01) == 2

    (folder / "instances.py").write_text(
        "import os, sys, time; time.sleep(0.1); sys.exit(9 if os.environ['ENCAP_PROCID'] == '2' else 0)"
    )
    monkeypatch.setattr(sys, "argv", ["encap", "rerun", "instances.py", "-n", "trial", "-i", "[2, 5]"])
    encap.main()
    assert native_wait.wait(machine, "instances/trial", interval=0.01) == 9
    assert (folder / ".encap-native" / "exit_5").read_text().strip() == "0"
    assert (folder / "pid").read_text() == "99999\n"
    assert (folder / ".encap-exit").read_text() == "0\n"
    assert not list((folder / ".encap-native").glob("*.tmp"))


def test_persistent_pid_tail_stops_on_log_completion(tmp_path, mocker, monkeypatch):
    encap = load_encap(tmp_path)
    machine = mocker.Mock()
    monkeypatch.setattr(encap.platform, "system", lambda: "Linux")
    encap.tail_pull(machine, "trial", pid="7")
    command = machine.run_code.call_args.args[0]
    assert "tail --pid=7 " in command
    assert f'== "{chr(4)}"' in command
    assert "trap 'kill $TAIL_PID" in command
    assert "/pid" not in command
    machine.pull.assert_called_once_with("trial", "trial", directory=True)
