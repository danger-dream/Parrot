"""Explicit profile platforms; unsupported native schemas are not relabelled Linux."""
from __future__ import annotations

DEFAULT = "linux-x64"
LOGIN_DEFAULT = "macos-arm64"
DISPLAY_ORDER = ("macos-arm64", "windows-x64", "linux-x64", "linux-arm64")
PLATFORMS = {
    "linux-x64": {"label": "Linux · x86_64", "machine_os": "x86_64_linux", "process": "runtime-info-linux-x64", "sdk": "1.5.0", "os": 8, "tls": False, "supported": True},
    "linux-arm64": {"label": "Linux · ARM64", "machine_os": "aarch64_linux", "process": "runtime-info-linux-arm64", "sdk": "1.5.0", "os": 8, "tls": False, "supported": True},
    "macos-x64": {"label": "macOS · Intel", "supported": False},
    "macos-arm64": {"label": "macOS · Apple Silicon（M 系列）", "machine_os": "aarch64_darwin", "process": "runtime-info-darwin-arm64", "sdk": "1.1.2", "os": 7, "tls": True, "supported": True},
    "windows-x64": {"label": "Windows · x64", "machine_os": "x86_64_win32", "process": "runtime-info-win32-x64.exe", "sdk": "1.0.0", "environment": 1005, "os": 6, "tls": False, "supported": True},
    "windows-arm64": {"label": "Windows · ARM64", "supported": False},
}


def resolve(platform: str) -> dict:
    if not isinstance(platform, str) or platform not in PLATFORMS or not PLATFORMS[platform]["supported"]:
        raise ValueError("Unsupported Qoder device platform")
    return PLATFORMS[platform]


def for_process(process: str) -> dict:
    for value in PLATFORMS.values():
        if isinstance(process, str) and value.get("process") == process:
            return value
    raise ValueError("Unsupported UMID process")


def for_profile(profile: dict) -> str:
    value = profile.get("platform", DEFAULT)
    resolve(value)
    return value


def choices() -> list[dict]:
    return [{"id": key, "label": PLATFORMS[key]["label"], "supported": True}
            for key in DISPLAY_ORDER if PLATFORMS[key]["supported"]]
