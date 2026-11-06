# Writing an Encap plugin

Create a separate Python package. Keep provider dependencies and orchestration
there, not in Encap. Plugins are trusted installed code, not a sandbox.

## Execution plugins and waiting

Encap 0.6 supports independently installed execution backends. Install a plugin
in the **same Python environment** as Encap; for example `encap-dataproc` registers
`-gcp` without Google-specific changes to Encap core.

```sh
encap run ./experiment -n trial -gcp
encap wait ./experiment -n trial
encap tail ./experiment -n trial
encap status ./experiment -n trial
encap pull ./experiment -n trial
encap kill ./experiment -n trial
encap ssh ./experiment -n trial
```

`ssh` opens an interactive terminal on a supporting backend's saved run.
Dataproc connects to the actual master instance discovered from its workflow
and cluster, using the saved project/region rather than current configuration.
For multi-instance runs, select an ID with `--instance ID`; add
`--tunnel-through-iap` when an IAP tunnel is required. Run `htop` after connecting.
Exiting or interrupting SSH does not cancel the experiment or pull outputs.
SSH requires the local `.encap-run` record and a live, provisioned cluster.

For backends supporting cloud discovery, omit `-n` to list active jobs directly
from the provider, even without local run records:

```sh
encap status ./experiment -gcp
encap status ./experiment       # infer the only configured listing backend
encap status -gcp               # use configuration from the current directory
```

An explicit selector or configured `backend` takes precedence. Otherwise Encap
selects the only installed listing-capable backend with a namespace in `plugins`;
multiple candidates require a selector. Native VM/Slurm/Pueue selection disables
this inference. The target supplies configuration, not an experiment-name filter:
Dataproc lists active Encap jobs visible to the selected identity in the configured
project and region, filtering by the `encap-request-id` label and ignoring jobs
submitted by other tools. No dependency staging or submission validation is
performed. Listings use a compact Pueue-style table with experiment source path,
run name, status, elapsed time in the current state and cluster. Elapsed time is
shown as `02h 15m 03s` (with a day count for runs lasting at least a day);
missing timestamps display `-`. Raw timestamps remain available with `--json`.
Source paths and
names come from the job's saved GCS request metadata, including for existing runs
without local records; the selected identity needs read access to that metadata.
Long paths and names wrap instead of being truncated. Empty listings print `No active jobs.`
Use `encap status ./experiment -gcp --json` for the full machine-readable output
with `backend` and a `runs` list of serialized `RunStatus` values, including links
and provider details; an empty JSON listing has `runs: []`.
Provider/authentication/partial-listing errors fail with code 2 rather than
appearing as an empty result. Other plugins can opt into this capability as
described below.
Named-run status (`-n`) retains its detailed JSON output.

Backends can register their own CLI options. For example, Dataproc owns
`-gcp_i`; like Slurm's `-sl_i`, it accepts a count, explicit IDs or a range:

```sh
encap run ./experiment -n trial -gcp_i 4
encap run ./experiment -n selected -gcp_i '[0, 2, 7]'
encap run ./experiment -n ranged -gcp_i 'range(2, 5)'
```

Supplying a plugin CLI option also selects its backend; `-gcp` is optional with
`-gcp_i`. Core passes the parsed value into that plugin's configuration namespace
and saves the override in the run configuration/history. It does not interpret
instance counts, expand ranges, or orchestrate multiple submissions.
Dataproc owns those semantics and uses `plugins.dataproc.i`, defaulting to one
instance (ID 0); `-gcp_i` overrides it. The former top-level `backend_instances`
setting is rejected for new runs: move it to `plugins.dataproc.i`.
See [the Dataproc documentation](../encap-dataproc/README.md) for its multi-machine
submission, output merging and recovery behavior.

Only explicitly selected plugin runs (a selector or configured `backend`) use
the plugin interface. Built-in local/SSH/Slurm/Pueue execution remains in place.
Plugin `run` and supported `rerun` save the confirmed submission, then **follow logs automatically**,
matching Encap's default run-and-tail behavior. Ctrl-C detaches without cancelling
the job. `run`, supported `rerun` and reattached `tail` automatically pull outputs when
log-following finishes normally. Detaching or a tailing error skips that final
foreground pull, but does not stop independently registered automatic pulls.
Use `wait` to wait without logs or downloads, and `pull` to download explicitly.

Before calling a plugin, core creates the normal local experiment directory,
copies the selected source, performs configured git tracking and generates
`.encap.conf` and `.encap_history.conf`. Plugins receive this prepared
`RunRequest.run_folder`; they package/execute it rather than reading the original
source directory. Folder/file layouts and configuration generation are shared
with Encap's normal preparation. Internal `.encap-run` records are not uploaded.
The public plugin API is version 3; existing API 1 and 2 run records remain
manageable without modifying their saved handles.

Use the existing `.encap.conf` hierarchy and `~/.encap/config.yml`:

```yaml
backend: example  # optional default; an explicit selector wins for new runs
plugins:
  example:
    provider_setting: value
```

Core passes only the selected namespace to the plugin, with the shared
`workspace_root` available as namespace metadata. Each plugin documents
its own settings and validates them. A saved run always selects its recorded
backend and settings, even after configuration changes. Omit selectors on
management commands. Keep the run folder's `.encap-run` metadata when moving
or archiving results; loss of that record prevents reliable reconnection.
Folder outputs retain `0encap_folder/<source>/<name>`; file outputs retain
`<source-without-extension>/<name>`.

### Source dependencies

Supporting backends (currently Dataproc) accept a top-level list:

```yaml
dependencies:
  - ../../src
  - ../shared_experiment
```

For paths relative to a project root, configure the generic top-level
`workspace_root`:

```yaml
workspace_root: /Users/alcalde/projects
dependencies:
  - "{root}/encap_experiemnts/src"
```

`workspace_root` may also be relative to its declaring configuration file.
It follows normal global/project/local configuration inheritance; a child value
overrides its parent. The effective root must be an existing directory other
than the filesystem root. `{root}/` is expanded only at the start of dependency
paths and requires this top-level setting. Quote these entries in YAML.
Empty paths, `.`/`..` components and unknown/misplaced placeholders are rejected.
Ordinary relative and absolute dependencies keep their existing meanings.
Dataproc also uses this root for GCS input destinations, worker paths and mirrored
storage. Legacy `plugins.dataproc.workspace_root` remains supported, but if both
settings are present they must resolve to the same directory.

Entries select files or directories. Plain relative paths resolve from the
configuration file declaring the list, including inherited/global configuration.
A child list replaces the
parent list; `dependencies: []` clears it. Only the selected source and dependencies
are copied. Their common ancestor determines their layout, not what gets copied.
Dependencies are snapshots, not executed experiments; their own dependency lists
are not followed. Missing/excluded paths and symlinks are errors.
Secrets, caches, virtual environments and internal run records are excluded.
Python import paths are not changed automatically.

Core saves the workspace under the run's `.encap-run/workspace`, keeping normal
source/output locations unchanged. It preserves source-relative dependency
locations; the backend defines where the prepared experiment executes.
Dataproc relocates it to its saved-run path in the worker workspace, retaining
dependencies at their original locations. Its scripts should use the worker root
for dependency paths rather than assuming the source directory is the cwd.
The flattened local output directory alone is not a dependency workspace.
Built-in execution and plugins without dependency support reject nonempty lists
rather than silently ignoring them.

`wait` polls every five seconds, streams no logs and pulls nothing. Ctrl-C in a
plugin `wait`/`tail` detaches with code 130, never cancelling remote work.
Only explicit `kill` requests cancellation. Successful plugin waiting also
requires output finalization; pending infrastructure cleanup is reported
separately. Unknown state and connection errors are monitoring errors, not
successful completion.

`wait` also works for new built-in runs, locally or through `-vm`. It uses saved
exit results for OS processes, Slurm accounting (`sacct`, with `squeue` for
pending visibility), and Pueue task IDs/JSON status. It never interprets scheduler
IDs as local OS PIDs or treats disappearance as success. Runs created before
0.6 lack reliable completion metadata and cannot be waited on this way.
All recorded instances must complete. Built-in Slurm/Pueue wrappers now preserve
the computation's exit code rather than masking it with the final log marker.

CLI exit codes: 0 for confirmed success, a valid remote nonzero code (1-255)
when available, 1 for failure without a valid code, 130 for cancellation or
client interrupt, and 2 for configuration/plugin/monitoring errors.

Plugin runs reject conflicting selectors, VM/scheduler/native `-i` options,
and unsupported copy/archive/push/dry-run modes before submission. `rerun`
requires a backend that explicitly supports it.
An unconfirmed submission is never automatically resubmitted. Consult the plugin's
recovery instructions rather than reusing the run name.

For an existing plugin run, `run` checks its remote status and prompts to
overwrite only after computation is confirmed succeeded, failed or cancelled.
`-y` accepts the prompt automatically, but never bypasses the status check.
By default, overwriting deletes the previous local run, including outputs and
its remote handle. Set `backup_on_overwrite: true` in `.encap.conf` or
`~/.encap/config.yml` to preserve it as `<name>.backup-<unique-suffix>` instead.
Management commands can use that backup name with `-n`.
Active, unknown, unreachable and unconfirmed runs cannot
be overwritten. Overwriting submits new work; it does not cancel or delete any
old remote resources.

## Register your backend

In your package's `pyproject.toml`:

```toml
[project]
name = "encap-example"
version = "0.1.0"
dependencies = ["encap>=0.6,<0.7"]

[project.entry-points."encap.backends"]
example = "encap_example:registration"
```

In `encap_example/__init__.py`:

```python
from encap_lib.backends import CLIOption, Registration

def factory(config):
    from .backend import ExampleBackend
    return ExampleBackend(config)

registration = Registration(
    name="example", selectors=("-example",), factory=factory,
    api_version=3, plugin_version="0.1.0",
    cli_options=(CLIOption(
        flags=("-example_queue",), key="queue", help="Select the provider queue",
    ),),
)
```

Keep registration lightweight: no SDK imports, authentication or remote calls.
Install the package in the **same environment** as Encap; its selector then
appears automatically in `encap --help`.
`CLIOption.key` names a setting in `plugins.example`. Its optional `parse`
callable defaults to `str`; the plugin can supply a converter that validates
provider-specific syntax and raises `argparse.ArgumentTypeError` on invalid input.
CLI values override inherited configuration and are preserved in run history.
Only one backend's options may be supplied, for `run` or a supported `rerun`.

### Rerunning saved experiments

Encap 0.6.2 adds the optional API 3 capability
`Registration.supports_rerun=True`. Supporting backends implement
`rerun(request, handle, writer) -> RunHandle`.

```sh
encap run ./experiment -n trial -gcp_i '[0]'
encap rerun ./experiment -n trial -sn run.py -gcp_i '[1, 2, 3]'
```

Core keeps the existing run directory and record. It loads saved configuration,
applies edits to the run's `.encap.conf` and CLI overrides, and prepares the saved
experiment files without recopying the original source. Dependency workspaces
are rebuilt using saved dependency locations; supporting workspace backends also
apply push exclusions when packaging reruns without dependencies. Legacy runs
with relative dependency paths need their original declaring configuration, or
absolute/`{root}/` paths in the saved `.encap.conf`.

The backend receives the previous durable handle and must return a handle that
retains all unselected instances. New IDs extend the run; selected completed IDs
may be restarted. Reject selected active, uncertain or cleanup-pending work
before submitting a replacement, and retain every recovery handle after partial
failure. `status`, `wait`, `tail`, `pull` and `kill` continue using the combined
saved handle. Monitoring clients attached before a rerun must reconnect if the
record changes; they cannot overwrite newer recovery handles.

Dataproc shows only the first instance selected by the latest launch, including
reruns, and saves that selection for `tail` reattachment. It never mixes logs or
switches to older instances. After that instance finishes, remaining jobs are
waited for quietly before the final download. Status, `wait`, downloads and
cancellation still cover the complete group. Ctrl-C only detaches.

Dataproc implements this capability for mirrored and legacy storage. Rerun does
not perform a local overwrite or delete the cloud run. Existing output collision
protection still applies. `-gcp_i 1` is a count (ID 0); use `'[0]'` for explicit
ID zero. With no selection override, the saved instance configuration is reused.

### Automatic cloud pulls

Encap 0.6.3 adds the optional API 3 capability
`Registration.supports_autopull=True`. Dataproc supports it. Supporting `run`
and `rerun` submissions, including partial submissions with saved handles,
automatically register their local run directory with a per-user daemon.
It pulls available outputs every **60 seconds** using the saved backend and
pull exclusions. Local/SSH/Slurm/Pueue behavior is unchanged.

```sh
encap watch ./experiment -n trial  # register an existing saved cloud run
encap daemon status               # compact daemon summary and per-run sync table
encap daemon status --json        # full metadata, paths, timestamps and errors
encap daemon stop                 # pause and disable automatic restarting
encap daemon start                # resume all registered runs
```

The daemon survives terminal closure and Ctrl-C detachment. It watches all
registered local cloud runs, not unrelated provider jobs with no local record.
After a run is terminal and cleanup completes, it takes a stable final snapshot
and stops polling that unchanged handle. The daemon remains available for new
runs and reruns. `complete` in daemon status describes synchronization, not
successful computation; `run_state` still reports failed or cancelled jobs.
Errors are recorded per run and retried on later cycles without blocking
other runs. `stop` does not cancel remote jobs or discard registrations.
New submissions remain registered but do not restart an explicitly stopped daemon.
`watch` explicitly resumes automatic pulls.

State and logs live under `~/.encap/daemon/`; per-run errors and last successful
sync times appear in `encap daemon status`, and diagnostics in
`~/.encap/daemon/daemon.log`. After reboot, use `encap daemon start` or a new
submission to resume; no login/system service is installed. While the machine
is asleep or offline no downloads occur, and authentication requiring interaction
must be refreshed in a terminal.

The status table shows each run's name, computation state, synchronization
state and time since its last successful pull. Internal revisions, fingerprints,
absolute paths and raw timestamps are omitted; duplicate run names use their
full paths to remain unambiguous. Errors are summarized below the table.
Use `--json` for the complete, unchanged status structure.

Supporting backends implement `sync(handle, destination, final=False)`.
Live synchronization must tolerate active outputs changing without claiming a
consistent final copy; `final=True` must retain strict download validation.
It must use saved pull exclusions, never delete local/remote files, and preserve
saved source/input files, `.encap.conf`, `.encap_history.conf` and `.encap-run`.
New run records include `preparation.source_files` for this purpose. Registering
legacy runs recovers that list from the original source directory; keep that
directory available until the first registration. Other output files can be
refreshed from the cloud as with an explicit pull. File locks prevent competing
downloads and avoid syncing while a submission/rerun is being prepared.
Daemon monitoring does not replace saved execution handles.

Only files already uploaded by the worker can be downloaded. Dataproc uploads
log snapshots periodically but normally uploads other outputs at finalization.
Automatic log downloads use temporary files and atomic replacement, retrying
generation changes without removing a previous local snapshot. Unchanged log
generations are not downloaded again.
Live snapshots and deliberate exclusions remain recorded in `retrieval.json`;
they are not full-run integrity verification. Explicit `pull` retains its existing
behavior, including downloading source/configuration files.

### Optional interactive SSH

Set `Registration.supports_ssh=True` and implement
`ssh(handle, instance=None, tunnel_through_iap=False)`. The default is unsupported;
existing plugins and saved handle formats are unchanged. Core reconnects with
empty configuration and passes the saved handle plus the SSH-only CLI options.
The backend must resolve the actual machine, reject ambiguous/unknown instance
IDs, and inherit the terminal's standard streams for interactive programs.
Connection failures must raise `BackendError`; interrupts must not cancel work.
Google-specific SSH routing stays in the Dataproc plugin.

### Optional cloud discovery

Set `Registration.list_runs` to a callable accepting the selected `PluginConfig`
and returning `list[RunStatus]`. This additive API 3 capability does not change
saved handles or require existing plugins to implement discovery. Keep the
registration callback lightweight and import provider implementation lazily,
as with `factory`.

The callback must discover active jobs directly from its provider, paginate
through all results and raise `BackendError` (usually `MonitoringError`) on
authentication, permission, connection or incomplete-listing errors. It receives
current configuration, not a saved handle, and should validate only the settings
needed for listing. Include provider job IDs, raw state, placement and useful
links in each status's `details`. Do not infer output finalization from job
discovery; leave those fields unknown unless verified.
Optional `details.source` and `details.name` strings select the table's Experiment
and Name columns instead of the provider Job ID column. With
`details.multiple_instances=True`, the name includes the `details.instance` suffix.

## Implement the contract

Import shared types and errors from `encap_lib.backends`:

| Method | Responsibility |
|---|---|
| `submit(request, writer) -> RunHandle` | Package the prepared run folder, submit work and return its reference. |
| `rerun(request, handle, writer) -> RunHandle` | Optional: run selected instances from the saved experiment, preserving unselected handles. |
| `status(handle) -> RunStatus` | Report computation state, exit code and output/cleanup details. |
| `tail(handle)` | Stream available logs; Ctrl-C only detaches. |
| `pull(handle, destination)` | Download outputs without deleting unrelated files or overwriting `.encap-run`. |
| `sync(handle, destination, final=False)` | Optional: background output snapshots using saved exclusions and preserving local source/configuration. |
| `cancel(handle)` | Explicitly request cancellation. |

`RunRequest.run_folder` is an **already-created local experiment directory**.
Core copies the selected folder contents (or only the target file in file mode),
applies source exclusions, performs git tracking, and writes `.encap.conf` and
`.encap_history.conf` before calling `submit`. Package this folder, not the
original source. Exclude internal `.encap-run` records from remote uploads, but
include the generated experiment configuration/history.

The request also supplies the relative entrypoint, arguments, environment,
run ID, record directory and optional interpreter. `record_dir` is internal
metadata inside `run_folder`; it is not computation input.
Instance selection, parsing, validation and multi-instance orchestration belong
to the plugin, using its own configuration namespace. There is no instance field
in `RunRequest` and no instance-specific registration API.
Set `Registration.supports_dependencies=True` only if the backend implements
workspace packaging/execution. With dependencies, `RunRequest.workspace_folder`
contains the prepared workspace, and `RunRequest.experiment_path` is its
POSIX-relative experiment directory. Package the workspace; a backend may relocate
the prepared experiment to its saved-run path while preserving dependency
locations. Run from the chosen experiment directory and collect outputs only
from that directory. Without dependencies these fields are `None` and `"."`, preserving
the existing `run_folder` contract. These are additive API 2 fields.
`RunRequest.workspace_origin` is optional, provider-independent path metadata:
the original local directory represented by the prepared workspace root, or the
original source directory when no dependency workspace was needed. It lets a
plugin preserve paths relative to a configured root without inferring source
locations from saved-run names. It is not another source tree to read or upload;
the prepared folder/workspace remains the only input. Plugins needing this
metadata must reject its absence explicitly; other plugins can ignore it.
`RunRequest.workspace_dependencies` lists explicitly selected dependency paths,
POSIX-relative to that origin. This metadata lets a plugin distinguish dependency
files from prepared experiment files, including overlaps and single-file targets,
when relocating the experiment. Its default is an empty tuple for older callers.
`RunRequest.source_path` optionally records the original source file or directory
as an absolute path for display metadata. It is not another tree to package:
plugins must still execute the prepared snapshot. Dataproc stores its path
relative to the configured workspace root (or submission working directory) in
the cloud request so discovery can display the source and full run name.
Keep a durable handle for every potentially accepted submission and never
automatically submit missing instances after partial submission failure.
Supplying any registered plugin CLI option also selects its backend.
The factory receives a `PluginConfig` mapping containing only `plugins.example`.
Its `workspace_root` attribute is the resolved top-level root (`Path` or `None`),
not an injected plugin setting. Backends may use this shared metadata without
accepting additional keys in their own configuration namespace.
Use `config.resolve_path("key")` for local configuration-relative paths.

**Save recovery information before submission:** call
`writer.save_handle(RunHandle({...}))` with request IDs and resolved non-secret
settings before a request could be accepted. Return the confirmed handle afterward.
Handles must be JSON-serializable; never include credentials or access tokens.

A backend can mark the local record with `writer.update(state="not-submitted")`
while it is certain that no submission request has started. With no saved handle,
this allows an explicit `run` retry after local overwrite confirmation.
`save_handle` ends this retryable phase; never leave `not-submitted` without a
handle when a remote request could have been accepted. This is a local record
state, not a `RunStatus` state.

On reconnect the factory receives empty configuration. Use the saved handle,
not current defaults. Reconcile uncertain submissions without automatically
resubmitting; updates to `handle.data` are persisted by core.
Errors include the saved run's `encap status` command when a recovery handle
exists. Without a handle, check provider job/workflow history; only after
confirming no submission was accepted should the local run folder be moved to
an unused backup path, preserving its recovery metadata, before retrying.

States are `submitted`, `running`, `succeeded`, `failed`, `cancelled` and `unknown`.
Report success only after output saving succeeds. Core supplies five-second
polling for `wait`; do not implement a separate wait loop. Raise
`ConfigurationError`, `MonitoringError` or `BackendError` explicitly.
API version 3, record version 1 and provider handle versions are independent.
API 2 replaces `source_dir` with `run_folder` and removes `source_files`/`exclude`:
source selection and staging belong to core. Old API 1 plugin registrations
must be updated. API 3 removes `RunRequest.instances` and `instance_selectors`,
replacing instance-specific core logic with generic `CLIOption` forwarding.
Plugins using API 2 must update their registrations. Saved API 1 and 2 runs
remain manageable with unchanged handles.

Test discovery, submission recovery, lifecycle operations and failures with
fakes/mocks, without cloud credentials. For a complete implementation, see the
separate [`encap-dataproc` package](../encap-dataproc/README.md).
