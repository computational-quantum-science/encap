"""Public, provider-independent execution backend API (version 3)."""

import json
import math
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Tuple

from filelock import FileLock

API_VERSION = 3
RECORD_VERSION = 1
POLL_INTERVAL = 5.0
STATES = frozenset(("submitted", "running", "succeeded", "failed", "cancelled", "unknown"))


class BackendError(Exception):
    """An actionable plugin, record or operation error."""


class ConfigurationError(BackendError):
    pass


class MonitoringError(BackendError):
    pass


class PluginConfig(dict):
    """A namespace whose path values retain their declaring configuration directory."""

    def __init__(self, values=(), origin=None):
        super().__init__()
        self.origins = {}
        for key, value in dict(values).items():
            self[key] = PluginConfig(value, origin) if isinstance(value, dict) else value
            self.origins[key] = origin

    def resolve_path(self, key):
        value = self[key]
        if not isinstance(value, str):
            raise ConfigurationError(f"{key} must be a path string")
        path = Path(value).expanduser()
        if not path.is_absolute():
            origin = self.origins.get(key)
            if origin is None:
                raise ConfigurationError(f"No configuration origin for relative path {key}")
            path = Path(origin) / path
        return path.resolve()


@dataclass(frozen=True)
class RunRequest:
    run_folder: Path
    entrypoint: str
    arguments: List[str]
    environment: Dict[str, str]
    run_id: str
    record_dir: Path
    interpreter: Optional[str] = None
    workspace_folder: Optional[Path] = None
    experiment_path: str = "."


@dataclass(frozen=True)
class RunHandle:
    data: Dict[str, object]
    version: int = 1


@dataclass(frozen=True)
class RunStatus:
    state: str
    exit_code: Optional[int] = None
    details: Dict[str, object] = field(default_factory=dict)
    outputs_finalized: Optional[bool] = None
    cleanup_pending: Optional[bool] = None

    def __post_init__(self):
        if self.state not in STATES:
            raise MonitoringError(f"Invalid backend state: {self.state!r}")


class Backend(Protocol):
    def submit(self, request: RunRequest, writer: "RecordWriter") -> RunHandle: ...
    def status(self, handle: RunHandle) -> RunStatus: ...
    def tail(self, handle: RunHandle) -> None: ...
    def pull(self, handle: RunHandle, destination: Path) -> None: ...
    def cancel(self, handle: RunHandle) -> None: ...


@dataclass(frozen=True)
class CLIOption:
    flags: Tuple[str, ...]
    key: str
    parse: Callable[[str], object] = str
    help: str = ""
    metavar: Optional[str] = None


@dataclass(frozen=True)
class Registration:
    name: str
    factory: Callable[[PluginConfig], Backend]
    selectors: Tuple[str, ...] = ()
    api_version: int = API_VERSION
    plugin_version: str = "unknown"
    cli_options: Tuple[CLIOption, ...] = ()
    supports_dependencies: bool = False


def discover() -> Dict[str, Registration]:
    registrations = {}
    flags = set()
    points = metadata.entry_points()
    entries = points.select(group="encap.backends") if hasattr(points, "select") else points.get("encap.backends", ())
    for entry in entries:
        try:
            registration = entry.load()
        except (ImportError, AttributeError) as error:
            raise ConfigurationError(f"Cannot load backend {entry.name}: {error}. Reinstall its plugin.") from error
        if not isinstance(registration, Registration):
            raise ConfigurationError(f"Backend {entry.name} must export a Registration")
        if type(registration.api_version) is not int or registration.api_version != API_VERSION:
            raise ConfigurationError(
                f"Backend {registration.name} uses API {registration.api_version}; Encap supports {API_VERSION}. "
                "Install compatible Encap and plugin versions."
            )
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", registration.name):
            raise ConfigurationError(f"Invalid backend name: {registration.name!r}")
        if registration.name in registrations:
            raise ConfigurationError(f"Duplicate backend name: {registration.name}")
        keys = set()
        for option in registration.cli_options:
            if (not isinstance(option, CLIOption) or not option.flags
                    or not re.fullmatch(r"[a-z][a-z0-9_]*", option.key)
                    or option.key in keys or not callable(option.parse)):
                raise ConfigurationError(f"Invalid or duplicate plugin option for {registration.name}")
            keys.add(option.key)
        option_flags = tuple(flag for option in registration.cli_options for flag in option.flags)
        for flag in registration.selectors + option_flags:
            if not re.fullmatch(r"--?[A-Za-z][A-Za-z0-9_-]*", flag) or flag in flags:
                raise ConfigurationError(f"Invalid or duplicate plugin selector: {flag}")
            flags.add(flag)
        registrations[registration.name] = registration
    return registrations


def register_selectors(parser, registrations):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--backend", help="Select an installed execution backend", default=None)
    for registration in registrations.values():
        for flag in registration.selectors:
            if flag in parser._option_string_actions:
                raise ConfigurationError(f"Plugin selector {flag} conflicts with an Encap option")
        if registration.selectors:
            group.add_argument(*registration.selectors, dest="backend", action="store_const",
                               const=registration.name, help=f"Use the {registration.name} backend")
        for option in registration.cli_options:
            for flag in option.flags:
                if flag in parser._option_string_actions:
                    raise ConfigurationError(f"Plugin selector {flag} conflicts with an Encap option")
            parser.add_argument(
                *option.flags, dest=f"plugin_{registration.name}_{option.key}",
                type=option.parse, default=None, metavar=option.metavar, help=option.help,
            )


def exit_code(status: RunStatus) -> int:
    if status.state == "succeeded":
        if status.exit_code is not None and (type(status.exit_code) is not int or status.exit_code != 0):
            raise MonitoringError("Backend reported success with a nonzero or invalid exit code")
        if status.outputs_finalized is False:
            raise MonitoringError("Computation succeeded but output finalization has not succeeded")
        return 0
    if status.state not in ("failed", "cancelled"):
        raise MonitoringError(f"Cannot determine completion: {status.state}; {status.details}")
    if type(status.exit_code) is int and 1 <= status.exit_code <= 255:
        return status.exit_code
    return 130 if status.state == "cancelled" else 1


def wait(backend: Backend, handle: RunHandle, interval: float = POLL_INTERVAL,
         on_status: Optional[Callable[[RunStatus], None]] = None) -> RunStatus:
    if not math.isfinite(interval) or interval <= 0:
        raise ConfigurationError("Polling interval must be positive and finite")
    while True:
        status = backend.status(handle)
        if not isinstance(status, RunStatus):
            raise MonitoringError("Backend returned an invalid status")
        if on_status is not None:
            on_status(status)
        if status.state == "unknown":
            raise MonitoringError(f"Remote state is unknown: {status.details}")
        if status.state in ("succeeded", "failed", "cancelled"):
            exit_code(status)
            return status
        time.sleep(interval)


class RecordWriter:
    """Atomic local records; plugins must save recovery handles before remote submission."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "run.json"
        self.lock = FileLock(str(directory / "record.lock"))

    @classmethod
    def reserve(cls, destination: Path, registration: Registration, run_id: str):
        try:
            destination.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise BackendError(f"Run already exists: {destination}. Use a new run name.") from error
        directory = destination / ".encap-run"
        directory.mkdir(mode=0o700)
        writer = cls(directory)
        writer._write({
            "record_version": RECORD_VERSION, "api_version": API_VERSION,
            "backend": registration.name, "plugin_version": registration.plugin_version,
            "run_id": run_id, "state": "submission-unconfirmed", "handle": None,
        })
        return writer

    def _write(self, data):
        try:
            encoded = json.dumps(data, indent=2, allow_nan=False) + "\n"
        except (TypeError, ValueError) as error:
            raise BackendError(f"Run record is not JSON serializable: {error}") from error
        fd, temporary = tempfile.mkstemp(prefix="record-", suffix=".tmp", dir=str(self.directory))
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(str(self.directory), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def read(self):
        try:
            with self.path.open() as stream:
                record = json.load(stream)
        except (OSError, ValueError) as error:
            raise BackendError(f"Cannot read run record {self.path}: {error}") from error
        if not isinstance(record, dict):
            raise BackendError(f"Invalid run record: {self.path}")
        if (type(record.get("record_version")) is not int or record["record_version"] != RECORD_VERSION
                or type(record.get("api_version")) is not int or record["api_version"] not in (1, 2, API_VERSION)):
            raise BackendError("Incompatible run record/API version; install the Encap version that wrote this record")
        if not all(isinstance(record.get(key), str) for key in ("backend", "plugin_version", "run_id", "state")):
            raise BackendError(f"Invalid run record fields: {self.path}")
        handle = record.get("handle")
        if handle is not None and (not isinstance(handle, dict) or type(handle.get("version")) is not int
                                   or not isinstance(handle.get("data"), dict)):
            raise BackendError(f"Invalid saved handle: {self.path}")
        return record

    def update(self, **changes):
        with self.lock:
            record = self.read()
            record.update(changes)
            self._write(record)

    def save_handle(self, handle: RunHandle, confirmed: bool = False):
        if not isinstance(handle, RunHandle):
            raise BackendError("Plugin must return a RunHandle")
        self.update(handle=asdict(handle), state="submitted" if confirmed else "submission-unconfirmed")

    def save_status(self, status: RunStatus):
        self.update(computation=asdict(status), monitoring_error=None)
