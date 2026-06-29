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
    machine.run_code.return_value = (["Success"], 0)
    
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
    pueue_command = machine.run_code.call_args_list[-1][0][0]
    assert "pueue add" in pueue_command
    assert '--group "default"' in pueue_command
    
    # Check script content from write_file
    script_content = machine.write_file.call_args_list[0][0][1]
    assert "python -u  run.py" in script_content
