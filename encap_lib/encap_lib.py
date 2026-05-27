import os
import warnings
import yaml
import encap_lib.encap_settings as settings
from encap_lib.gcloud import GCMachine
import encap_lib.gcloud as gcloud
from encap_lib.machines import SSHMachine, LocalMachine
from datetime import date
# pip install --upgrade google-api-python-client


# Checks if the name of the vm matches the signature in the config file
def is_it_ssh(vm, local_project_dir):
    if "ssh" in settings.config["projects"][local_project_dir]:
        if vm in settings.config["projects"][local_project_dir]["ssh"]:
            return True
    return False


# Checks if the name of the vm matches the signature in the config file
def is_it_gcloud(vm, local_project_dir):
    if "gcloud" in settings.config["projects"][local_project_dir]:
        signature_gcloud = list(settings.config["signature"]["gcloud"].keys())
        is_it = False
        for s in signature_gcloud:
            if s == vm[:len(s)]:
                is_it = True
        return is_it

    return False

def get_machine(vm, local_project_dir):

    if local_project_dir not in settings.config["projects"]:
        raise Exception(f"{local_project_dir} has no configuraion entry in {settings.config_file_name}.")

    ssh_options= ""
    ssh_ignore = []
    sync_files = []
    rsync_exclude = []


    if is_it_ssh(vm, local_project_dir):

        settings_project = settings.get_all_items(["projects", local_project_dir, "ssh", vm])

        ssh_kwargs = {}
        copy_dict_keys = ["ssh_options", "ssh_ignore", "sync_files", "rsync_exclude",
                          "rsync_exclude_push", "rsync_exclude_pull", "sync"]

        for key in copy_dict_keys:
            if settings_project[key] is not None:
                ssh_kwargs[key] = settings_project[key]

        remote_project_dir = settings_project["dir"]
        ip = settings_project["ip"]
        username = settings_project["user"]

        machine = SSHMachine(ip=ip, username=username, local_project_dir=local_project_dir,
                            remote_project_dir=remote_project_dir, **ssh_kwargs)

    elif is_it_gcloud(vm, local_project_dir):

        settings_project = settings.get_all_items(["projects", local_project_dir, "gcloud"])

        if settings_project['GOOGLE_APPLICATION_CREDENTIALS'] is not None:
            os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = settings_project['GOOGLE_APPLICATION_CREDENTIALS']

        with warnings.catch_warnings():
            # pip install --upgrade google-api-python-client
            warnings.simplefilter("ignore")
            import apiclient.discovery
            gcloud.compute = apiclient.discovery.build('compute', 'v1')
            gcloud.instances = gcloud.compute.instances()

        if settings_project["ssh_options"] is not None:
            ssh_options = settings_project["ssh_options"]

        if settings_project["ssh_ignore"] is not None:
            ssh_ignore = settings_project["ssh_ignore"]

        if settings_project["sync_files"] is not None:
            sync_files = settings_project["sync_files"]

        if settings_project["rsync_exclude"] is not None:
            rsync_exclude = settings_project["rsync_exclude"]


        remote_project_dir = settings_project["dir"]
        username = settings_project["user"]
        zone = settings_project["zone"]
        machine_config = settings_project["machine_config"]
        project = settings_project["project"]
        nfs = settings_project["nfs"]

        machine = GCMachine(vm=vm, username=username, local_project_dir=local_project_dir,
                            remote_project_dir=remote_project_dir, ssh_options=ssh_options,
                            ssh_ignore=ssh_ignore, sync_files=sync_files, rsync_exclude=rsync_exclude,
                            zone=zone, project=project, machine_config=machine_config, nfs_name=nfs)


    else:
        raise Exception(f"The VM name '{vm}' does not match any of the ssh instances nor the gcloud signature.")

    machine.settings = settings_project
    
    return machine

def get_interpreter_from_file_extension(f, ignore_file_extensior_if_interpreter_set_in_settings=False, error_if_not_found=True):
    if ignore_file_extensior_if_interpreter_set_in_settings:
        if "interpreter" in settings.config:
            return settings.config["interpreter"]
    
    if f == "":
        return ""
    
    if f in settings.config["file_extension"]:
        return settings.config["file_extension"][f]
    
    if error_if_not_found:
        raise ValueError(f"{f} is not a file extension that is present in your config file.")
    
    return ""
    
    

def filename_and_file_extension(a):
    a_split = a.split(".")
    # If there is no .
    if len(a_split) == 1:
        return a, ""
    else:
        file_extension = a_split[-1]
        file_without_extension = a[:-len(file_extension) - 1]
        return file_without_extension, file_extension

def extract_folder_name(path):
    path_split = os.path.split(path)
    return  os.path.join(*path_split[:-1]), path_split[-1]

def record_process(vm, name, i):
    remove_process_from_database(name, i)

    with open(settings.running_processes_file, 'r') as ymlfile:
        running = yaml.load(ymlfile, Loader=yaml.FullLoader)

    running["running"] += [[vm, name, i, date.today()]]
    with open(settings.running_processes_file, 'w') as yml_file:
        yaml.dump(running, yml_file, default_flow_style=False)

def remove_process_from_database(name, i):
    """
    Remove a process from the running processes file.
    args: name: name of the process
            i: index of the process
    """
    try:
        with open(settings.running_processes_file, 'r') as ymlfile:
            running = yaml.load(ymlfile, Loader=yaml.FullLoader)
    except FileNotFoundError:
        running = {"running": []}

    # Go backwards so that the deleteion does not mess with the indices
    for j in range(len(running["running"]))[::-1]:
        item = running["running"][j]
        if item[1] == name and item[2] == i:
            del running["running"][j]

    with open(settings.running_processes_file, 'w') as yml_file:
        yaml.dump(running, yml_file, default_flow_style=False)

def get_all_processes():
    """
    Get all the processes that are currently running.
    """
    try:
        with open(settings.running_processes_file, 'r') as ymlfile:
            running = yaml.load(ymlfile)
    except FileNotFoundError:
        running = {"running": []}

    return running["running"]

def changedir(s):
    os.chdir(os.path.dirname(os.path.realpath(s)))
