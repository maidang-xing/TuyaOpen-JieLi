#!/usr/bin/env python3
"""Build and package a TuyaOpen example with a Jieli chip SDK.

The build phase consumes the plan CMake resolved during configure
(``<build-dir>/cache/jieli/build_plan.json``) instead of resolving the chip, the
SDK and the toolchain a second time. Configure and build run in different
processes at different moments, so every value the plan asserts is re-checked
against this host before it is used, and a plan that no longer describes this
build fails loudly instead of falling back to an independent resolution.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

from tools.jieli_build.board_facts import load_board_facts
from tools.jieli_build.build_plan import chip_probe_path, config_digest, parse_config
from tools.jieli_build.chip_profiles import JIELI_CHIPS, PLATFORM_ROOT
from tools.jieli_build.errors import BuildError
from tools.jieli_build.packaging import find_qio_artifact, run_postbuild, tuya_qio_name
from tools.jieli_build.sdk_overlay import build_make_command, clean_staging_tree, create_staging_tree
from tools.jieli_build.toolchain import resolve_tool_path


# The plan and the evaluated config it digests both live under the CMake binary
# directory. The build hook is handed <binary-dir>/build, so both are one level
# up from the parameter directory it receives.
PLAN_RELATIVE = Path("cache") / "jieli" / "build_plan.json"
CONFIG_RELATIVE = Path("cache") / "using.config"

# What this consumer understands. Deliberately not imported from build_plan: a
# handshake that reads the producer's own constants would accept a producer that
# moved ahead of this consumer without a single line changing here.
SUPPORTED_PLAN_SCHEMA_VERSIONS = (1,)
SUPPORTED_PLAN_PROTOCOL_VERSIONS = (1,)
PLAN_PLATFORM = "JIELI"

# The plan tool paths this consumer reads by name. The build only needs the
# compiler itself and the objdump it exports to the vendor Makefile; the
# archiver and ranlib are CMake's business.
CONSUMED_PLAN_TOOLS = ("c_compiler", "objdump")

# Set by a user, read by two phases with two different meanings -- see
# _reject_ambiguous_sdk_root.
SDK_ROOT_ENV = "JIELI_SDK_ROOT"


def parse_build_params(path: Path) -> dict[str, str]:
    """Read the generated build parameters, in the evaluated config's format."""
    return parse_config(path)


# The board catalog under boards/JIELI declares each board twice in Kconfig: the
# canonical name in CONFIG_BOARD_CHOICE and a selection flag whose symbol is
# that name upper-cased (config BOARD_CHOICE_<NAME>). Reading the pair instead
# of hardcoding a board name is what lets a new board be added by editing
# boards/JIELI only. Both halves are required so a stale or aliased flag cannot
# select a board whose profile was not actually chosen.
BOARD_CHOICE_FLAG_PREFIX = "CONFIG_BOARD_CHOICE_"
_TRUTHY = ("y", "true", "True")


def resolve_board_name(params: dict[str, str]) -> str | None:
    """Return the selected Jieli board profile used by the staging hooks.

    The selection is data, not code: the board name comes from the generated
    build parameters and is confirmed by its matching Kconfig choice flag, so
    the platform carries no per-board branch.
    """
    board = params.get("CONFIG_BOARD_CHOICE", "").strip()
    if not board:
        return None
    if params.get(BOARD_CHOICE_FLAG_PREFIX + board.upper()) in _TRUTHY:
        return board
    return None


def _same_path(left: object, right: object) -> bool:
    """Compare two paths the way the plan records them, not as strings.

    canonical_path resolves a path before rendering a plan field, so a value
    that names the same directory through a symlink or a relative segment would
    not compare equal to it as text. Resolve both sides, and let Path's own
    comparison apply the host's case and separator rules.
    """
    try:
        return Path(str(left)).resolve() == Path(str(right)).resolve()
    except OSError:
        return False


def _is_within(path: Path, root: Path) -> bool:
    """Whether path is root itself or lives underneath it."""
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except OSError:
        return False


def _plan_versions(plan: Mapping[str, Any], plan_path: Path) -> None:
    schema = plan.get("schema_version")
    if schema not in SUPPORTED_PLAN_SCHEMA_VERSIONS:
        raise BuildError(
            f"build plan {plan_path} has schema_version {schema!r}; this build "
            f"reads {list(SUPPORTED_PLAN_SCHEMA_VERSIONS)}. The configure step "
            f"wrote a plan this build cannot read; re-run configure."
        )
    protocol = plan.get("protocol_version")
    if protocol not in SUPPORTED_PLAN_PROTOCOL_VERSIONS:
        raise BuildError(
            f"build plan {plan_path} has protocol_version {protocol!r}; this "
            f"build speaks {list(SUPPORTED_PLAN_PROTOCOL_VERSIONS)}. The configure "
            f"step wrote a plan this build cannot read; re-run configure."
        )
    platform = plan.get("platform")
    if platform != PLAN_PLATFORM:
        raise BuildError(
            f"build plan {plan_path} is not a {PLAN_PLATFORM} plan: expected "
            f"platform {PLAN_PLATFORM!r}, found {platform!r}"
        )


def _plan_selection(plan: Mapping[str, Any], params: Mapping[str, str]) -> None:
    """The generated parameters and the plan must name the same build."""
    chip_choice = params.get("CONFIG_CHIP_CHOICE", "").strip()
    if chip_choice != plan.get("chip"):
        raise BuildError(
            f"build parameters and build plan disagree on the chip: "
            f"CONFIG_CHIP_CHOICE is {chip_choice!r}, the plan resolved "
            f"{plan.get('chip')!r}. The plan is stale; re-run configure."
        )
    board_choice = params.get("CONFIG_BOARD_CHOICE", "").strip()
    if board_choice != plan.get("board"):
        raise BuildError(
            f"build parameters and build plan disagree on the board: "
            f"CONFIG_BOARD_CHOICE is {board_choice!r}, the plan resolved "
            f"{plan.get('board')!r}. The plan is stale; re-run configure."
        )


def _plan_config_freshness(plan: Mapping[str, Any], build_dir: Path) -> None:
    """Reject a plan resolved against a different evaluated config."""
    config_path = build_dir / CONFIG_RELATIVE
    if not config_path.is_file():
        raise BuildError(f"evaluated config is missing: expected {config_path}")
    recorded = plan.get("config_digest")
    current = config_digest(config_path)
    if recorded != current:
        raise BuildError(
            f"build plan is stale: {config_path} changed after the plan was "
            f"resolved. Plan config_digest {recorded}, current config digest "
            f"{current}. Re-run configure."
        )


def _required_plan_path(plan: Mapping[str, Any], key: str) -> Path:
    """Read a plan path field, refusing an absent or empty one.

    An empty string would otherwise resolve to the working directory and be
    reported as a probe failure somewhere further along.
    """
    value = plan.get(key)
    if not value:
        raise BuildError(f"build plan is missing the {key} path: found {value!r}")
    return Path(str(value))


def _plan_locations(plan: Mapping[str, Any]) -> None:
    """Back up the SDK and toolchain the plan names before trusting them."""
    sdk_source_root = _required_plan_path(plan, "sdk_source_root")
    if not sdk_source_root.is_dir():
        raise BuildError(
            f"build plan field sdk_source_root does not name a directory: expected "
            f"an existing SDK source root, found {sdk_source_root}"
        )
    chip = JIELI_CHIPS.get(plan.get("chip"))
    if chip is None:
        raise BuildError(
            f"build plan names unknown chip {plan.get('chip')!r}; expected one of "
            f"{sorted(JIELI_CHIPS)}"
        )
    probe = chip_probe_path(chip, sdk_source_root)
    if not probe.is_file():
        raise BuildError(
            f"build plan field sdk_source_root {sdk_source_root} does not carry the "
            f"{chip.name} probe: expected {probe}"
        )

    toolchain_root = _required_plan_path(plan, "toolchain_root")
    if not toolchain_root.is_dir():
        raise BuildError(
            f"build plan field toolchain_root does not name a directory: expected "
            f"an existing toolchain root, found {toolchain_root}"
        )
    tools = plan.get("tools") or {}
    for name in CONSUMED_PLAN_TOOLS:
        tool = tools.get(name)
        if not tool or not Path(str(tool)).is_file():
            raise BuildError(
                f"build plan field toolchain_root {toolchain_root} does not provide "
                f"its {name}: plan field tools.{name} is {tool!r}"
            )
    compiler = Path(str(tools["c_compiler"]))
    if not _is_within(compiler, toolchain_root):
        raise BuildError(
            f"build plan field toolchain_root {toolchain_root} does not contain its "
            f"c_compiler: plan field tools.c_compiler is {str(compiler)}"
        )


def _plan_uart(plan: Mapping[str, Any]) -> None:
    """The staging hooks program the log UART from the plan's resolved values."""
    uart = plan.get("uart")
    if not isinstance(uart, dict):
        raise BuildError(
            f"build plan field uart is missing: expected log_port and log_baudrate, "
            f"found {uart!r}"
        )
    for name in ("log_port", "log_baudrate"):
        if not isinstance(uart.get(name), int):
            raise BuildError(
                f"build plan field uart.{name} is not an integer: found "
                f"{uart.get(name)!r}"
            )


def _reject_ambiguous_sdk_root(plan: Mapping[str, Any], env: Mapping[str, str]) -> None:
    """Refuse a JIELI_SDK_ROOT the two build phases cannot read the same way.

    The variable means two different directories: CMake reads it as the SDK
    *source* root (it appends the chip profile's probe to it), while the SDK
    resolver reads it as the SDK *checkout* root (it appends the chip's
    sdk_source_relative to it afterwards). Where the sources live one level down
    -- wl83 keeps them in ``sdk/`` -- no single value satisfies both, so a value
    that is not exactly the source root the plan resolved means CMake compiled
    against a tree this build did not stage. Rather than build the wrong SDK,
    fail and say which value would have been consistent.
    """
    configured = (env.get(SDK_ROOT_ENV) or "").strip()
    if not configured:
        return
    source_root = plan.get("sdk_source_root")
    if _same_path(configured, source_root):
        return
    raise BuildError(
        f"{SDK_ROOT_ENV} is set to {configured!r} but this build's SDK source root "
        f"is {source_root!r}. The two build phases read the variable with different "
        f"meanings -- CMake as the SDK source root, the SDK resolver as the SDK "
        f"checkout root -- so a single value cannot serve both on a chip whose "
        f"sources live in a subdirectory. The plan is the authority for where the "
        f"SDK is: unset {SDK_ROOT_ENV} and re-run configure, or set it to "
        f"{source_root!r}."
    )


def load_build_plan(
    param_dir: Path,
    params: Mapping[str, str],
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Load the plan CMake resolved for this build and prove it still applies.

    The plan is the only source of the SDK and toolchain locations: nothing here
    falls back to resolving them again, so a plan that cannot be trusted is a
    build failure rather than a silent second opinion.
    """
    env = os.environ if environ is None else environ
    param_dir = Path(param_dir)
    build_dir = param_dir.parent
    plan_path = build_dir / PLAN_RELATIVE
    if not plan_path.is_file():
        configured = (env.get(SDK_ROOT_ENV) or "").strip()
        hint = (
            f" {SDK_ROOT_ENV} is set to {configured!r}; unset it, because the two "
            f"build phases read it with different meanings."
            if configured
            else ""
        )
        raise BuildError(
            f"build plan is missing: expected {plan_path}. The configure step "
            f"writes it, so re-run configure (remove {build_dir} and build "
            f"again).{hint}"
        )
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"build plan is not readable JSON: {plan_path}: {exc}") from exc
    if not isinstance(plan, dict):
        raise BuildError(f"build plan is not a JSON object: {plan_path}")

    _plan_versions(plan, plan_path)
    _plan_selection(plan, params)
    _plan_config_freshness(plan, build_dir)
    _plan_locations(plan)
    _plan_uart(plan)
    _reject_ambiguous_sdk_root(plan, env)
    return plan


def _run(command: list[str], cwd: Path, env: dict[str, str]) -> None:
    print(f"[JIELI] run: {shlex.join(command)}")
    result = subprocess.run(command, cwd=cwd, env=env, check=False)
    if result.returncode != 0:
        raise BuildError(f"command failed with exit code {result.returncode}: {command[0]}")


def build(param_dir: Path, params: dict[str, str]) -> Path:
    plan = load_build_plan(param_dir, params)
    # The plan validated the chip against the profile table, so the layout facts
    # the staging hooks need come from that same table, never from the plan.
    chip = JIELI_CHIPS[plan["chip"]]
    os.environ["JIELI_CHIP"] = chip.name
    sdk_root = Path(plan["sdk_checkout_root"])
    tool_dir = Path(plan["toolchain_root"])
    tuyaopen_root = Path(params.get("OPEN_ROOT", ""))
    if not tuyaopen_root.is_dir():
        raise BuildError("OPEN_ROOT is missing from build parameters")
    output_dir_text = params.get("BIN_OUTPUT_DIR", "").strip()
    if not output_dir_text:
        raise BuildError("BIN_OUTPUT_DIR is missing from build parameters")
    output_dir = Path(output_dir_text)
    staging_root = output_dir.parent / "jieli-staging"
    header_dir_text = params.get("OPEN_HEADER_DIR", "").split()
    header_dir = Path(header_dir_text[0]) if header_dir_text else None
    tuya_lib_dir = Path(params.get("OPEN_LIBS_DIR", ""))
    build_root = create_staging_tree(
        sdk_root,
        staging_root,
        tuyaopen_root,
        header_dir,
        tuya_lib_dir=tuya_lib_dir,
        uart_log_port=plan["uart"]["log_port"],
        uart_log_baudrate=plan["uart"]["log_baudrate"],
        platform_root=PLATFORM_ROOT,
        chip=chip,
        # The board's facts are ordinary Kconfig settings, so they are read
        # from the same evaluated config as everything else.
        facts=load_board_facts(params),
    )
    # The vendor Makefile builds serially at -j1, which leaves most cores idle
    # on the ~130 vendor objects every staging rebuild has to recompile. Default
    # to 8 jobs; JIELI_BUILD_JOBS still overrides it per build.
    jobs = max(1, int(os.environ.get("JIELI_BUILD_JOBS", "8")))
    env = os.environ.copy()
    vendor_source_root = sdk_root / chip.sdk_source_relative
    inherited_path = env.get("PATH", "").split(os.pathsep)
    if os.name == "nt":
        # GnuWin32 make picks sh.exe as its recipe shell when one is on PATH.
        # That shell mangles the Windows paths interpolated by the vendor
        # Makefile; remove those entries so make falls back to cmd.exe.
        inherited_path = [
            entry
            for entry in inherited_path
            if entry and not os.path.exists(os.path.join(entry, "sh.exe"))
        ]
    env["PATH"] = os.pathsep.join(
        (str(tool_dir), str(vendor_source_root / "tools/utils"), *inherited_path)
    )
    # objdump is the one tool path the plan records, so take it from there; the
    # plan does not record objsizedump, so it is still picked by alias inside
    # the toolchain root the plan named.
    env["OBJDUMP"] = str(Path(plan["tools"]["objdump"]))
    env["OBJSIZEDUMP"] = str(resolve_tool_path(tool_dir, "objsizedump"))

    command = build_make_command(build_root, tool_dir, jobs, chip)
    _run(command, build_root, env)

    tools_dir = build_root / chip.sdk_source_relative / chip.tools_relative
    elf = build_root / chip.sdk_source_relative / chip.elf_relative
    if not elf.is_file() or elf.stat().st_size == 0:
        raise BuildError(f"Jieli linker did not produce {elf}")

    run_postbuild(build_root, tool_dir, env, chip)
    package = find_qio_artifact(tools_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / tuya_qio_name(params)
    shutil.copy2(package, output)
    print(f"[JIELI] artifact: {output}")
    return output


def clean(param_dir: Path, params: dict[str, str]) -> None:
    """Remove only generated bridge outputs, without shelling into the SDK."""
    del params
    staging_root = param_dir.parent / "jieli-staging"
    if staging_root.is_dir():
        clean_staging_tree(staging_root)
        print(f"[JIELI] removed staging tree: {staging_root}")


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[2] not in ("build", "clean"):
        print(f"usage: {argv[0]} <build-param-dir> <build|clean>", file=sys.stderr)
        return 2
    try:
        param_dir = Path(argv[1])
        param_file = param_dir / "build_param.config"
        params = parse_build_params(param_file)
        if argv[2] == "clean":
            clean(param_dir, params)
        else:
            build(param_dir, params)
    except (BuildError, OSError, ValueError) as exc:
        print(f"[JIELI] build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
