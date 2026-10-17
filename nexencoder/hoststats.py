"""Host metrics for the NOC agent.

CPU percent and NIC throughput need two samples, which this collector keeps
in memory. GPU numbers come from nvidia-smi, then intel_gpu_top, then a DRM
sysfs busy counter. Missing tools leave the corresponding fields empty.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("nexencoder.host")

SKIP_FS = frozenset({
    "tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs", "cgroup",
    "cgroup2", "efivarfs", "autofs", "nsfs", "ramfs", "devpts", "securityfs",
    "pstore", "bpf", "tracefs", "debugfs", "configfs", "fusectl", "mqueue",
    "hugetlbfs", "rpc_pipefs", "binfmt_misc", "fuse.portal",
})
_NVIDIA_QUERY = [
    "nvidia-smi",
    "--query-gpu=name,utilization.gpu,utilization.encoder,utilization.decoder,memory.used,memory.total,temperature.gpu",
    "--format=csv,noheader,nounits",
]
_MIN_INTERVAL = 0.2
_CARD_DIR = re.compile(r"card\d+$")


def parse_proc_stat(text: str) -> tuple[int, int] | None:
    """Return (idle, total) jiffies from the aggregate ``cpu`` line."""
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        numbers: list[int] = []
        for part in line.split()[1:]:
            try:
                numbers.append(int(part))
            except ValueError:
                return None
        if len(numbers) < 4:
            return None
        idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
        total = sum(numbers[:8] if len(numbers) >= 8 else numbers)
        return idle, total
    return None


def cpu_percent(previous: tuple[int, int] | None, sample: tuple[int, int] | None) -> float | None:
    if previous is None or sample is None:
        return None
    idle_delta = sample[0] - previous[0]
    total_delta = sample[1] - previous[1]
    if total_delta <= 0:
        return None
    value = 100.0 * (1.0 - (idle_delta / total_delta))
    if value < 0:
        value = 0.0
    if value > 100:
        value = 100.0
    return round(value, 2)


def parse_meminfo(text: str) -> dict | None:
    values: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        bits = rest.split()
        if not bits:
            continue
        try:
            values[key] = int(bits[0]) * 1024
        except ValueError:
            continue
    total = values.get("MemTotal")
    if not total:
        return None
    available = values.get("MemAvailable")
    used = None
    if available is not None and total > 0:
        used = round(100.0 * (1.0 - (available / total)), 2)
        if used < 0:
            used = 0.0
        if used > 100:
            used = 100.0
    return {"total_bytes": total, "available_bytes": available, "used_percent": used}


def parse_loadavg(text: str) -> dict | None:
    parts = text.split()
    if len(parts) < 3:
        return None
    try:
        return {"1m": float(parts[0]), "5m": float(parts[1]), "15m": float(parts[2])}
    except ValueError:
        return None


def parse_uptime(text: str) -> float | None:
    parts = text.split()
    if not parts:
        return None
    try:
        value = float(parts[0])
    except ValueError:
        return None
    if value < 0:
        return None
    return value


def parse_netdev(text: str) -> dict[str, tuple[int, int]]:
    rows: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if not name or name == "lo":
            continue
        cols = rest.split()
        if len(cols) < 9:
            continue
        try:
            rows[name] = (int(cols[0]), int(cols[8]))
        except ValueError:
            continue
    return rows


def nic_rates(
    previous: dict[str, tuple[int, int]] | None,
    sample: dict[str, tuple[int, int]],
    elapsed: float,
) -> list[dict]:
    rows = []
    for name in sorted(sample):
        rx, tx = sample[name]
        rx_bps = None
        tx_bps = None
        if previous and name in previous and elapsed >= _MIN_INTERVAL:
            rx_delta = rx - previous[name][0]
            tx_delta = tx - previous[name][1]
            if rx_delta >= 0 and tx_delta >= 0:
                rx_bps = int(round((rx_delta * 8) / elapsed))
                tx_bps = int(round((tx_delta * 8) / elapsed))
        rows.append({"name": name, "rx_bps": rx_bps, "tx_bps": tx_bps})
    return rows


def parse_mounts(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = parts[1].replace("\\040", " ")
        fstype = parts[2]
        if fstype in SKIP_FS or mount in seen:
            continue
        if mount.startswith(("/proc", "/sys", "/run", "/snap", "/dev")):
            continue
        seen.add(mount)
        rows.append((mount, fstype))
    return rows


def disk_row(mount: str, fstype: str, stat) -> dict | None:
    try:
        total = int(stat.f_frsize) * int(stat.f_blocks)
        avail = int(stat.f_frsize) * int(stat.f_bavail)
    except (AttributeError, TypeError, ValueError):
        return None
    if total <= 0:
        return None
    used = total - avail
    if used < 0:
        used = 0
    return {
        "mount": mount,
        "fstype": fstype,
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": avail,
        "used_percent": round(100.0 * used / total, 2),
    }


def _celsius(milli: str) -> float | None:
    try:
        value = int(milli.strip()) / 1000.0
    except (TypeError, ValueError):
        return None
    if value <= 0 or value >= 150:
        return None
    return round(value, 1)


def read_temperatures(sys_root: Path) -> list[dict]:
    found: list[dict] = []
    thermal = sys_root / "class" / "thermal"
    if thermal.is_dir():
        for zone in sorted(path for path in thermal.glob("thermal_zone*") if path.is_dir()):
            try:
                name = (zone / "type").read_text(encoding="utf-8", errors="replace").strip()
                temp = _celsius((zone / "temp").read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if temp is None:
                continue
            found.append({"name": name or zone.name, "celsius": temp, "source": "thermal"})
    hwmon = sys_root / "class" / "hwmon"
    if hwmon.is_dir():
        for chip in sorted(path for path in hwmon.glob("hwmon*") if path.is_dir()):
            try:
                chip_name = (chip / "name").read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                chip_name = chip.name
            for sensor in sorted(chip.glob("temp*_input")):
                label_path = chip / (sensor.name.replace("_input", "_label"))
                try:
                    label = label_path.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    label = sensor.name.replace("_input", "")
                try:
                    temp = _celsius(sensor.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                if temp is None:
                    continue
                name = f"{chip_name} {label}".strip()
                found.append({"name": name, "celsius": temp, "source": "hwmon"})
    return found


def _metric(text: str) -> float | None:
    cleaned = text.strip()
    if cleaned.upper() in {"", "N/A", "[N/A]", "[NOT SUPPORTED]"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_nvidia_smi(text: str) -> list[dict]:
    devices = []
    for line in text.splitlines():
        if not line.strip() or line.lower().startswith("no devices"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 7:
            continue
        used = _metric(parts[4])
        total = _metric(parts[5])
        devices.append({
            "index": len(devices),
            "name": parts[0],
            "vendor": "nvidia",
            "utilization_percent": _metric(parts[1]),
            "encoder_percent": _metric(parts[2]),
            "decoder_percent": _metric(parts[3]),
            "memory_used_bytes": None if used is None else int(used * 1024 * 1024),
            "memory_total_bytes": None if total is None else int(total * 1024 * 1024),
            "temperature_c": _metric(parts[6]),
        })
    return devices


def _json_objects(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    found: list[dict] = []
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        index = end if end > start else start + 1
    return found


def _engine_busy(engines: dict) -> tuple[float | None, float | None, float | None]:
    render = None
    encode = None
    decode = None
    for name, body in engines.items():
        if not isinstance(body, dict):
            continue
        busy = _metric(str(body.get("busy", "")))
        if busy is None:
            continue
        lowered = name.lower()
        if "render" in lowered or lowered.startswith("rcs"):
            render = busy if render is None else max(render, busy)
        elif "enhance" in lowered:
            encode = busy if encode is None else max(encode, busy)
        elif "video" in lowered or lowered.startswith("vcs"):
            decode = busy if decode is None else max(decode, busy)
    return render, encode, decode


def parse_intel_gpu_top(text: str) -> list[dict]:
    devices = []
    for obj in _json_objects(text):
        engines = obj.get("engines")
        if not isinstance(engines, dict):
            continue
        render, encode, decode = _engine_busy(engines)
        if render is None and encode is None and decode is None:
            continue
        devices = [{
            "index": 0,
            "name": "Intel GPU",
            "vendor": "intel",
            "utilization_percent": render,
            "encoder_percent": encode,
            "decoder_percent": decode,
            "memory_used_bytes": None,
            "memory_total_bytes": None,
            "temperature_c": None,
        }]
    return devices


def _vendor_name(card: Path) -> str:
    vendor_path = card / "device" / "vendor"
    try:
        code = vendor_path.read_text(encoding="utf-8", errors="replace").strip().lower()
    except OSError:
        return "drm"
    return {"0x10de": "nvidia", "0x8086": "intel", "0x1002": "amd"}.get(code, "drm")


def read_drm_gpus(sys_root: Path) -> list[dict]:
    drm = sys_root / "class" / "drm"
    if not drm.is_dir():
        return []
    devices = []
    cards = sorted(path for path in drm.iterdir() if path.is_dir() and _CARD_DIR.fullmatch(path.name))
    for card in cards:
        busy_path = card / "device" / "gpu_busy_percent"
        if not busy_path.is_file():
            busy_path = card / "gpu_busy_percent"
        if not busy_path.is_file():
            continue
        try:
            busy = float(busy_path.read_text(encoding="utf-8", errors="replace").strip())
        except (OSError, ValueError):
            continue
        devices.append({
            "index": len(devices),
            "name": card.name,
            "vendor": _vendor_name(card),
            "utilization_percent": busy,
            "encoder_percent": None,
            "decoder_percent": None,
            "memory_used_bytes": None,
            "memory_total_bytes": None,
            "temperature_c": None,
        })
    return devices


def _gpu_shell(source: str, devices: list[dict], detail: str = "") -> dict:
    return {"available": bool(devices), "source": source, "devices": devices, "detail": detail}


class HostCollector:
    """Process-local sampler. The first status poll has no CPU or NIC rate."""

    def __init__(
        self,
        proc_root: Path | None = None,
        sys_root: Path | None = None,
        run=None,
        which=None,
        clock=None,
    ) -> None:
        self.proc_root = proc_root or Path("/proc")
        self.sys_root = sys_root or Path("/sys")
        self.run = run or subprocess.run
        self.which = which or shutil.which
        self.clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._cpu: tuple[int, int] | None = None
        self._cpu_at: float | None = None
        self._nics: dict[str, tuple[int, int]] | None = None
        self._nic_at: float | None = None
        self._gpu: dict | None = None
        self._gpu_at = 0.0

    def _text(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _nvidia(self) -> list[dict]:
        if self.which("nvidia-smi") is None:
            return []
        try:
            proc = self.run(_NVIDIA_QUERY, capture_output=True, text=True, timeout=2.0, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if getattr(proc, "returncode", 1) != 0:
            return []
        return parse_nvidia_smi(getattr(proc, "stdout", "") or "")

    def _intel(self) -> list[dict]:
        if self.which("intel_gpu_top") is None:
            return []
        proc = None
        try:
            proc = subprocess.Popen(
                ["intel_gpu_top", "-J", "-s", "200"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            out, _err = proc.communicate(timeout=1.5)
        except (OSError, subprocess.TimeoutExpired):
            out = b""
            if proc is not None and proc.poll() is None:
                proc.kill()
                try:
                    out, _err = proc.communicate(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    out = b""
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else (out or "")
        return parse_intel_gpu_top(text)

    def _gpu_now(self, now: float) -> dict:
        if self._gpu is not None and (now - self._gpu_at) < 5:
            return self._gpu
        nvidia = self._nvidia()
        if nvidia:
            snapshot = _gpu_shell("nvidia-smi", nvidia)
        else:
            intel = self._intel()
            if intel:
                detail = ""
                if all(item["encoder_percent"] is None and item["decoder_percent"] is None for item in intel):
                    detail = "intel_gpu_top did not report video engines"
                snapshot = _gpu_shell("intel_gpu_top", intel, detail)
            else:
                drm = read_drm_gpus(self.sys_root)
                if drm:
                    snapshot = _gpu_shell(
                        "drm-sysfs",
                        drm,
                        "DRM busy percent has no encoder or decoder split",
                    )
                else:
                    snapshot = _gpu_shell("none", [], "no nvidia-smi, intel_gpu_top, or DRM busy counter")
        self._gpu = snapshot
        self._gpu_at = now
        return snapshot

    def snapshot(self, now: float | None = None) -> dict:
        with self._lock:
            return self._snapshot(self.clock() if now is None else now)

    def _snapshot(self, now: float) -> dict:
        limitations: list[str] = []
        stat = parse_proc_stat(self._text(self.proc_root / "stat") or "")
        if stat is None:
            limitations.append("cpu_unavailable")
            cpu = None
        else:
            elapsed = None if self._cpu_at is None else now - self._cpu_at
            if self._cpu is None or elapsed is None or elapsed < _MIN_INTERVAL:
                cpu = None
                limitations.append("cpu_percent_needs_two_samples")
            else:
                cpu = cpu_percent(self._cpu, stat)
                if cpu is None:
                    limitations.append("cpu_unavailable")
            self._cpu = stat
            self._cpu_at = now

        load = parse_loadavg(self._text(self.proc_root / "loadavg") or "")
        if load is None:
            limitations.append("load_unavailable")
            load = {"1m": None, "5m": None, "15m": None}

        memory = parse_meminfo(self._text(self.proc_root / "meminfo") or "")
        if memory is None:
            limitations.append("memory_unavailable")
            memory = {"total_bytes": None, "available_bytes": None, "used_percent": None}

        uptime = parse_uptime(self._text(self.proc_root / "uptime") or "")
        if uptime is None:
            limitations.append("uptime_unavailable")

        mounts = parse_mounts(self._text(self.proc_root / "mounts") or "")
        disks = []
        for mount, fstype in mounts:
            try:
                row = disk_row(mount, fstype, os.statvfs(mount))
            except OSError:
                continue
            if row is not None:
                disks.append(row)
        if not disks:
            limitations.append("disk_unavailable")

        net = self._text(self.proc_root / "net" / "dev")
        if net is None:
            limitations.append("nic_unavailable")
            nics: list[dict] = []
        else:
            sample = parse_netdev(net)
            elapsed = None if self._nic_at is None else now - self._nic_at
            if self._nics is None or elapsed is None or elapsed < _MIN_INTERVAL:
                nics = nic_rates(None, sample, 0)
                limitations.append("nic_throughput_needs_two_samples")
            else:
                nics = nic_rates(self._nics, sample, elapsed)
            self._nics = sample
            self._nic_at = now

        temperatures = read_temperatures(self.sys_root)
        gpu = self._gpu_now(now)
        for device in gpu["devices"]:
            if device.get("temperature_c") is None:
                continue
            temperatures.append({
                "name": f"{device['vendor']}-{device['index']}",
                "celsius": device["temperature_c"],
                "source": gpu["source"],
            })
        if not gpu["available"]:
            limitations.append("gpu_unavailable")
        elif gpu["devices"] and all(
            item.get("encoder_percent") is None and item.get("decoder_percent") is None
            for item in gpu["devices"]
        ):
            limitations.append("gpu_encoder_decoder_unavailable")
        if not temperatures:
            limitations.append("temperatures_unavailable")

        return {
            "cpu_percent": cpu,
            "load": load,
            "memory": memory,
            "temperatures_c": temperatures,
            "disks": disks,
            "nics": nics,
            "gpu": gpu,
            "uptime_seconds": None if uptime is None else int(uptime),
            "limitations": limitations,
        }
