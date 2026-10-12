"""Resolve one Jieli build into the machine-readable build plan.

The plan records every decision the build makes about *where* things are: the
SDK checkout and its source root, the toolchain, the component providers, and
the evaluated config that selected them. It exists because that resolution
used to be written three times -- toolchain_file.cmake, platform_config.cmake
and build_example.py -- in two languages, with two different meanings for the
same JIELI_SDK_ROOT name. The plan names the two roots separately so the
confusion cannot survive a consumer.

Two different contracts meet here, and they are deliberately not the same:

* The resolver itself never blocks a configure. A plan that cannot be resolved,
  or that fails validation, is reported and dropped, and the CLI always exits 0
  because the configure step that calls it must not depend on it.
* The plan is nevertheless the authority for the build. build_example.py
  consumes it and fails when it is missing, stale, or contradicts the build
  parameters -- so a configure whose resolver skipped is caught at the build
  step rather than silently resolved a second time.

Retiring the first contract is a separate step; until then a consumer that
needs the plan has to say so, which is what build_example.py does.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

# Invoked as a script, only tools/jieli_build is on sys.path, so the platform
# root -- the parent of the tools package -- has to be added before the
# package imports below can resolve. That is how toolchain_file.cmake calls it.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.jieli_build.chip_profiles import (
    JIELI_CHIPS,
    PLATFORM_ROOT,
    resolve_chip,
    resolve_sdk_root,
)
from tools.jieli_build.errors import BuildError
from tools.jieli_build.toolchain import resolve_tool_dir, resolve_tool_path

SCHEMA_VERSION = 1
PROTOCOL_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)
SUPPORTED_PROTOCOL_VERSIONS = (PROTOCOL_VERSION,)
PLATFORM_NAME = "JIELI"

# Where the plan lands when the caller does not name a file: inside the build
# tree, next to the evaluated config it was derived from.
DEFAULT_PLAN_RELATIVE = Path("cache") / "jieli" / "build_plan.json"

# The board catalog declares every Jieli board twice in Kconfig: the canonical
# name in CONFIG_BOARD_CHOICE and a selection flag whose symbol is that name
# upper-cased. Both halves are required so a stale or aliased flag cannot
# select a board whose profile was not actually chosen. build_example.py reads
# the same pair; the plan stores the flag it matched so a consumer can re-check
# the pair without re-reading the config.
BOARD_CHOICE_PREFIX = "CONFIG_BOARD_CHOICE_"
TRUTHY_VALUES = ("y", "true", "True")

# The tool paths a plan consumer needs by name. They are the same four
# toolchain_file.cmake hands to CMake; recording them means a consumer does not
# have to repeat the alias search in TOOL_ALIASES.
PLAN_TOOLS = ("c_compiler", "archiver", "ranlib", "objdump")

# Provider ids name the implementation a consumer links against, not the
# directory it happens to live in. The tables map an observed layout onto a
# canonical id, so the plan never claims a provider the tree does not carry.
TLS_PROVIDERS = {"mbedtls-3.1.0": "tuyaopen_mbedtls_3_1"}
LWIP_PROVIDERS = {"lwip-2.1.2": "tuyaopen_lwip_2_1"}
LVGL_PROVIDER = "tuyaopen_lvgl"
VENDOR_LWIP_PROVIDER = "jieli_vendor_lwip_2_2"
VENDOR_LWIP_RELATIVE = Path("include_lib/net/lwip_2_2_0")


def parse_config(path: Path) -> dict[str, str]:
    """Read a Kconfig-evaluated using.config into a flat mapping.

    Commented-out symbols ("# CONFIG_X is not set") are dropped rather than
    recorded as false: a missing key already means "not set", which is exactly
    what the generated comment says.
    """
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"')
    return values


def config_digest(path: Path) -> str:
    """Digest the evaluated config so a plan can be tied to one configuration.

    Only the config content is covered -- not the plan itself and not any build
    artifact -- so the digest can be recomputed from the input alone without
    the digest feeding back into what it summarises. Line endings are
    normalised first so the same config digests the same on a Windows and on a
    Linux checkout.
    """
    text = path.read_text(encoding="utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_path(path: os.PathLike | str) -> str:
    """Render a path the way every plan field does: absolute, forward slashes.

    A plan is read from a different working directory than the one it was
    written in, so relative paths would silently mean something else.
    """
    return Path(path).expanduser().resolve().as_posix()


def resolve_board(params: Mapping[str, str]) -> str:
    """Return the selected board profile, confirmed by its Kconfig choice flag."""
    board = params.get("CONFIG_BOARD_CHOICE", "").strip()
    if not board:
        raise BuildError("CONFIG_BOARD_CHOICE is missing from the evaluated config")
    flag = BOARD_CHOICE_PREFIX + board.upper()
    if params.get(flag) not in TRUTHY_VALUES:
        raise BuildError(
            f"CONFIG_BOARD_CHOICE={board} is not confirmed by {flag}=y"
        )
    return board


def resolve_uart(params: Mapping[str, str], chip) -> dict[str, int]:
    """Return the log UART the staging hooks program into the vendor board file.

    The defaults mirror build_example.py: wl82 logs on SDK UART1 (the AC79
    DevKitBoard wiring), the AC792 profile on UART0.
    """
    default_port = "1" if chip.name == "wl82" else "0"
    try:
        return {
            "log_port": int(params.get("CONFIG_JIELI_UART_LOG_PORT", default_port)),
            "log_baudrate": int(params.get("CONFIG_JIELI_UART_LOG_BAUDRATE", "115200")),
        }
    except ValueError as exc:
        raise BuildError(f"Jieli log UART settings are not integers: {exc}") from exc


def resolve_tools(tool_dir: Path, module_root: Path = PLATFORM_ROOT) -> dict[str, str]:
    """Resolve the tool paths toolchain_file.cmake selects for this host.

    On Windows the archiver and ranlib are the platform wrappers, not the
    toolchain's lto-ar: the legacy lto-ar cannot index the pi32v2 bitcode
    archives the full TuyaOpen image produces, and a host ranlib cannot either.
    That choice is repeated here verbatim from toolchain_file.cmake.
    """
    if os.name == "nt":
        archiver = module_root / "jieli_llvm_ar.cmd"
        ranlib = module_root / "jieli_ranlib.cmd"
    else:
        archiver = resolve_tool_path(tool_dir, "lto-ar")
        ranlib = module_root / "jieli_ranlib.sh"
    return {
        "c_compiler": canonical_path(resolve_tool_path(tool_dir, "clang")),
        "archiver": canonical_path(archiver),
        "ranlib": canonical_path(ranlib),
        "objdump": canonical_path(resolve_tool_path(tool_dir, "objdump")),
    }


def _vendored_provider(module_dir: Path, table: Mapping[str, str]) -> Optional[str]:
    """Return the provider id for the library vendored under a src component."""
    if not module_dir.is_dir():
        return None
    for directory, provider in table.items():
        if (module_dir / directory).is_dir():
            return provider
    return None


def resolve_providers(
    open_root: Path, sdk_source_root: Path, params: Mapping[str, str]
) -> dict[str, Optional[str]]:
    """Name the implementation behind each replaceable component.

    A null provider means the build does not carry one, which is different
    from "unknown": every value is read off the tree or the evaluated config.
    TuyaOpen's TLS stack is unconditional; lwIP and LVGL are opt-in, and the
    Jieli build takes the vendor lwIP when its own is not enabled.
    """
    providers: dict[str, Optional[str]] = {
        "tls": _vendored_provider(open_root / "src" / "libtls", TLS_PROVIDERS),
        "lwip": None,
        "lvgl": None,
    }
    if params.get("CONFIG_ENABLE_LIBLWIP") in TRUTHY_VALUES:
        providers["lwip"] = _vendored_provider(
            open_root / "src" / "liblwip", LWIP_PROVIDERS
        )
    elif (sdk_source_root / VENDOR_LWIP_RELATIVE).is_dir():
        providers["lwip"] = VENDOR_LWIP_PROVIDER
    if (
        params.get("CONFIG_ENABLE_LIBLVGL") in TRUTHY_VALUES
        and (open_root / "src" / "liblvgl").is_dir()
    ):
        providers["lvgl"] = LVGL_PROVIDER
    return providers


def chip_probe_path(chip, sdk_source_root: Path) -> Path:
    """Rebase a chip profile's probe from the SDK checkout onto the source root.

    The profile states the probe relative to the checkout it discovers, and the
    two roots differ wherever sdk_source_relative is not "." (wl83 keeps the
    source tree one level down in an AC792_SDK checkout).
    """
    relative = chip.probe
    if chip.sdk_source_relative != Path("."):
        try:
            relative = chip.probe.relative_to(chip.sdk_source_relative)
        except ValueError:
            relative = chip.probe
    return sdk_source_root / relative


def build_plan(
    config_path: Path,
    open_root: Path,
    build_dir: Path,
    bin_output_dir: Path,
    environ: Optional[Mapping[str, str]] = None,
    module_root: Path = PLATFORM_ROOT,
) -> dict[str, Any]:
    """Resolve one build into the plan schema, then validate it.

    The evaluated Kconfig config is the only authority for what was selected.
    The environment is consulted only for the SDK and toolchain locations, the
    same way build_example.py consults it, so the plan describes the build the
    existing helpers would produce.
    """
    env = os.environ if environ is None else environ
    config_path = Path(config_path)
    params = parse_config(config_path)

    chip_choice = params.get("CONFIG_CHIP_CHOICE", "").strip()
    if not chip_choice:
        raise BuildError("CONFIG_CHIP_CHOICE is missing from the evaluated config")
    chip = resolve_chip(env, chip_name=chip_choice)
    board = resolve_board(params)

    sdk_checkout_root = resolve_sdk_root(env, module_root=module_root, chip_name=chip.name)
    sdk_source_root = sdk_checkout_root / chip.sdk_source_relative
    tool_dir = resolve_tool_dir(sdk_checkout_root, env, module_root)

    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "platform": PLATFORM_NAME,
        "chip": chip.name,
        "cpu": chip.cpu,
        "board": board,
        "board_choice_flag": BOARD_CHOICE_PREFIX + board.upper(),
        "sdk_checkout_root": canonical_path(sdk_checkout_root),
        "sdk_source_root": canonical_path(sdk_source_root),
        "toolchain_root": canonical_path(tool_dir),
        "config_digest": config_digest(config_path),
        "tools": resolve_tools(tool_dir, module_root),
        "paths": {
            "open_root": canonical_path(open_root),
            "build_dir": canonical_path(build_dir),
            "bin_output_dir": canonical_path(bin_output_dir),
            "config": canonical_path(config_path),
        },
        "providers": resolve_providers(open_root, sdk_source_root, params),
        "uart": resolve_uart(params, chip),
    }
    validate_plan(plan)
    return plan


def _require(plan: Mapping[str, Any], key: str) -> Any:
    value = plan.get(key)
    if value is None or value == "":
        raise BuildError(f"build plan is missing {key!r}")
    return value


def validate_plan(plan: Mapping[str, Any]) -> None:
    """Reject a plan whose claims the host cannot back up.

    Every rule is a claim a consumer would otherwise trust blindly: the schema
    it was written against, the chip it names, the SDK probe that chip profile
    defines, the tool executables, and the board's Kconfig pair. A plan that
    passes here still describes a build that may fail for other reasons; it
    only guarantees that the paths and the selection are self-consistent.
    """
    version = plan.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise BuildError(
            f"unsupported build plan schema_version {version!r}; this resolver "
            f"writes {list(SUPPORTED_SCHEMA_VERSIONS)}"
        )
    protocol = plan.get("protocol_version")
    if protocol not in SUPPORTED_PROTOCOL_VERSIONS:
        raise BuildError(
            f"unsupported build plan protocol_version {protocol!r}; this "
            f"resolver speaks {list(SUPPORTED_PROTOCOL_VERSIONS)}"
        )
    if plan.get("platform") != PLATFORM_NAME:
        raise BuildError(f"build plan is not a {PLATFORM_NAME} plan: {plan.get('platform')!r}")

    chip = JIELI_CHIPS.get(plan.get("chip", ""))
    if chip is None:
        raise BuildError(
            f"build plan names unknown chip {plan.get('chip')!r}; expected one "
            f"of {sorted(JIELI_CHIPS)}"
        )

    board = str(_require(plan, "board"))
    expected_flag = BOARD_CHOICE_PREFIX + board.upper()
    if plan.get("board_choice_flag") != expected_flag:
        raise BuildError(
            f"build plan board {board!r} is not confirmed by its Kconfig choice "
            f"flag: expected {expected_flag}, got {plan.get('board_choice_flag')!r}"
        )

    sdk_source_root = Path(_require(plan, "sdk_source_root"))
    probe = chip_probe_path(chip, sdk_source_root)
    if not probe.is_file():
        raise BuildError(
            f"Jieli SDK profile does not match chip '{chip.name}': {probe} is missing"
        )
    sdk_checkout_root = Path(_require(plan, "sdk_checkout_root"))
    if sdk_source_root != sdk_checkout_root and not sdk_source_root.is_relative_to(
        sdk_checkout_root
    ):
        raise BuildError(
            f"build plan sdk_source_root {sdk_source_root} is not inside its "
            f"sdk_checkout_root {sdk_checkout_root}"
        )

    tools = plan.get("tools") or {}
    for name in PLAN_TOOLS:
        tool = tools.get(name)
        if not tool or not Path(tool).is_file():
            raise BuildError(f"build plan toolchain is missing {name}: {tool!r}")

    paths = plan.get("paths") or {}
    for name in ("open_root", "build_dir", "bin_output_dir"):
        if not paths.get(name):
            raise BuildError(f"build plan is missing paths.{name}")


def write_plan(plan: Mapping[str, Any], path: Path) -> Path:
    """Write the plan atomically: a sibling temp file, then os.replace.

    The temporary file is created in the destination directory so the replace
    stays a rename on one filesystem. A plain file and a rename need no symlink
    privilege on Windows.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(plan, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_plan.py",
        description=(
            "Resolve the Jieli build plan. Shadow only: the plan is written for "
            "other tools to read and never feeds back into this build."
        ),
    )
    parser.add_argument("--config", required=True, help="evaluated Kconfig using.config")
    parser.add_argument("--open-root", required=True, help="TuyaOpen root")
    parser.add_argument("--build-dir", required=True, help="CMake binary directory")
    parser.add_argument("--bin-output-dir", required=True, help="firmware output directory")
    parser.add_argument(
        "--out",
        default=None,
        help=f"plan path (default: <build-dir>/{DEFAULT_PLAN_RELATIVE.as_posix()})",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Resolve and write the plan, reporting failure without failing the build.

    Shadow-only contract: the caller is a configure step that must not depend
    on this, so every exit path returns 0 and every problem is a warning on
    stderr.
    """
    try:
        args = _parse_args(sys.argv[1:] if argv is None else argv)
        build_dir = Path(args.build_dir)
        out = Path(args.out) if args.out else build_dir / DEFAULT_PLAN_RELATIVE
        plan = build_plan(
            config_path=Path(args.config),
            open_root=Path(args.open_root),
            build_dir=build_dir,
            bin_output_dir=Path(args.bin_output_dir),
        )
        write_plan(plan, out)
    except SystemExit as exc:
        if exc.code not in (0, None):
            print("[JIELI] build plan resolver skipped: invalid arguments", file=sys.stderr)
        return 0
    except Exception as exc:  # shadow only: nothing may fail the caller
        print(f"[JIELI] build plan resolver skipped: {exc}", file=sys.stderr)
        return 0
    print(f"[JIELI] build plan: {canonical_path(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
