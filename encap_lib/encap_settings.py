import os
import yaml
import sys
import encap_lib
import copy
from pathlib import Path
from encap_lib.backends import ConfigurationError, PluginConfig


class DependencyPaths(list):
    """Dependency paths relative to their declaring configuration file."""

    def __init__(self, values, origin):
        super().__init__(values)
        self.origin = origin


def resolve_workspace_root(value):
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError("workspace_root must be a nonempty path string")
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise ConfigurationError("Relative workspace_root requires a declaring configuration file")
    root = root.resolve()
    if not root.is_dir() or root == Path(root.anchor):
        raise ConfigurationError("workspace_root must be an existing non-filesystem-root directory")
    return root


def read_terminal_arguments(pargs):
    global args_config, config, using_slurm, using_pueue

    slurm_conf = encap_lib.slurm.initialize_slurm_settings(pargs)

    if slurm_conf is not None:
        args_config["slurm"] = slurm_conf
        using_slurm = True
        if pargs.i is not None:
            raise ConfigurationError("The -i flag is not supported when using slurm.")
    
    pueue_conf = encap_lib.pueue.initialize_pueue_settings(pargs)
    if pueue_conf is not None:
        args_config["pueue"] = pueue_conf
        using_pueue = True
        if pargs.i is not None:
            raise ConfigurationError("The -i flag is not supported when using pueue.")
        if using_slurm:
            raise ConfigurationError("Cannot use slurm and pueue at the same time.")
    
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
    if not isinstance(config_, dict):
        raise ConfigurationError(f"{file_name}: configuration must be a mapping")
    def plugin_namespaces(mapping):
        for key, value in list(mapping.items()):
            if key == "plugins":
                if not isinstance(value, dict) or any(not isinstance(namespace, dict) for namespace in value.values()):
                    raise ConfigurationError(f"{file_name}: plugins must contain configuration mappings")
                mapping[key] = {
                    name: PluginConfig(namespace, str(Path(file_name).resolve().parent))
                    for name, namespace in value.items()
                }
            elif key == "dependencies":
                if not isinstance(value, list) or any(not isinstance(path, str) or not path.strip() for path in value):
                    raise ConfigurationError(f"{file_name}: dependencies must be a list of nonempty path strings")
                mapping[key] = DependencyPaths(value, str(Path(file_name).resolve().parent))
            elif key == "workspace_root":
                if not isinstance(value, str) or not value.strip():
                    raise ConfigurationError(f"{file_name}: workspace_root must be a nonempty path string")
                mapping[key] = str(PluginConfig({key: value}, Path(file_name).resolve().parent).resolve_path(key))
            elif isinstance(value, dict):
                plugin_namespaces(value)
    plugin_namespaces(config_)
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

def load_encap_config_files_recursive(path, overrides=None):
    """Load all .encap files in parent directories"""
    global config, args_config

    config_file_names = find_encap_config_files(path)
    for file_name in config_file_names:
        config_patch = read_config_file(file_name)
        config = merge_dicts(config, config_patch)
    
    # Reload config with command line arguments in case they overwrite config file settings
    merge_dicts(config, copy.deepcopy(args_config if overrides is None else overrides))

def merge_dicts(dict1, dict2):
    """Merge two dictionaries"""
    for k in dict2:
        if k in dict1 and isinstance(dict1[k], dict) and isinstance(dict2[k], dict):
            merge_dicts(dict1[k], dict2[k])
        else:
            dict1[k] = dict2[k]
            if isinstance(dict1, PluginConfig) and isinstance(dict2, PluginConfig):
                dict1.origins[k] = dict2.origins.get(k)
    return dict1

yaml.add_representer(PluginConfig, lambda dumper, value: dumper.represent_dict(value))
yaml.add_representer(DependencyPaths, lambda dumper, value: dumper.represent_list(value))

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
                "GOOGLE_APPLICATION_CREDENTIALS", "machine_config", "slurm", "pueue"]

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
using_pueue = False