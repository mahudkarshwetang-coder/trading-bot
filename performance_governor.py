import ctypes
import os
import subprocess
import time
from dataclasses import dataclass

from config import (
    PERFORMANCE_BUSY_CPU_PCT,
    PERFORMANCE_DEFER_TASKS,
    PERFORMANCE_GAME_PROCESS_NAMES,
    PERFORMANCE_GAMING_CPU_BUDGET_PCT,
    PERFORMANCE_GAMING_EXTERNAL_PRIORITY,
    PERFORMANCE_GAMING_EXTERNAL_PROCESSES,
    PERFORMANCE_GAMING_INTERVAL_MULTIPLIER,
    PERFORMANCE_GAMING_KEEP_ALIVE,
    PERFORMANCE_GAMING_MAX_BATCH_SIZE,
    PERFORMANCE_GAMING_MAX_LLM_ITEMS,
    PERFORMANCE_GAMING_MAX_LLM_TICKERS,
    PERFORMANCE_GAMING_MAX_NUM_PREDICT,
    PERFORMANCE_GAMING_MAX_WORKERS,
    PERFORMANCE_GAMING_PROCESS_PRIORITY,
    PERFORMANCE_GAMING_THROTTLE_ENABLED,
    PERFORMANCE_GAMING_THROTTLE_MAX_SLEEP_SECONDS,
    PERFORMANCE_GAMING_THROTTLE_MIN_SLEEP_SECONDS,
    PERFORMANCE_GAMING_TIMEOUT_SECONDS,
    PERFORMANCE_GOVERNOR_ENABLED,
    PERFORMANCE_LOW_MEMORY_GB,
    PERFORMANCE_MODE,
    PERFORMANCE_PROFILE_TTL_SECONDS,
    PERFORMANCE_RESTORE_PRIORITY_ON_NORMAL,
)


@dataclass(frozen=True)
class PerformanceProfile:
    active: bool
    mode: str
    reasons: tuple
    cpu_pct: float | None = None
    free_memory_gb: float | None = None
    matched_processes: tuple = ()


_PROFILE_CACHE = {"expires_at": 0.0, "profile": None}
_NOTICE_CACHE = {}
_PRIORITY_STATE = {"current": None, "external": None, "external_processes": ()}

WINDOWS_PRIORITY_CLASSES = {
    "idle": 0x00000040,
    "belownormal": 0x00004000,
    "below_normal": 0x00004000,
    "normal": 0x00000020,
}

WINDOWS_PRIORITY_NAMES = {
    "idle": "Idle",
    "belownormal": "BelowNormal",
    "below_normal": "BelowNormal",
    "normal": "Normal",
}


def parse_csv(value):
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [
        item.strip()
        for item in str(value or "").replace("\n", ",").split(",")
        if item.strip()
    ]


def normalize_process_name(name):
    normalized = str(name or "").strip().lower()
    if normalized.endswith(".exe"):
        normalized = normalized[:-4]
    return normalized


def hidden_subprocess_kwargs():
    flags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags = subprocess.CREATE_NO_WINDOW
    return {"creationflags": flags} if flags else {}


def priority_key(value, default="belownormal"):
    normalized = str(value or default).strip().replace(" ", "").replace("-", "").lower()
    if normalized in {"below_normal", "belownormal"}:
        return "belownormal"
    if normalized in {"idle", "normal"}:
        return normalized
    return default


def set_current_process_priority(priority):
    if os.name != "nt":
        return False

    key = priority_key(priority)
    priority_class = WINDOWS_PRIORITY_CLASSES.get(key)
    if not priority_class:
        return False

    try:
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        return bool(ctypes.windll.kernel32.SetPriorityClass(handle, priority_class))
    except Exception:
        return False


def set_external_process_priority(process_names, priority):
    if os.name != "nt":
        return False

    names = [normalize_process_name(name) for name in parse_csv(process_names)]
    names = [name for name in names if name]
    if not names:
        return False

    key = priority_key(priority)
    priority_name = WINDOWS_PRIORITY_NAMES.get(key)
    if not priority_name:
        return False

    quoted_names = ",".join("'" + name.replace("'", "''") + "'" for name in names)
    command = (
        f"$names=@({quoted_names}); "
        "foreach($name in $names){ "
        "Get-Process -Name $name -ErrorAction SilentlyContinue | "
        f"ForEach-Object {{ try {{ $_.PriorityClass = '{priority_name}' }} catch {{}} }} "
        "}"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=3,
            **hidden_subprocess_kwargs(),
        )
        return result.returncode == 0
    except Exception:
        return False


def apply_resource_limits(profile):
    if not PERFORMANCE_GOVERNOR_ENABLED:
        return

    external_processes = tuple(normalize_process_name(name) for name in parse_csv(PERFORMANCE_GAMING_EXTERNAL_PROCESSES))
    if profile.active:
        current_priority = priority_key(PERFORMANCE_GAMING_PROCESS_PRIORITY)
        if _PRIORITY_STATE.get("current") != current_priority:
            if set_current_process_priority(current_priority):
                _PRIORITY_STATE["current"] = current_priority

        external_priority = priority_key(PERFORMANCE_GAMING_EXTERNAL_PRIORITY)
        external_state = (external_priority, external_processes)
        if _PRIORITY_STATE.get("external") != external_state:
            if set_external_process_priority(external_processes, external_priority):
                _PRIORITY_STATE["external"] = external_state
                _PRIORITY_STATE["external_processes"] = external_processes
        return

    if not PERFORMANCE_RESTORE_PRIORITY_ON_NORMAL:
        return

    if _PRIORITY_STATE.get("current") and _PRIORITY_STATE.get("current") != "normal":
        if set_current_process_priority("normal"):
            _PRIORITY_STATE["current"] = "normal"

    processes = _PRIORITY_STATE.get("external_processes") or external_processes
    if processes and set_external_process_priority(processes, "normal"):
        _PRIORITY_STATE["external"] = None
        _PRIORITY_STATE["external_processes"] = ()


def read_process_names():
    if os.name != "nt":
        return set()

    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-Process | Select-Object -ExpandProperty ProcessName",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3,
            **hidden_subprocess_kwargs(),
        )
    except Exception:
        return set()

    if result.returncode != 0:
        return set()

    return {
        normalize_process_name(line)
        for line in result.stdout.splitlines()
        if line.strip()
    }


def read_cpu_load_pct():
    if os.name != "nt":
        return None

    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        "(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3,
            **hidden_subprocess_kwargs(),
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    try:
        return float(str(result.stdout).strip())
    except Exception:
        return None


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def read_free_memory_gb():
    if os.name != "nt":
        return None

    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return round(status.ullAvailPhys / (1024 ** 3), 2)
    except Exception:
        return None


def current_performance_profile(force_refresh=False):
    now = time.monotonic()
    cached = _PROFILE_CACHE.get("profile")
    if not force_refresh and cached and now < float(_PROFILE_CACHE.get("expires_at") or 0):
        return cached

    if not PERFORMANCE_GOVERNOR_ENABLED:
        profile = PerformanceProfile(active=False, mode="normal", reasons=("governor disabled",))
        _PROFILE_CACHE.update({"profile": profile, "expires_at": now + 60})
        return profile

    mode = str(PERFORMANCE_MODE or "auto").strip().lower()
    if mode in {"gaming", "game", "quiet", "low", "low-power"}:
        profile = PerformanceProfile(active=True, mode="gaming", reasons=(f"PERFORMANCE_MODE={mode}",))
        apply_resource_limits(profile)
        _PROFILE_CACHE.update({"profile": profile, "expires_at": now + max(5, PERFORMANCE_PROFILE_TTL_SECONDS)})
        return profile
    if mode in {"normal", "off", "disabled"}:
        profile = PerformanceProfile(active=False, mode="normal", reasons=(f"PERFORMANCE_MODE={mode}",))
        apply_resource_limits(profile)
        _PROFILE_CACHE.update({"profile": profile, "expires_at": now + max(5, PERFORMANCE_PROFILE_TTL_SECONDS)})
        return profile

    reasons = []
    matched = ()
    process_names = read_process_names()
    configured_games = {normalize_process_name(name) for name in parse_csv(PERFORMANCE_GAME_PROCESS_NAMES)}
    if process_names and configured_games:
        matched = tuple(sorted(process_names & configured_games))
        if matched:
            reasons.append("game process: " + ", ".join(matched[:3]))

    cpu_pct = read_cpu_load_pct()
    if cpu_pct is not None and cpu_pct >= float(PERFORMANCE_BUSY_CPU_PCT):
        reasons.append(f"CPU busy: {cpu_pct:.0f}%")

    free_memory_gb = read_free_memory_gb()
    if free_memory_gb is not None and free_memory_gb <= float(PERFORMANCE_LOW_MEMORY_GB):
        reasons.append(f"low free RAM: {free_memory_gb:.1f}GB")

    active = bool(reasons)
    profile = PerformanceProfile(
        active=active,
        mode="gaming" if active else "normal",
        reasons=tuple(reasons or ("system load normal",)),
        cpu_pct=cpu_pct,
        free_memory_gb=free_memory_gb,
        matched_processes=matched,
    )
    apply_resource_limits(profile)
    _PROFILE_CACHE.update(
        {
            "profile": profile,
            "expires_at": now + max(5, int(PERFORMANCE_PROFILE_TTL_SECONDS)),
        }
    )
    return profile


def describe_performance_profile(profile=None):
    profile = profile or current_performance_profile()
    if not profile.active:
        return "normal"

    reason_text = "; ".join(profile.reasons[:3])
    return f"gaming/quiet mode ({reason_text})"


def print_profile_notice(key, prefix="[PERFORMANCE]"):
    profile = current_performance_profile()
    if not profile.active:
        return profile

    now = time.monotonic()
    last = float(_NOTICE_CACHE.get(key) or 0)
    if now - last >= 120:
        print(f"{prefix} Performance governor active: {describe_performance_profile(profile)}")
        _NOTICE_CACHE[key] = now
    return profile


def clamp_int(value, cap, minimum=1):
    try:
        numeric = int(value)
    except Exception:
        numeric = minimum
    try:
        cap_numeric = int(cap)
    except Exception:
        cap_numeric = numeric
    if cap_numeric <= 0:
        return max(minimum, numeric)
    return max(minimum, min(numeric, cap_numeric))


def adjust_ollama_runtime(
    task,
    keep_alive=None,
    timeout_seconds=None,
    num_predict=None,
    batch_size=None,
    max_items=None,
    workers=None,
    warmup=None,
):
    profile = current_performance_profile()
    result = {
        "profile": profile,
        "keep_alive": keep_alive,
        "timeout_seconds": timeout_seconds,
        "num_predict": num_predict,
        "batch_size": batch_size,
        "max_items": max_items,
        "workers": workers,
        "warmup": warmup,
    }
    if not profile.active:
        return result

    if keep_alive is not None:
        result["keep_alive"] = PERFORMANCE_GAMING_KEEP_ALIVE
    if timeout_seconds is not None:
        result["timeout_seconds"] = clamp_int(timeout_seconds, PERFORMANCE_GAMING_TIMEOUT_SECONDS, minimum=10)
    if num_predict is not None:
        result["num_predict"] = clamp_int(num_predict, PERFORMANCE_GAMING_MAX_NUM_PREDICT, minimum=32)
    if batch_size is not None:
        result["batch_size"] = clamp_int(batch_size, PERFORMANCE_GAMING_MAX_BATCH_SIZE, minimum=1)
    if max_items is not None:
        result["max_items"] = clamp_int(max_items, PERFORMANCE_GAMING_MAX_LLM_ITEMS, minimum=1)
    if workers is not None:
        result["workers"] = clamp_int(workers, PERFORMANCE_GAMING_MAX_WORKERS, minimum=1)
    if warmup is not None:
        result["warmup"] = False

    return result


def adjust_ticker_count(count):
    profile = current_performance_profile()
    if not profile.active:
        return int(count)
    return clamp_int(count, PERFORMANCE_GAMING_MAX_LLM_TICKERS, minimum=1)


def adjust_poll_interval(seconds):
    profile = current_performance_profile()
    base = max(30, int(seconds))
    if not profile.active:
        return base

    try:
        multiplier = max(1.0, float(PERFORMANCE_GAMING_INTERVAL_MULTIPLIER))
    except Exception:
        multiplier = 2.0
    return int(base * multiplier)


def gaming_budget_pause(task, estimated_work_seconds=1.0, critical=False):
    profile = current_performance_profile()
    if not profile.active or not PERFORMANCE_GAMING_THROTTLE_ENABLED:
        return 0.0

    try:
        budget = max(1.0, min(100.0, float(PERFORMANCE_GAMING_CPU_BUDGET_PCT)))
    except Exception:
        budget = 10.0

    if budget >= 100.0:
        return 0.0

    try:
        work_seconds = max(0.1, float(estimated_work_seconds))
    except Exception:
        work_seconds = 1.0

    sleep_seconds = work_seconds * ((100.0 / budget) - 1.0)
    if profile.cpu_pct is not None:
        try:
            load_multiplier = max(1.0, min(2.5, float(profile.cpu_pct) / max(1.0, float(PERFORMANCE_BUSY_CPU_PCT))))
            sleep_seconds *= load_multiplier
        except Exception:
            pass

    min_sleep = max(0.0, float(PERFORMANCE_GAMING_THROTTLE_MIN_SLEEP_SECONDS))
    max_sleep = max(min_sleep, float(PERFORMANCE_GAMING_THROTTLE_MAX_SLEEP_SECONDS))
    sleep_seconds = max(min_sleep, min(max_sleep, sleep_seconds))
    if critical:
        sleep_seconds = min(1.0, sleep_seconds)

    now = time.monotonic()
    notice_key = f"throttle:{task}"
    last = float(_NOTICE_CACHE.get(notice_key) or 0)
    if now - last >= 120:
        print(
            f"[PERFORMANCE] 10% budget throttle: sleeping {sleep_seconds:.1f}s "
            f"before {task} ({describe_performance_profile(profile)})"
        )
        _NOTICE_CACHE[notice_key] = now

    time.sleep(sleep_seconds)
    return sleep_seconds


def should_defer_work(kind, name):
    profile = current_performance_profile()
    if not profile.active:
        return False

    target = f"{kind}:{name}".strip().lower()
    name_only = str(name or "").strip().lower()
    defer_tokens = {item.strip().lower() for item in parse_csv(PERFORMANCE_DEFER_TASKS)}
    return target in defer_tokens or name_only in defer_tokens or f"{kind}:*" in defer_tokens
