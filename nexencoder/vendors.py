"""Vendor sidecars. Docker runs beside the channel, never inside it.

A sidecar is one image bound to channel A or B. systemd unit
``nexencoder-vendor@<name>`` owns the container. The container uses the host
network so it can reach the loopback caption and splice ports and the program
multicast. Stopping the container does not stop the channel. Starting one
selects vendor captions for its channel; stopping the last one for that
channel returns captions to VANC.

``python3 -m nexencoder.vendors supervise <name>`` is what the unit starts.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
from pathlib import Path

from .caption_feed import read_caption_source, write_caption_source
from .pipeline import CAPTION_PORT, FEED_ADDR, FEED_PORT, SPLICE_PORT
from .services import ServiceUnavailable, default_run as systemctl_run

log = logging.getLogger("nexencoder.vendors")

SLUG = re.compile(r"[a-z][a-z0-9-]{0,30}\Z")
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,200}\Z")
MAX_VENDORS = 8
MAX_ARGS = 16
MAX_ARG = 200


class VendorError(ValueError):
    """The vendor record, image, or unit name is not acceptable."""


def vendors_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "vendors"


def unit_name(name: str) -> str:
    slug = _slug(name)
    return f"nexencoder-vendor@{slug}.service"


def container_name(name: str) -> str:
    return f"nexencoder-vendor-{_slug(name)}"


def _slug(name: str) -> str:
    slug = (name or "").strip()
    if not SLUG.fullmatch(slug):
        raise VendorError("vendor name must be a lowercase slug")
    return slug


def _image(image: str) -> str:
    ref = (image or "").strip()
    if not IMAGE.fullmatch(ref):
        raise VendorError("image name is not a docker reference")
    return ref


def _args(raw) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            raise VendorError("vendor arguments are not valid") from exc
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        parts = list(raw)
    else:
        raise VendorError("vendor arguments must be a string or a list of strings")
    if len(parts) > MAX_ARGS:
        raise VendorError("a vendor accepts at most 16 arguments")
    for item in parts:
        if not item or len(item) > MAX_ARG or "\n" in item or "\r" in item or "\x00" in item:
            raise VendorError("a vendor argument is empty or too long")
    return parts


def _privileged(argv: list[str]) -> list[str]:
    if os.name != "posix":
        return argv
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() != 0:
        return ["sudo", "-n", *argv]
    return argv


def docker_run(args: list[str], timeout: float = 30) -> subprocess.CompletedProcess[str]:
    if shutil.which("docker") is None:
        raise ServiceUnavailable("docker is not available on this host")
    try:
        return subprocess.run(
            _privileged(["docker", *args]),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ServiceUnavailable("docker timed out") from exc


def _record_path(state_dir: Path, name: str) -> Path:
    return vendors_dir(state_dir) / f"{_slug(name)}.json"


def load_record(state_dir: Path, name: str) -> dict:
    path = _record_path(state_dir, name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VendorError(f"vendor {name} is not installed") from exc
    if not isinstance(raw, dict):
        raise VendorError(f"vendor {name} record is not an object")
    return {
        "name": _slug(str(raw.get("name") or name)),
        "image": _image(str(raw.get("image") or "")),
        "channel": _channel(raw.get("channel")),
        "args": _args(raw.get("args")),
    }


def _channel(value) -> str:
    if value not in ("A", "B"):
        raise VendorError("channel must be A or B")
    return value


def list_records(state_dir: Path) -> list[dict]:
    root = vendors_dir(state_dir)
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.glob("*.json")):
        try:
            found.append(load_record(state_dir, path.stem))
        except VendorError:
            log.warning("skipping vendor record %s", path.name, exc_info=True)
    return found


def save_vendor(state_dir: Path, raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise VendorError("vendor must be an object")
    record = {
        "name": _slug(str(raw.get("name") or "")),
        "image": _image(str(raw.get("image") or "")),
        "channel": _channel(raw.get("channel")),
        "args": _args(raw.get("args")),
    }
    root = vendors_dir(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    existing = {item["name"] for item in list_records(state_dir)}
    if record["name"] not in existing and len(existing) >= MAX_VENDORS:
        raise VendorError("eight vendors is the limit")
    path = _record_path(state_dir, record["name"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return record


def remove_vendor(state_dir: Path, name: str, run=None) -> dict:
    record = load_record(state_dir, name)
    stop_vendor(state_dir, record["name"], run=run)
    _record_path(state_dir, record["name"]).unlink(missing_ok=True)
    return record


def _unit_state(name: str, run) -> str:
    runner = run or systemctl_run
    proc = runner(["is-active", unit_name(name)])
    return (proc.stdout or "").strip() or "unknown"


def _active_names(state_dir: Path, channel: str, run, skip: str = "") -> list[str]:
    names = []
    for record in list_records(state_dir):
        if record["channel"] != channel or record["name"] == skip:
            continue
        try:
            active = _unit_state(record["name"], run) == "active"
        except ServiceUnavailable:
            active = False
        if active:
            names.append(record["name"])
    return names


def _is_active(name: str, run) -> bool:
    return _unit_state(name, run) == "active"


def _release_channel(state_dir: Path, channel: str, run, skip: str) -> None:
    if _active_names(state_dir, channel, run, skip=skip):
        return
    write_caption_source(state_dir, channel, "vanc")


def start_vendor(state_dir: Path, name: str, run=None) -> dict:
    record = load_record(state_dir, name)
    write_caption_source(state_dir, record["channel"], "vendor")
    runner = run or systemctl_run
    try:
        proc = runner(["start", unit_name(record["name"])])
    except ServiceUnavailable:
        _release_channel(state_dir, record["channel"], runner, skip=record["name"])
        raise
    if proc.returncode != 0:
        _release_channel(state_dir, record["channel"], runner, skip=record["name"])
        detail = (proc.stderr or proc.stdout or "systemctl start failed").strip()
        raise VendorError(detail[:500])
    record["active"] = "active"
    record["unit"] = unit_name(record["name"])
    return record


def stop_vendor(state_dir: Path, name: str, run=None) -> dict:
    record = load_record(state_dir, name)
    runner = run or systemctl_run
    proc = runner(["stop", unit_name(record["name"])])
    if proc.returncode not in (0, 5, None):
        detail = (proc.stderr or proc.stdout or "systemctl stop failed").strip()
        if "not loaded" not in detail and "does not exist" not in detail:
            raise VendorError(detail[:500])
    _release_channel(state_dir, record["channel"], runner, skip=record["name"])
    record["active"] = "inactive"
    record["unit"] = unit_name(record["name"])
    return record


def caption_status(state_dir: Path) -> dict:
    status = {}
    for channel in ("A", "B"):
        status[channel] = {
            "source": read_caption_source(state_dir, channel),
            "caption_port": CAPTION_PORT[channel],
            "splice_port": SPLICE_PORT[channel],
            "feed": f"{FEED_ADDR}:{FEED_PORT[channel]}",
        }
    return status


def list_vendors(state_dir: Path, run=None) -> dict:
    docker = shutil.which("docker") is not None
    detail = "" if docker else "docker is not available on this host"
    vendors = []
    down = ""
    for record in list_records(state_dir):
        if down:
            active = "unavailable"
        else:
            try:
                active = "active" if _is_active(record["name"], run) else "inactive"
            except ServiceUnavailable as exc:
                docker = False
                down = str(exc)
                detail = down
                active = "unavailable"
        vendors.append({**record, "active": active, "unit": unit_name(record["name"])})
    return {
        "ok": True,
        "docker": docker,
        "detail": detail,
        "vendors": vendors,
        "captions": caption_status(state_dir),
    }


def pull_image(image: str, docker=None) -> dict:
    ref = _image(image)
    runner = docker or (lambda args: docker_run(args, timeout=180))
    proc = runner(["pull", ref])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "docker pull failed").strip()
        raise VendorError(detail[:500])
    return {"ok": True, "image": ref}


def load_image(state_dir: Path, filename: str, docker=None) -> dict:
    """Load a tar that an operator placed in the vendor incoming directory."""
    incoming = (vendors_dir(state_dir) / "incoming").resolve()
    name = Path(filename or "").name
    if not name.endswith(".tar") or name != (filename or "").strip():
        raise VendorError("load a .tar file from the vendor incoming directory")
    path = (incoming / name).resolve()
    if path.parent != incoming or not path.is_file():
        raise VendorError("that tar is not in the vendor incoming directory")
    runner = docker or docker_run
    proc = runner(["load", "-i", str(path)])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "docker load failed").strip()
        raise VendorError(detail[:500])
    return {"ok": True, "file": name}


def docker_argv(record: dict) -> list[str]:
    """``docker run`` for one sidecar. The image arguments follow the image name."""
    channel = record["channel"]
    return [
        "docker", "run",
        "--name", container_name(record["name"]),
        "--network", "host",
        "--rm",
        "--label", "nexencoder.vendor=1",
        "--label", f"nexencoder.channel={channel}",
        "-e", f"NEXENCODER_CHANNEL={channel}",
        "-e", f"NEXENCODER_CAPTION_PORT={CAPTION_PORT[channel]}",
        "-e", f"NEXENCODER_SPLICE_PORT={SPLICE_PORT[channel]}",
        "-e", f"NEXENCODER_FEED={FEED_ADDR}:{FEED_PORT[channel]}",
        record["image"],
        *record["args"],
    ]


def docker_rm_argv(name: str) -> list[str]:
    return ["docker", "rm", "-f", container_name(name)]


def docker_stop_argv(name: str) -> list[str]:
    return ["docker", "stop", "-t", "10", container_name(name)]


def supervise(name: str, state_dir: Path, spawn=None, run=None) -> int:
    """Replace a leftover container and run the sidecar until it exits."""
    record = load_record(state_dir, name)
    starter = spawn or subprocess.Popen
    waiter = run or subprocess.run
    container = container_name(record["name"])
    waiter(docker_rm_argv(record["name"]), check=False)
    proc = starter(docker_argv(record))
    stopping = False

    def _stop(*_args) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        log.info("stopping vendor container %s", container)
        try:
            waiter(docker_stop_argv(record["name"]), timeout=15, check=False)
        except Exception:
            log.warning("docker stop failed for %s", container, exc_info=True)

    if os.name == "posix":
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    code = proc.wait()
    return int(code or 0)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Run or stop one NexEncoder vendor sidecar")
    parser.add_argument("command", choices=("supervise", "stop"))
    parser.add_argument("name")
    parser.add_argument("--state", type=Path, default=Path(os.environ.get("NEXENCODER_STATE", "/var/lib/nexencoder/state")))
    args = parser.parse_args(argv)
    try:
        if args.command == "stop":
            subprocess.run(docker_stop_argv(args.name), check=False)
            return 0
        return supervise(args.name, args.state)
    except VendorError as exc:
        log.error("%s", exc)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
