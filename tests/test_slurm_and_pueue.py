import pytest
from unittest.mock import MagicMock
from encap_lib import slurm
from encap_lib import pueue

def test_generate_slurm_variables():
    slurm_settings = {
        "ntasks-per-node": 4,
        "time": "01:00:00",
        "job-name": "test_job"
    }
    
    code = slurm.generate_code_for_slurm_script(
        run_folder_name="/fake/run_folder",
        slurm_settings=slurm_settings,
        executable_file_name="run.sh",
        runslurm_file_name="run.slurm",
        log_file_name="log.slurm",
        job_name="test_job"
    )
    
    assert "#SBATCH --ntasks-per-node=4" in code
    assert "#SBATCH --time=01:00:00" in code
    assert "#SBATCH --job-name=test_job" in code
    assert "srun bash run.sh" in code

def test_pueue_run(mocker):
    # Mock machine
    machine = MagicMock()
    machine.run_code.side_effect = lambda command, **kwargs: (["Success"], 0) if kwargs.get("get_returncode") else ["42"]
    
    pueue_settings = {
        "group": "default"
    }
    
    log_file_name = pueue.run_pueue(
        machine=machine,
        file_extension="py",
        target="run.py",
        args="--my-arg 1",
        run_folder_name="/fake/run_folder",
        target_file="run.py",
        target_file_path="/fake/run_folder/run.py",
        pueue_settings=pueue_settings,
        name="test_pueue_job"
    )
    
    # Check pueue add command was executed
    pueue_command = next(call[0][0] for call in machine.run_code.call_args_list if "pueue add" in call[0][0])
    assert "pueue add" in pueue_command
    assert '--group "default"' in pueue_command
    
    # Check script content from write_file
    script_content = machine.write_file.call_args_list[0][0][1]
    assert "python -u  run.py" in script_content
    tracking_command = next(call.args[0] for call in machine.run_code.call_args_list if "tracking.json" in call.args[0])
    assert "/fake/run_folder/.encap-native/tracking.json" in tracking_command
    assert not any("/pid" in call.args[0] for call in machine.run_code.call_args_list)


def test_slurm_submission_saves_tracking_without_pid(tmp_path, mocker, monkeypatch):
    from tests.test_encap_modes import load_encap
    encap = load_encap(tmp_path)
    machine = MagicMock()
    machine.run_code.side_effect = lambda command, **kwargs: ([], 0) if kwargs.get("get_returncode") else ["Submitted batch job 42"]
    log, jobid = encap.run_slurm(
        machine, "py", "run.py", "", "/fake/run_folder", "run.py", "/fake/run_folder/run.py", {},
    )
    assert (log, jobid) == ("log", "42")
    tracking_command = next(call.args[0] for call in machine.run_code.call_args_list if "tracking.json" in call.args[0])
    assert "/fake/run_folder/.encap-native/tracking.json" in tracking_command
    assert '"kind": "slurm"' in tracking_command
    assert not any("/pid" in call.args[0] for call in machine.run_code.call_args_list)


@pytest.mark.parametrize("scheduler", ["slurm", "pueue"])
def test_scheduler_wrapper_preserves_failure(tmp_path, monkeypatch, scheduler):
    import os
    import subprocess
    import sys
    from encap_lib import encap_settings as settings
    monkeypatch.setitem(settings.config, "interpreter", sys.executable + " -u")
    script = tmp_path / "run.py"
    script.write_text("import sys; print('before failure'); sys.exit(7)")
    generate = slurm.generate_slurm_executable if scheduler == "slurm" else pueue.generate_pueue_executable
    code, _ = generate("py", str(tmp_path), str(script), "", "run.py")
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(code)
    result = subprocess.run(["bash", str(wrapper)], env=dict(os.environ, SLURM_PROCID="0"), capture_output=True)
    assert result.returncode == 7
    log = (tmp_path / "log").read_text()
    assert "before failure" in log
    assert chr(4) in log
