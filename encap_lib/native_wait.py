"""Completion tracking for existing local/SSH, Slurm and Pueue runs."""

import json
import re
import shlex
import time

from encap_lib.backends import MonitoringError, POLL_INTERVAL

DIRECTORY = ".encap-native"


def exit_filename(index, legacy=False):
    filename = "exit" if index == 0 else f"exit_{index}"
    return ".encap-" + filename if legacy else DIRECTORY + "/" + filename


def completion_code(index):
    filename = exit_filename(index)
    return (
        "encap_result=$?; "
        f"printf '%s\\n' \"$encap_result\" > {shlex.quote(filename + '.tmp')}; "
        f"mv {shlex.quote(filename + '.tmp')} {shlex.quote(filename)}; "
        "printf '\\004\\n'; exit \"$encap_result\""
    )


def save_tracking(machine, folder, kind, identifiers):
    data = json.dumps({"version": 1, "kind": kind, "identifiers": identifiers})
    directory = shlex.quote(folder + "/" + DIRECTORY)
    filename = shlex.quote(folder + "/" + DIRECTORY + "/tracking.json")
    _command(machine, f"mkdir -p {directory} && "
             f"printf '%s\\n' {shlex.quote(data)} > {filename}.tmp && mv {filename}.tmp {filename}")


def _command(machine, command):
    lines, code = machine.run_code(command, get_returncode=True, ignore_errors=True, propagate_interrupt=True)
    if code != 0:
        raise MonitoringError(f"Cannot monitor computation: {command}: {' '.join(lines)}")
    return lines


def read_tracking(machine, folder):
    filename = shlex.quote(folder + "/" + DIRECTORY + "/tracking.json")
    old_filename = shlex.quote(folder + "/.encap-native.json")
    # Command output strips the last character of unterminated lines.
    lines = _command(machine, f"if [ -e {filename} ] || [ -L {filename} ]; then "
                     f"echo native; cat {filename} && printf '\\n'; "
                     f"elif [ -e {old_filename} ] || [ -L {old_filename} ]; then "
                     f"echo legacy; cat {old_filename} && printf '\\n'; else echo missing; fi")
    if lines == ["missing"]:
        return None, False
    if not lines or lines[0] not in ("native", "legacy"):
        raise MonitoringError("Invalid native run tracking response")
    try:
        tracking = json.loads("\n".join(lines[1:]))
    except ValueError as error:
        raise MonitoringError(f"Invalid native run tracking: {error}") from error
    if (not isinstance(tracking, dict) or tracking.get("version") != 1
            or tracking.get("kind") not in ("process", "slurm", "pueue")
            or not isinstance(tracking.get("identifiers"), list) or not tracking["identifiers"]):
        raise MonitoringError("No supported completion tracking. wait requires a run submitted by Encap 0.6 or later.")
    if any(not isinstance(item, dict) or not re.fullmatch(r"[0-9]+", str(item.get("id", "")))
           or not re.fullmatch(r"[0-9]+", str(item.get("instance", ""))) for item in tracking["identifiers"]):
        raise MonitoringError("Invalid native computation identifiers")
    return tracking, lines[0] == "legacy"


def tail_target(machine, folder):
    tracking, _ = read_tracking(machine, folder)
    if tracking is not None:
        first = tracking["identifiers"][0]
        index = int(first["instance"])
        log = "log" if index == 0 else f"log_{index}"
        if tracking["kind"] == "process":
            return str(first["id"]), log
        if tracking["kind"] == "slurm":
            return "slurm:" + str(first["id"]), log
        return None, log
    filename = shlex.quote(folder + "/pid")
    lines = _command(machine, f"if [ -e {filename} ] || [ -L {filename} ]; then cat {filename} && printf '\\n'; fi")
    if not lines:
        return None, "log"
    value = "\n".join(lines).strip()
    if not re.fullmatch(r"(slurm:)?[0-9]+", value):
        raise MonitoringError(f"Invalid legacy run PID: {value}")
    return value, "log"


def _pueue_result(task):
    status = task.get("status")
    if status in ("Queued", "Stashed", "Running", "Paused", "Locked"):
        return None
    if isinstance(status, dict) and any(key in status for key in ("Queued", "Stashed", "Running", "Paused", "Locked")):
        return None
    if not isinstance(status, dict) or "Done" not in status:
        raise MonitoringError(f"Unknown Pueue task state: {status}")
    result = status["Done"]
    if isinstance(result, dict) and "result" in result:
        result = result["result"]
    if result == "Success":
        return 0
    if result == "Killed":
        return 130
    if isinstance(result, dict) and type(result.get("Failed")) is int:
        code = result["Failed"]
        return code if 1 <= code <= 255 else 1
    if result in ("DependencyFailed", "Errored") or isinstance(result, dict) and any(key in result for key in ("Errored", "FailedToSpawn")):
        return 1
    raise MonitoringError(f"Unknown Pueue result: {result}")


def wait(machine, folder, interval=POLL_INTERVAL):
    tracking, legacy = read_tracking(machine, folder)
    if tracking is None:
        raise MonitoringError("No supported completion tracking. wait requires a run submitted by Encap 0.6 or later.")
    kind = tracking["kind"]
    identifiers = tracking["identifiers"]
    while True:
        results = []
        if kind == "slurm":
            ids = ",".join(str(item["id"]) for item in identifiers)
            rows = _command(machine, f"sacct -j {ids} --noheader --parsable2 --format=JobIDRaw,State,ExitCode")
            states = {}
            for row in rows:
                fields = row.strip().split("|")
                if len(fields) >= 3 and fields[0] in ids.split(","):
                    states[fields[0]] = fields[1:3]
            for item in identifiers:
                jobid = str(item["id"])
                if jobid not in states:
                    # Accounting can lag submission; a live queue entry proves pending work.
                    queued = _command(machine, f"squeue -h -j {jobid} -o %T")
                    if not any(line.strip() for line in queued):
                        raise MonitoringError(f"Slurm job {jobid} is absent from both accounting and queue")
                    results.append(None)
                    continue
                state, code = states[jobid]
                if state in ("PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED", "REQUEUED", "RESIZING"):
                    results.append(None)
                elif state.startswith("CANCELLED"):
                    results.append(130)
                elif state in ("COMPLETED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED"):
                    if not re.fullmatch(r"\d+:\d+", code):
                        raise MonitoringError(f"Invalid Slurm exit code: {code}")
                    result, signal = map(int, code.split(":"))
                    result = result or (128 + signal if signal else 0)
                    results.append((result if 1 <= result <= 255 else 1) if state != "COMPLETED" or result else 0)
                else:
                    raise MonitoringError(f"Unknown Slurm state: {state}")
        elif kind == "pueue":
            try:
                data = json.loads("\n".join(_command(machine, "pueue status --json")))
            except ValueError as error:
                raise MonitoringError(f"Invalid Pueue status: {error}") from error
            if not isinstance(data, dict) or not isinstance(data.get("tasks"), dict):
                raise MonitoringError("Pueue status contains no task mapping")
            tasks = data["tasks"]
            for item in identifiers:
                task = tasks.get(str(item["id"]))
                if not isinstance(task, dict):
                    raise MonitoringError(f"Pueue task {item['id']} is missing")
                results.append(_pueue_result(task))
        else:
            for item in identifiers:
                index = int(item["instance"])
                filename = exit_filename(index, legacy)
                path = shlex.quote(folder + "/" + filename)
                # This command runs on the actual machine, not the client laptop.
                lines = _command(machine, f"if [ -f {path} ]; then cat {path}; "
                                 f"elif kill -0 {item['id']} 2>/dev/null; then echo running; else echo unknown; fi")
                value = "".join(lines).strip()
                if value == "running":
                    results.append(None)
                elif value.isdigit() and 0 <= int(value) <= 255:
                    results.append(int(value))
                else:
                    raise MonitoringError("Process disappeared without a saved exit result; completion is unknown")
        if all(result is not None for result in results):
            return next((result for result in results if result), 0)
        time.sleep(interval)
