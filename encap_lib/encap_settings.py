import os
import yaml
import sys
import encap_lib
import copy

def read_terminal_arguments(pargs):
    global args_config, config, using_slurm

    slurm_conf = encap_lib.slurm.initialize_slurm_settings(pargs)

    if slurm_conf is not None:
        args_config["slurm"] = slurm_conf
        using_slurm = True
        assert pargs.i == None, "The -i flag is not supported when using slurm."
    
    if pargs.i != None:
        if not isinstance(pargs.i, int):
            pargs.i = list(pargs.i)
        
        args_config["i"] = pargs.i
    else:
        args_config["i"] = 1
    
    if pargs.args is not None:
        args_config["args"] = pargs.args
    
    if pargs.script_name is not None:
        args_config["script_name"] = pargs.script_name
    
    if pargs.interpreter is not None:
        args_config["interpreter"] = pargs.interpreter
    
    config = merge_dicts(config, copy.deepcopy(args_config))


def read_config_file(file_name):
    with open(file_name, 'r') as ymlfile:
        try:
            config_ = yaml.load(ymlfile, Loader=yaml.SafeLoader)
        except yaml.YAMLError as exc:
            print("Error in configuration file")
            sys.exit(exc)
    if config_ is None:
        config_ = dict()
    
    return config_

def write_config_file(file_name, settings, comment=""):
    with open(file_name, 'w') as ymlfile:
        if comment != "":
            ymlfile.write(f"# {comment}\n")
        
        yaml.dump(settings, ymlfile, default_flow_style=False)

def find_encap_config_files(path):
    """Find all .encap files in parent directories"""
    config_file_names = []
    rest = None
    while rest != "":
        path, rest = os.path.split(path)
        file_path = os.path.join(path, rest, ".encap.conf")
        if os.path.isfile(file_path):
            config_file_names.append(file_path)
        
    return config_file_names[::-1]

def load_encap_config_files_recursive(path):
    """Load all .encap files in parent directories"""
    global config, args_config

    config_file_names = find_encap_config_files(path)
    for file_name in config_file_names:
        config_patch = read_config_file(file_name)
        config = merge_dicts(config, config_patch)
    
    # Reload config with command line arguments in case they overwrite config file settings
    merge_dicts(config, copy.deepcopy(args_config))

def merge_dicts(dict1, dict2):
    """Merge two dictionaries"""
    for k in dict2:
        if k in dict1 and isinstance(dict1[k], dict) and isinstance(dict2[k], dict):
            merge_dicts(dict1[k], dict2[k])
        else:
            dict1[k] = dict2[k]
    return dict1

def get_item(item, config_location):
    global config
    cc = config
    out = None
    for loc in config_location:
        if item in cc:
            out = cc[item]
        cc = cc[loc]
    if item in cc:
        out = cc[item]
    return out

def get_all_items(config_location):
    out = dict()
    for item in config_items:
        out[item] = get_item(item, config_location)
    return out


# Code to help read the config file
home = os.path.expanduser("~")

config_folder = os.path.join(home, ".encap")
temp_file = os.path.join(config_folder, "temp")

config_file_name = os.path.join(config_folder, "config.yml")

running_processes_file = os.path.join(home, ".encap", "running_processes.yml")

config_items = ["dir", "ip", "sync", "user", "ssh_ignore", "ssh_options", "sync_files",
                "rsync_exclude", "rsync_exclude_push", "rsync_exclude_pull",
                "project", "zone", "nfs", "machine_config",
                "GOOGLE_APPLICATION_CREDENTIALS", "machine_config", "slurm"]

default_extensions = {"py": "python -u", "sh": "bash", "jl": "julia"}

# If .encap does not exist
if not os.path.isdir(config_folder):
    os.mkdir(config_folder)

# If .encap/config.yml does not exist
if not os.path.isfile(config_file_name):
    config = {"file_extension": default_extensions}
    with open(config_file_name, 'w') as ymlfile:
        yaml.dump(config, ymlfile, default_flow_style=False)


path = os.getcwd()

# Open config file
config = read_config_file(config_file_name)
if not ("file_extension" in config):
    print("There are no registered file_extension in you config file. The default ones were added!")
    config["file_extension"] = default_extensions
    with open(config_file_name, 'w') as ymlfile:
        yaml.dump(config, ymlfile, default_flow_style=False)

args_config = dict()

debug = False
dryrun = False
using_slurm = False