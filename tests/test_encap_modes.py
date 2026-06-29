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
    test_file.write_text("print('hello from file mode')")
    
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
    
    assert "hello from file mode" in log_file.read_text()

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
