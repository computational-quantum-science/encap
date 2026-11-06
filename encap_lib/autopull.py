"""Persistent, per-user synchronization of saved cloud runs."""

import argparse
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import psutil
from filelock import FileLock, Timeout

from encap_lib import backends, encap_settings as settings
from encap_lib.backends import (
    BackendError, ConfigurationError, PluginConfig, RecordWriter, RunHandle, RunStatus,
)
from encap_lib.run_preparation import saved_source_files

INTERVAL = 60


def directory():
    return Path(settings.config_folder) / "daemon"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read(path, default):
    if path.is_symlink():
        raise BackendError(f"Daemon metadata must not be a symlink: {path}")
    try:
        with path.open() as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as error:
        raise BackendError(f"Cannot read daemon metadata {path}: {error}") from error
    if not isinstance(data, dict):
        raise BackendError(f"Invalid daemon metadata: {path}")
    return data


def _write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _state(root):
    state = _read(root / "runs.json", {"version": 1, "enabled": True, "runs": {}})
    if (state.get("version") != 1 or type(state.get("enabled")) is not bool
            or not isinstance(state.get("runs"), dict)
            or any(not isinstance(path, str) or not Path(path).is_absolute()
                   or not isinstance(entry, dict) or type(entry.get("revision")) is not int
                   for path, entry in state["runs"].items())):
        raise BackendError(f"Invalid daemon registry: {root / 'runs.json'}")
    return state


def _process(root):
    info = _read(root / "pid.json", {})
    pid = info.get("pid")
    if pid is None:
        return None
    if type(pid) is not int or pid <= 0 or not isinstance(info.get("created"), (float, int)):
        raise BackendError("Invalid daemon process identity")
    try:
        process = psutil.Process(pid)
        if process.create_time() != info["created"] or process.status() == psutil.STATUS_ZOMBIE:
            return None
        command = process.cmdline()
        if "encap_lib.autopull" not in command or str(root) not in command:
            return None
        return process
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied as error:
        raise BackendError(f"Cannot verify daemon process {pid}: {error}") from error


def status(root=None):
    root = Path(root) if root is not None else directory()
    state = _state(root)
    process = _process(root)
    return {
        "running": process is not None, "pid": process.pid if process else None,
        "enabled": state["enabled"], "interval_seconds": INTERVAL,
        "last_cycle_at": state.get("last_cycle_at"), "runs": state["runs"],
        "log": str(root / "daemon.log"),
    }


def start(root=None, automatic=False):
    root = Path(root) if root is not None else directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with FileLock(str(root / "control.lock")):
        with FileLock(str(root / "registry.lock")):
            state = _state(root)
            if not automatic:
                state["enabled"] = True
            _write(root / "runs.json", state)
        if not state["enabled"] or _process(root) is not None:
            return status(root)
        log_path = root / "daemon.log"
        fd = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a") as log:
            try:
                child = subprocess.Popen(
                    [sys.executable, "-u", "-m", "encap_lib.autopull", "--state-dir", str(root)],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                    env=dict(os.environ, CLOUDSDK_CORE_DISABLE_PROMPTS="1"),
                )
            except OSError as error:
                raise BackendError(f"Cannot start automatic pulls: {error}") from error
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise BackendError(f"Automatic pull daemon exited with {child.returncode}; see {log_path}")
            process = _process(root)
            if process is not None and process.pid == child.pid:
                return status(root)
            time.sleep(0.05)
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        raise BackendError(f"Automatic pull daemon did not become ready; see {log_path}")


def stop(root=None):
    root = Path(root) if root is not None else directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with FileLock(str(root / "control.lock")):
        with FileLock(str(root / "registry.lock")):
            state = _state(root)
            state["enabled"] = False
            _write(root / "runs.json", state)
        process = _process(root)
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except psutil.TimeoutExpired:
                try:
                    for child in process.children(recursive=True):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    process.kill()
                    process.wait(timeout=5)
                except psutil.NoSuchProcess:
                    pass
            except psutil.NoSuchProcess:
                pass
        return status(root)


def register(destination, registration, source_dir=None):
    destination = Path(destination).absolute()
    record = RecordWriter(destination / ".encap-run").read()
    if record["backend"] != registration.name or not registration.supports_autopull:
        raise ConfigurationError(f"Backend {registration.name} does not support automatic pulls")
    if record["handle"] is None:
        raise BackendError("Cannot watch a run without a saved submission handle")
    preparation = record.get("preparation", {})
    if "source_files" not in preparation:
        original = source_dir or preparation.get("source_dir")
        if original is None:
            raise BackendError("Use encap watch with the original target to recover this run's source manifest")
        preparation["source_files"] = saved_source_files(original, destination)
        RecordWriter(destination / ".encap-run").update(preparation=preparation)
    root = directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with FileLock(str(root / "registry.lock")):
        state = _state(root)
        previous = state["runs"].get(str(destination), {})
        state["runs"][str(destination)] = {
            "revision": previous.get("revision", 0) + 1,
            "backend": registration.name, "state": "watching", "registered_at": _now(),
        }
        _write(root / "runs.json", state)


def watch(destination, registration, automatic=False, source_dir=None):
    register(destination, registration, source_dir=source_dir)
    return start(automatic=automatic)


def _sync(destination, entry, registrations):
    writer = RecordWriter(destination / ".encap-run")
    record = writer.read()
    registration = registrations.get(record["backend"])
    if registration is None or not registration.supports_autopull:
        raise ConfigurationError(f"Automatic-pull backend {record['backend']} is unavailable")
    if record["handle"] is None:
        raise BackendError("Saved run has no submission handle")
    fingerprint = hashlib.sha256(json.dumps(record["handle"], sort_keys=True).encode()).hexdigest()
    if entry.get("state") == "complete" and entry.get("handle_fingerprint") == fingerprint:
        return entry
    backend = registration.factory(PluginConfig())
    handle = RunHandle(**record["handle"])
    backend.sync(handle, destination)
    result = dict(entry, last_synced_at=_now(), handle_fingerprint=fingerprint, error=None)
    try:
        computation = backend.status(handle)
        if not isinstance(computation, RunStatus):
            raise BackendError("Backend returned an invalid computation status")
        result["run_state"] = computation.state
        if (computation.state in ("succeeded", "failed", "cancelled")
                and computation.cleanup_pending is False):
            backend.sync(handle, destination, final=True)
            result.update(state="complete", last_synced_at=_now())
        else:
            result["state"] = "watching"
    except BackendError as error:
        result.update(state="retrying", error=str(error))
        print(f"{_now()} {destination}: {error}", flush=True)
    return result


def cycle(root, registrations):
    with FileLock(str(root / "registry.lock")):
        snapshot = _state(root)
    for path, entry in snapshot["runs"].items():
        if not _state(root)["enabled"]:
            break
        destination = Path(path)
        try:
            with FileLock(str(destination.parent / f".{destination.name}.submission.lock"), timeout=0):
                result = _sync(destination, entry, registrations)
        except Timeout:
            result = dict(entry, state="pending", error="Run is being submitted or rerun; retrying next cycle")
        except (BackendError, OSError) as error:
            result = dict(entry, state="retrying", error=f"{type(error).__name__}: {error}")
            print(f"{_now()} {destination}: {result['error']}", flush=True)
        if result is entry:
            continue
        result["last_attempt_at"] = _now()
        with FileLock(str(root / "registry.lock")):
            latest = _state(root)
            current = latest["runs"].get(path)
            if current is not None and current["revision"] == entry["revision"]:
                latest["runs"][path] = result
                _write(root / "runs.json", latest)
    with FileLock(str(root / "registry.lock")):
        state = _state(root)
        state["last_cycle_at"] = _now()
        _write(root / "runs.json", state)


def serve(root):
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    with FileLock(str(root / "daemon.lock"), timeout=0):
        registrations = backends.discover()
        _write(root / "pid.json", {
            "pid": os.getpid(), "created": psutil.Process().create_time(), "started_at": _now(),
        })
        print(f"{_now()} Automatic pulls started; interval={INTERVAL}s", flush=True)
        try:
            while not stopped.is_set() and _state(root)["enabled"]:
                began = time.monotonic()
                cycle(root, registrations)
                stopped.wait(max(0, INTERVAL - (time.monotonic() - began)))
        finally:
            _write(root / "pid.json", {"pid": None, "stopped_at": _now()})
            print(f"{_now()} Automatic pulls stopped", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(arguments.state_dir / "daemon.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a", buffering=1) as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        try:
            serve(arguments.state_dir)
        except (BackendError, OSError, Timeout):
            traceback.print_exc()
            raise SystemExit(2)


if __name__ == "__main__":
    main()
