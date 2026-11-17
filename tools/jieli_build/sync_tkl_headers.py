"""Synchronize JIELI TKL headers against their reviewed contracts.

Run from ``platform/JIELI`` with ``python -m tools.jieli_build.sync_tkl_headers``.
The 8080 declaration is intentionally based on T5AI's reviewed de-duplicated
header. ``tuya_cloud_types.h`` is check-only: its sole permitted difference
from tools/porting is the approved JIELI GPIO64..GPIO127 enum extension.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import Optional, Sequence


PLATFORM_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OPEN_ROOT = Path(__file__).resolve().parents[4]
PLATFORM_INCLUDE = Path("tuyaos/tuyaos_adapter/include")
PORTING_INCLUDE = Path("tools/porting/adapter")
T5AI_INCLUDE = Path("platform/T5AI/tuyaos/tuyaos_adapter/include")

PORTED_HEADERS = (
    "adc/tkl_adc.h",
    "bluetooth/tkl_bluetooth_def.h",
    "i2c/tkl_i2c.h",
    "kws/tkl_kws.h",
    "mcu8080/tkl_8080.h",
    "media/tkl_audio.h",
    "media/tkl_media.h",
    "pinmux/tkl_pinmux.h",
    "pwm/tkl_pwm.h",
    "spi/tkl_spi.h",
    "system/tkl_fs.h",
    "timer/tkl_timer.h",
    "vad/tkl_vad.h",
    "security/tkl_hash.h",
    "security/tkl_symmetry.h",
    "security/tkl_asymmetrical.h",
    "network/tkl_lwip.h",
)
CLOUD_TYPES_HEADER = "utilities/include/tuya_cloud_types.h"
_GPIO_ENUM = re.compile(
    rb"typedef enum\s*\{(?P<body>.*?)\}\s*TUYA_GPIO_NUM_E\s*;",
    re.DOTALL,
)
_GPIO_VALUE = re.compile(rb"\bTUYA_GPIO_NUM_(\d+|MAX)\b")


class HeaderSyncError(Exception):
    """A required reference is missing or cannot be read."""


def _reference_path(open_root: Path, relative: str) -> Path:
    reference_root = T5AI_INCLUDE if relative == "mcu8080/tkl_8080.h" else PORTING_INCLUDE
    return open_root / reference_root / relative


def _local_path(platform_root: Path, relative: str) -> Path:
    return platform_root / PLATFORM_INCLUDE / relative


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _approved_cloud_delta(upstream: bytes, local: bytes) -> Optional[str]:
    """Accept only the reviewed 64..127 enum append, preserving all other bytes."""
    upstream_match = _GPIO_ENUM.search(upstream)
    local_match = _GPIO_ENUM.search(local)
    if upstream_match is None or local_match is None:
        return "cannot locate TUYA_GPIO_NUM_E enum"

    upstream_values = _GPIO_VALUE.findall(upstream_match.group("body"))
    local_values = _GPIO_VALUE.findall(local_match.group("body"))
    expected_upstream = [str(i).encode() for i in range(64)] + [b"MAX"]
    expected_local = [str(i).encode() for i in range(128)] + [b"MAX"]
    if upstream_values != expected_upstream:
        return "tools/porting GPIO enum is not GPIO0..GPIO63 with MAX=64"
    if local_values != expected_local:
        return "JIELI GPIO enum is not GPIO0..GPIO127 with MAX=128"

    newline = b"\r\n" if b"\r\n" in upstream_match.group("body") else b"\n"
    extension = b"".join(
        f"    TUYA_GPIO_NUM_{i},".encode() + newline for i in range(64, 128)
    )
    expected_body = upstream_match.group("body").replace(
        b"    TUYA_GPIO_NUM_MAX,", extension + b"    TUYA_GPIO_NUM_MAX,", 1
    )
    expected_enum = (
        upstream_match.group(0)[: upstream_match.start("body") - upstream_match.start()]
        + expected_body
        + upstream_match.group(0)[upstream_match.end("body") - upstream_match.start() :]
    )
    expected_local_bytes = (
        upstream[: upstream_match.start()]
        + expected_enum
        + upstream[upstream_match.end() :]
    )
    if local != expected_local_bytes:
        return "cloud types differ outside the approved GPIO64..GPIO127 enum extension"
    return None


def sync_headers(open_root: Path, *, platform_root: Path = PLATFORM_ROOT) -> list[str]:
    """Overwrite only the reviewed JIELI TKL headers from their references."""
    open_root = Path(open_root).resolve()
    platform_root = Path(platform_root).resolve()
    contents: dict[str, bytes] = {}
    for relative in PORTED_HEADERS:
        reference = _reference_path(open_root, relative)
        try:
            contents[relative] = reference.read_bytes()
        except OSError as exc:
            raise HeaderSyncError(f"cannot read reviewed reference {reference}: {exc}") from exc

    for relative, data in contents.items():
        destination = _local_path(platform_root, relative)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        except OSError as exc:
            raise HeaderSyncError(f"cannot write JIELI header {destination}: {exc}") from exc
    return list(PORTED_HEADERS)


def check_headers(open_root: Path, *, platform_root: Path = PLATFORM_ROOT) -> list[str]:
    """Return every mismatch across ported headers and check-only cloud types."""
    open_root = Path(open_root).resolve()
    platform_root = Path(platform_root).resolve()
    problems: list[str] = []

    for relative in PORTED_HEADERS:
        reference = _reference_path(open_root, relative)
        local = _local_path(platform_root, relative)
        try:
            expected = reference.read_bytes()
        except OSError as exc:
            problems.append(f"{relative}: cannot read reviewed reference {reference}: {exc}")
            continue
        try:
            actual = local.read_bytes()
        except OSError as exc:
            problems.append(f"{relative}: cannot read JIELI header {local}: {exc}")
            continue
        if actual != expected:
            problems.append(
                f"{relative}: differs from {reference} "
                f"(JIELI sha256={_sha256(actual)}, reference sha256={_sha256(expected)})"
            )

    cloud_reference = open_root / PORTING_INCLUDE / CLOUD_TYPES_HEADER
    cloud_local = _local_path(platform_root, CLOUD_TYPES_HEADER)
    try:
        expected_cloud = cloud_reference.read_bytes()
        actual_cloud = cloud_local.read_bytes()
    except OSError as exc:
        problems.append(f"{CLOUD_TYPES_HEADER}: cannot compare shared contract: {exc}")
    else:
        delta = _approved_cloud_delta(expected_cloud, actual_cloud)
        if delta is not None:
            problems.append(
                f"{CLOUD_TYPES_HEADER}: {delta} "
                f"(JIELI sha256={_sha256(actual_cloud)}, upstream sha256={_sha256(expected_cloud)})"
            )
    return problems


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--open-root",
        type=Path,
        default=DEFAULT_OPEN_ROOT,
        help="TuyaOpen root containing tools/porting and platform/T5AI",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare all ported headers and cloud types without writing files",
    )
    args = parser.parse_args(argv)

    try:
        if args.check:
            problems = check_headers(args.open_root)
            if problems:
                for problem in problems:
                    print(f"MISMATCH: {problem}", file=sys.stderr)
                print(
                    f"Checked {len(PORTED_HEADERS)} ported headers and "
                    f"{CLOUD_TYPES_HEADER}; {len(problems)} mismatch(es).",
                    file=sys.stderr,
                )
                return 1
            print(f"Checked {len(PORTED_HEADERS)} ported headers and {CLOUD_TYPES_HEADER}; all match.")
            return 0

        synced = sync_headers(args.open_root)
        print(f"Synchronized {len(synced)} reviewed TKL headers; {CLOUD_TYPES_HEADER} was not touched.")
        return 0
    except HeaderSyncError as exc:
        print(f"TKL header synchronization failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
