import encap_lib.encap_settings as settings
from encap_lib.encap_lib import get_interpreter_from_file_extension
import sys
import os
import warnings

def ensure_daemon_running(machine, auto_start=True):
    """
    Ensure the pueue daemon is running.
    Checks `pueue status`. If it fails, checks if `pueued` is installed.
    If installed and auto_start is True, starts `pueued -d`.
    """
    out, rcode = machine.run_code("pueue status", get_returncode=True, ignore_errors=True)
    if rcode == 0:
        return True # Daemon is already running
    
    # Check if pueued is installed
    out_which, rcode_which = machine.run_code("which pueued", get_returncode=True, ignore_errors=True)
    if rcode_which != 0 or out_which.strip() == "":
        print("Error: pueued is not installed or not in PATH. Please install pueue.")
        sys.exit(1)
        
    if auto_start:
        machine.run_code("pueued -d", verbose=True)
    else:
        print("Error: pueue daemon is not running and auto_start is disabled. Run `pueued -d`.")
        sys.exit(1)

def generate_pueue_executable(file_extension, run_folder_name, target_file_path, args, target_file, pueue_instance=0, interpreter_args=""):
    """
    Generate pueue wrapper script. Mirrors slurm's generate_slurm_executable.
    """
    # If the interpreter has not been specified, get it from the file extension
    interpreter = get_interpreter_from_file_extension(file_extension, ignore_file_extensior_if_interpreter_set_in_settings=True)

    # Chech if the files is executable, if yes then run it directly
    if os.access(target_file_path, os.X_OK):
        run_the_experiment = f"""bash -c "time ./{target_file} {args} 2>&1 | tee -a /dev/null" &>> $log"""

        # Give warning if an interpreter is set in the settings file
        if "interpreter" in settings.config:
            warnings.warn(f"WARNING: 'interpreter' is set in the settings files, but your file is executable. The interpreter in the (settings file)/(command line) will be ignored.", UserWarning)
    else:
        run_the_experiment = f"""bash -c "time {interpreter} {interpreter_args} {target_file} {args} 2>&1 | tee -a /dev/null" &>> $log"""

    args = args.replace("{i}", f"{pueue_instance}")
        
    code = f'''#!/bin/bash
    export ENCAP_NAME="{run_folder_name}"
    export ENCAP_PROCID={pueue_instance}
    cd {run_folder_name}
    
    # If $ENCAP_PROCID is 0, then the log file is called log
    if [ "$ENCAP_PROCID" == "0" ]
    then
        log="log"
    else
        log="log_$ENCAP_PROCID"
    fi

    date &>> $log
    echo "host: $(hostname)" &>> $log

    echo "Encap Proc Id: $ENCAP_PROCID" &>> $log

    echo "{target_file_path} {args}" &>> $log
    echo "" &>> $log
    {run_the_experiment}
    echo {chr(4)} &>> $log
    '''
    return code, args

def initialize_pueue_settings(pargs):
    pueue_settings = {}
    
    if pargs.pu_i is not None:
        pueue_settings["i"] = pargs.pu_i
        if not isinstance(pueue_settings["i"], int):
            pueue_settings["i"] = list(pueue_settings["i"])
            
    if pargs.pu_pt is not None:
        pueue_settings["parallel_tasks"] = pargs.pu_pt
        
    if len(pueue_settings) == 0:
        if not pargs.pueue:
            return None
            
    return pueue_settings

def read_pueue_settings_from_encapconfig(vm, local_project_dir, pueue_settings=None):
    if pueue_settings is None:
        pueue_settings = {}
    
    try:
        if vm is not None:
            pueue_settings2 = settings.get_item("pueue", ["projects", local_project_dir, "ssh", vm])
        else:
            pueue_settings2 = settings.get_item("pueue", ["projects", local_project_dir])
    except KeyError:
        if "pueue" in settings.config:
            pueue_settings2 = settings.config["pueue"]
        else:
            pueue_settings2 = {} # Pueue settings are optional, auto_start is true by default
            
    pueue_settings2.update(pueue_settings)
    
    # Default auto_start is true if not specified
    if "auto_start" not in pueue_settings2:
        pueue_settings2["auto_start"] = True
        
    return pueue_settings2

def run_pueue(machine, file_extension, target, args, run_folder_name, target_file, target_file_path, pueue_settings, interpreter_args="", name=None):
    # Ensure daemon is running
    ensure_daemon_running(machine, auto_start=pueue_settings.get("auto_start", True))
    
    # Delete previous log file and create a new one
    machine.run_code(f"""rm {run_folder_name}/pid -f
    rm {run_folder_name}/.pueue_files/log* -f
    touch {run_folder_name}/log
    mkdir -p {run_folder_name}/.pueue_files
    """)
    if not ("i" in pueue_settings):
        pueue_settings["i"] = 1
    
    if isinstance(pueue_settings["i"], int):
        iterator = range(pueue_settings["i"])
    else:
        iterator = pueue_settings["i"]
        
    log_file_name = ""
    group = pueue_settings.get("group", "default")
    
    # Configure group parallel tasks if requested
    if "parallel_tasks" in pueue_settings:
        machine.run_code(f"pueue parallel {pueue_settings['parallel_tasks']} -g {group}")
        
    for i in iterator:
        if i == 0:
            pueue_instance_text = ""
        else:
            pueue_instance_text = f"_{i}"
            
        if log_file_name == "":
            log_file_name = f"log{pueue_instance_text}"
            machine.run_code(f"""rm {run_folder_name}/{log_file_name} -f
                                 touch {run_folder_name}/{log_file_name }""")
                                 
        executable_file_name = f"{run_folder_name}/.pueue_files/run{pueue_instance_text}.sh"
        
        # Script to run the file and save outputs in log
        code, args_replace = generate_pueue_executable(file_extension, run_folder_name, target_file_path, args, target_file=target_file, pueue_instance=i)
        
        # Save the script in the run_folder
        machine.write_file(executable_file_name, code, verbose=True)
        
        # Define job name
        job_name = f"{target}/{name}{pueue_instance_text}"
        if len(args_replace) > 0:
            args_ = args_replace.replace(" ", "_")
            if len(args_) != 0 and args_[0] == "_":
                args_ = args_[1:]
            job_name = f"{job_name}_{args_}"
            
        # Add to pueue
        # We execute the bash wrapper script using pueue
        add_command = f"pueue add --label \"{job_name}\" --group \"{group}\" -- bash {executable_file_name}"
        machine.run_code(add_command, verbose=True)
        
    # We don't save a PID in the pid file because `encap tail` will just wait for chr(4).
    # Return the log file name.
    return log_file_name
