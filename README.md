# Encap: A Simple Tool for Managing Computational Experiments

<p align="center">
<img src="https://user-images.githubusercontent.com/53435922/217352989-c400e86c-31e0-40cb-a734-004e5994dda8.svg" width="200"/>
</p>

Encap is a user-friendly tool designed to help you manage and keep track of your computational experiments, especially in the field of scientific computing. With Encap, you can easily run various experiments in separate containers and maintain a record of the results.

## Features

Encap currently supports:

- Re-running old experiments
- Tracking git repositories
- Running multiple experiments in parallel
- Running experiments on Slurm
- Running experiments remotely via SSH

Note that Encap is currently only compatible with Linux/macOS.

## Running a Script with Encap

Instead of running a script using the standard command:

```bash
python scripts/my_script.py
```

You can run it with Encap by typing:

```
encap run scripts/my_script.py -n <version_name>
```

This command creates a folder named `scripts/my_script/<version_name>` and copies the script inside. The script is then automatically executed using the following command:

```
setsid nohup time python scripts/my_script/<version_name>/my_script.py &>> scripts/my_script/<version_name>/log & disown
```

As the experiment runs, the log file is displayed in the terminal, making it easy and convenient to monitor different computing experiments.

### Python Example

Consider the following simple Python experiment:

#### my_script.py

```python
import pickle

save_name = "test_data.p"
print(save_name)

# Generate some data
data = ["Some", "test", "data"]

# Save the data
pickle.dump(data, open(save_name, "wb"))
```

By running:

```
encap run scripts/my_script.py -n test
```

You'll get the output:

```
PID 23968
Sat Sep 14 01:03:28 CEST 2019
scripts/test/my_script.py

test_data.p
```

And the following three files will be generated:

- my_script/test/log
- my_script/test/my_script.py
- my_script/test/test_data.p

## Installation

To install Encap, use the following command:

```bash
pip install encap
```

## Accessing Help

```bash
encap -h
```

## Re-running a Previous Experiment

To re-run a previous experiment, use Encap in rerun mode:

```bash
encap rerun scripts/my_script.py -n test
```

This command re-runs the experiment without copying the script again. This is useful if you want to re-run an old experiment with different parameters. For instance, you can copy the script (using the copy command), modify the parameters, and then rerun the updated script.

## Following an Experiment with `encap tail`

If you started an experiment in another shell, or detached from a remote `-vm` run while the job is still going on the cluster, you can re-attach to its log later:

```bash
encap tail scripts/my_script.py -n test
```

This streams the log in real time and exits cleanly when the experiment finishes — either when the script writes its end-of-transmission marker on normal exit, or, for Slurm jobs, when the job leaves the queue (so `scancel`, walltime kills, and OOMs are detected too). For experiments that have already completed, the full log is dumped once and `encap tail` returns.

Combine with `-vm <machine_name>` to follow a remote job.

## Running Multiple Experiments in Parallel

#### my_script.py

```python
import pickle
import os

i = os.environ["ENCAP_PROCID"]
save_name = f"test_data_{i}.p"
print(save_name)
print("This is the", i, "encap instance")

pickle.dump(["Some", "test", "data", i], open(save_name, "wb"))
```

To run the script three times in parallel, use the following command:

```bash
encap run scripts/my_script.py -i 3 -n test
```

This command generates the following files:

- my_script/test/my_script.py
- my_script/test/log
- my_script/test/log_1
- my_script/test/log_2
- my_script/test/test_data_0.p
- my_script/test/test_data_1.p
- my_script/test/test_data_2.p

## More Examples

Several examples can be found in the examples folder.

You can find more examples in the `examples` folder.

## Choosing a Script Interpreter

There are three ways to choose the interpreter for executing your script:

1. Set a custom file extension in the configuration file located in `~/.encap/config.yml` (see Sec. Nested Configurations)

```yml
file_extension:
  go: go run
```

2. Make your script executable, which will execute it directly.
3. Pass your desired interpreter as a command-line argument:

```sh
encap run my_script.go -n <version> --interpreter "go run"
```

## Archive Mode - tar/untar

To tar an experiment, use the following command:

```bash
encap tar scripts/my_script.py -n test
```

This command creates a tar.gz file of the experiment folder and saves it. To untar the experiment, use the following command:

```bash
encap untar scripts/my_script.py -n test
```

If all experiments should be tarred remove the `-n` argument.

```bash
encap untar scripts/my_script.py
```

## Configuring Slurm

Encap can also work with Slurm. To run your experiment using Slurm, execute the following command:

```
encap run slurm_test.py -n test -sl
```

If you want to run three experiments in parallel, use this command:

```
encap run slurm_test.py -n test -sl_i 3
```

This command launches three different Slurm jobs and passes the `ENCAP_PROCID` environment variable to the script. In this example, the `ENCAP_PROCID` will take the values 0, 1, 2 if ntasks-per-node has been configured to be one. If you run:

```sh
encap run slurm_test.py -n test -sl_i 3 -sl_ntasks 20
```

60 jobs will be launched in total, and the `ENCAP_PROCID` will take the values 0-59 respectively.
See `/examples/slurm_test.py` for more details.

The configuration file is located at `~/.encap/config.yml`.

Example config file for a Slurm job:

```yml
file_extension:
  py: python -u

slurm:
  account: <account>
  partition: <partition>
  cpus-per-task: 256 # How many CPUs to allocate to each of your experiments
  ntasks-per-node: 1 # How many copies of your experiment to start per node
  time: "24:00:00" # Time until your job is terminated by Slurm
```

If you want to execute different Slurm instances in parallel, use the `-sl_i <i>` argument. This will create _i_ different Slurm jobs.

### Adavnced Configurations

You can create custom code to be executed within the Slurm file. For example, you may need to restart a Slurm job if it doesn't complete successfully within the allowed time. Here's a sample configuration file that restarts the Slurm job up to two additional times if it fails to complete:

```yml
slurm:
  account: <account>
  partition: <partition>
  cpus-per-task: 256 # How many CPUs to allocate to each of your experiments
  ntasks-per-node: 1 # How many copies of your experiment to start per node
  time: "24:00:00" # Time until your job is terminated by Slurm
  code:
    - export SLURM_RESTARTNR=$((SLURM_RESTARTNR + 1))
    - timeout 23.5h srun bash {run.sh} # Time until the job is terminated by timeout
    - if [ $? -eq 124 ] && [ $SLURM_RESTARTNR -lt 3 ]; then # Number of repetitions 3 here
    - sbatch {run.slurm}
    - fi
```

This configuration is useful if your job needs to run for longer than the maximum time allowed by your Slurm system. The job will be run up to three times in total, including two restarts, and it is up to you to save and reload the current state of your computational experiment.

In this example, `{run.sh}` and `{run.slurm}` will be replaced by encap with the actual script and Slurm file automatically upon execution.

## Nesting Configuration Files

Sometimes you may want to have different configuration files for different projects or even different experiments. Encap will recursively search for files named `.encap.conf` in the script's directory and all parent directories. Each `.encap.conf` file will be merged with the previous one, allowing you to have a global configuration file and overwrite only parts of it for specific projects or experiments. For example, you can set the default Slurm partition to "gpu" in a global configuration file and overwrite it for a specific project that doesn't require a GPU. See the `examples/slurm_folder_script_extra_configs` folder for more examples.

## Folder Mode

If you need to run a script that relies on other files within the same folder, you can use the folder mode. This mode duplicates the entire folder into the experiment folder and then runs the main script. This is particularly helpful if you have a custom .encap.conf file in the folder that you want to use for the experiment or if your script depends on a configuration file located in the same folder. The folder mode is automatically activated if you provide a folder instead of a script to Encap. For example:

```bash
encap run examples/folder_script -n test
```

This command will copy the entire folder to the experiment folder and then execute the script called `run.*` in the folder.

Note that the script name can be different from `run.*` if it is specified with the `-f` argument, or if the folder contains a `.encap.conf` file with the `script_name` field set to the name of the script.

## Tracking Git Repositories

If you want to keep track of the commit in a git repository, add the following to your `.encap.conf` file:

```yml
git-track:
  - <repo_dir_1>
  - <repo_dir_2>
```

This configuration will write the commit hash of the current commit in the repository to the `.encap_history.conf` file in the experiment folder. This can be helpful if you want to keep track of the exact commit used for a specific experiment, in case you need to reproduce the results in the future.

## Force Commit Changes to Git (experimental)

If you often forget to commit your changes before performing a simulation, you can use `git-track-force`. This feature creates a new branch called `encap`. The `encap` branch is always automatically kept up to date with the current state of your git project. This is done by committing any changes on the `encap` branch and saving the hash of this commit in your experiment folder. This process ensures that you can always go back to the moment in time when you performed the experiment. Note that this procedure uses worktrees to ensure that your main branch remains untouched.

```yml
git-track-force: <repo_dir_3>
```

## Configuring SSH

You can execute scripts on a remote server through SSH. Encap mirrors the project folder on the remote, runs the experiment there, and pulls results back when it finishes — all in a single command.

```bash
encap run scripts/my_script.py -n <version_name> -vm <machine_name>
```

The same `-vm` flag works with `rerun` and `tail`. It can be combined with the Slurm flags to submit a Slurm job on the remote in one step.

Transfers in both directions use `rsync`, so only changed files cross the network and mtimes are preserved. If you ever need to bypass the change-detection (for example after a clock-skew incident on the remote), use `-fp` / `--force-push` to transfer regardless of timestamps:

```bash
encap run scripts/my_script.py -n <version_name> -vm <machine_name> -fp
```

The configuration file is located at `~/.encap/config.yml`:

```yml
file_extension:
  py: python -u
  sh: bash

projects:
  <dir_in_local_machine>:
    dir: <dir_in_remote_machine>
    ssh:
      user: <username>
      <machine_name>:
        ip: <ip>
```

### Ignore SSH output

```yml
ssh_ignore: ["X11 forwarding request failed on channel"]
```

### Excluding files from rsync

Push (local → remote) and pull (remote → local) have separate exclude lists, since they usually have opposite intent.

```yml
rsync_exclude_push: [".git", "__pycache__", "*.hdf5"] # don't ship up
rsync_exclude_pull: ["__pycache__"] # don't bring back
```

The legacy single `rsync_exclude` key is still accepted and is treated as an alias for `rsync_exclude_push`.

## Configuring Google Cloud

TODO

That's an overview of the main features and configurations for the Encap tool. You can use it to efficiently manage your computational experiments and ensure that your results are reproducible. If you have any questions, or want to contribute to the project, feel free to leave an issue.
