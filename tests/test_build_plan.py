import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.jieli_build import build_plan as build_plan_module
from tools.jieli_build.build_plan import (
    DEFAULT_PLAN_RELATIVE,
    canonical_path,
    main,
    validate_plan,
    write_plan,
)
from tools.jieli_build.errors import BuildError


_REQUIRED_TOOLS = (
    "clang.exe",
    "lto-wrapper.exe",
    "lto-ar.exe",
    "objdump.exe",
    "objsizedump.exe",
)

# The probe each chip profile declares, relative to its SDK checkout.
_CHECKOUT_PROBES = {
    "wl82": Path("apps/demo/demo_hello/board/wl82/Makefile"),
    "wl83": Path("sdk/apps/demo/demo_hello/board/wl83/Makefile"),
}

_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "jieli_build" / "build_plan.py"


class _Fixture:
    """A minimal but complete host tree the resolver can be pointed at."""

    def __init__(self, chip: str = "wl83", board: str = "AC792N_Develop_Board"):
        self.chip = chip
        self.board = board
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)

        self.open_root = root / "TuyaOpen"
        self.build_dir = self.open_root / ".build"
        self.bin_output_dir = self.build_dir / "bin"
        self.config = self.build_dir / "cache" / "using.config"
        self.sdk_checkout_root = root / ("AC792_SDK" if chip == "wl83" else "AC79_AIoT_SDK")
        self.sdk_source_root = (
            self.sdk_checkout_root / "sdk" if chip == "wl83" else self.sdk_checkout_root
        )
        self.tool_dir = root / "toolchain" / "pi32v2" / "bin"
        self.out = self.build_dir / DEFAULT_PLAN_RELATIVE

        # TuyaOpen components the provider table reads.
        for directory in ("libtls/mbedtls-3.1.0", "liblwip/lwip-2.1.2", "liblvgl/v8"):
            (self.open_root / "src" / directory).mkdir(parents=True, exist_ok=True)

        # A checkout that carries the chip profile's probe, plus the vendor
        # lwIP the Jieli build falls back to when its own is not enabled.
        probe = self.sdk_checkout_root / _CHECKOUT_PROBES[chip]
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.touch()
        (self.sdk_source_root / "include_lib/net/lwip_2_2_0").mkdir(
            parents=True, exist_ok=True
        )

        self.tool_dir.mkdir(parents=True, exist_ok=True)
        for name in _REQUIRED_TOOLS:
            (self.tool_dir / name).touch()

        self.write_config()

    def write_config(self, **overrides) -> None:
        values = {
            "CONFIG_PLATFORM_CHOICE": "JIELI",
            "CONFIG_CHIP_CHOICE": self.chip,
            "CONFIG_BOARD_CHOICE": self.board,
            "CONFIG_BOARD_CHOICE_" + self.board.upper(): "y",
            "CONFIG_JIELI_UART_LOG_PORT": "1" if self.chip == "wl82" else "0",
            "CONFIG_JIELI_UART_LOG_BAUDRATE": "115200",
        }
        values.update(overrides)
        lines = [f'{key}="{value}"' for key, value in values.items()]
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @property
    def environ(self) -> dict[str, str]:
        return {
            "JIELI_SDK_ROOT": str(self.sdk_checkout_root),
            "JIELI_TOOL_DIR": str(self.tool_dir),
        }

    def build_plan(self, **kwargs):
        arguments = {
            "config_path": self.config,
            "open_root": self.open_root,
            "build_dir": self.build_dir,
            "bin_output_dir": self.bin_output_dir,
            "environ": self.environ,
        }
        arguments.update(kwargs)
        return build_plan_module.build_plan(**arguments)

    def close(self) -> None:
        self._temp.cleanup()


class BuildPlanResolutionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)

    def test_names_the_checkout_and_the_source_root_separately(self):
        plan = self.fixture.build_plan()

        self.assertEqual(plan["chip"], "wl83")
        self.assertEqual(plan["sdk_checkout_root"], canonical_path(self.fixture.sdk_checkout_root))
        self.assertEqual(plan["sdk_source_root"], canonical_path(self.fixture.sdk_source_root))
        # The two names are what the CMake and Python halves used to disagree
        # about: the same physical tree, one level apart.
        self.assertNotEqual(plan["sdk_checkout_root"], plan["sdk_source_root"])

    def test_wl82_checkout_and_source_root_are_the_same_tree(self):
        fixture = _Fixture(chip="wl82", board="AC79_DevKitBoard")
        self.addCleanup(fixture.close)

        plan = fixture.build_plan()

        self.assertEqual(plan["chip"], "wl82")
        self.assertEqual(plan["sdk_checkout_root"], plan["sdk_source_root"])

    def test_records_the_selected_board_and_the_flag_that_confirmed_it(self):
        plan = self.fixture.build_plan()

        self.assertEqual(plan["board"], "AC792N_Develop_Board")
        self.assertEqual(
            plan["board_choice_flag"], "CONFIG_BOARD_CHOICE_AC792N_DEVELOP_BOARD"
        )

    def test_records_the_schema_and_protocol_versions(self):
        plan = self.fixture.build_plan()

        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["protocol_version"], 1)
        self.assertEqual(plan["platform"], "JIELI")

    def test_tools_point_at_the_wrappers_the_toolchain_file_selects(self):
        plan = self.fixture.build_plan()

        self.assertEqual(
            plan["tools"]["c_compiler"],
            canonical_path(self.fixture.tool_dir / "clang.exe"),
        )
        self.assertEqual(
            plan["tools"]["objdump"],
            canonical_path(self.fixture.tool_dir / "objdump.exe"),
        )
        if os.name == "nt":
            self.assertTrue(plan["tools"]["archiver"].endswith("/jieli_llvm_ar.cmd"))
            self.assertTrue(plan["tools"]["ranlib"].endswith("/jieli_ranlib.cmd"))
        else:
            self.assertTrue(plan["tools"]["ranlib"].endswith("/jieli_ranlib.sh"))
        for tool in plan["tools"].values():
            self.assertTrue(Path(tool).is_file(), tool)

    def test_paths_are_absolute_and_use_forward_slashes(self):
        plan = self.fixture.build_plan()

        for value in (
            plan["sdk_checkout_root"],
            plan["sdk_source_root"],
            plan["toolchain_root"],
            plan["paths"]["open_root"],
            plan["paths"]["build_dir"],
            plan["paths"]["bin_output_dir"],
            plan["paths"]["config"],
        ):
            self.assertNotIn("\\", value)
            self.assertTrue(Path(value).is_absolute(), value)

    def test_toolchain_root_matches_the_resolved_tool_dir(self):
        plan = self.fixture.build_plan()

        self.assertEqual(plan["toolchain_root"], canonical_path(self.fixture.tool_dir))

    def test_uart_defaults_follow_the_chip_profile(self):
        plan = self.fixture.build_plan()
        self.assertEqual(plan["uart"], {"log_port": 0, "log_baudrate": 115200})

        fixture = _Fixture(chip="wl82", board="AC79_DevKitBoard")
        self.addCleanup(fixture.close)
        fixture.write_config(
            CONFIG_JIELI_UART_LOG_PORT="1", CONFIG_JIELI_UART_LOG_BAUDRATE="921600"
        )
        wl82_plan = fixture.build_plan()
        self.assertEqual(wl82_plan["uart"], {"log_port": 1, "log_baudrate": 921600})

    def test_providers_follow_the_tree_and_the_evaluated_config(self):
        plan = self.fixture.build_plan()

        self.assertEqual(plan["providers"]["tls"], "tuyaopen_mbedtls_3_1")
        self.assertEqual(plan["providers"]["lwip"], "jieli_vendor_lwip_2_2")
        # LVGL is opt-in and this config does not enable it, so the build
        # carries no LVGL provider at all.
        self.assertIsNone(plan["providers"]["lvgl"])

    def test_providers_report_nothing_when_the_tree_carries_nothing(self):
        self.fixture.write_config(CONFIG_ENABLE_LIBLWIP="y", CONFIG_ENABLE_LIBLVGL="y")
        plan = self.fixture.build_plan()

        self.assertEqual(plan["providers"]["lwip"], "tuyaopen_lwip_2_1")
        self.assertEqual(plan["providers"]["lvgl"], "tuyaopen_lvgl")

    def test_config_digest_tracks_the_config_and_not_the_plan(self):
        first = self.fixture.build_plan()
        second = self.fixture.build_plan(bin_output_dir=self.fixture.open_root / "other")

        self.assertEqual(first["config_digest"], second["config_digest"])
        self.assertNotEqual(
            first["paths"]["bin_output_dir"], second["paths"]["bin_output_dir"]
        )

        self.fixture.write_config(CONFIG_JIELI_UART_LOG_BAUDRATE="921600")
        third = self.fixture.build_plan()
        self.assertNotEqual(first["config_digest"], third["config_digest"])

    def test_config_digest_ignores_line_endings(self):
        first = self.fixture.build_plan()
        text = self.fixture.config.read_text(encoding="utf-8")
        self.fixture.config.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))

        self.assertEqual(first["config_digest"], self.fixture.build_plan()["config_digest"])

    def test_missing_config_is_rejected(self):
        with self.assertRaises(FileNotFoundError):
            self.fixture.build_plan(config_path=self.fixture.build_dir / "absent.config")

    def test_board_without_its_choice_flag_is_rejected(self):
        self.fixture.write_config(CONFIG_BOARD_CHOICE_AC792N_DEVELOP_BOARD="n")

        with self.assertRaises(BuildError) as caught:
            self.fixture.build_plan()

        self.assertIn("CONFIG_BOARD_CHOICE_AC792N_DEVELOP_BOARD", str(caught.exception))

    def test_missing_chip_choice_is_rejected(self):
        self.fixture.write_config()
        text = self.fixture.config.read_text(encoding="utf-8")
        self.fixture.config.write_text(
            text.replace('CONFIG_CHIP_CHOICE="wl83"\n', ""), encoding="utf-8"
        )

        with self.assertRaises(BuildError) as caught:
            self.fixture.build_plan()

        self.assertIn("CONFIG_CHIP_CHOICE", str(caught.exception))


class ValidatePlanTest(unittest.TestCase):
    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)
        self.plan = self.fixture.build_plan()

    def test_accepts_a_resolved_plan(self):
        validate_plan(self.plan)

    def test_rejects_an_unknown_schema_version(self):
        plan = copy.deepcopy(self.plan)
        plan["schema_version"] = 2

        with self.assertRaises(BuildError) as caught:
            validate_plan(plan)

        self.assertIn("schema_version", str(caught.exception))

    def test_rejects_an_unknown_protocol_version(self):
        plan = copy.deepcopy(self.plan)
        plan["protocol_version"] = 99

        with self.assertRaises(BuildError) as caught:
            validate_plan(plan)

        self.assertIn("protocol_version", str(caught.exception))

    def test_rejects_a_chip_outside_the_profile_table(self):
        plan = copy.deepcopy(self.plan)
        plan["chip"] = "wl84"

        with self.assertRaises(BuildError) as caught:
            validate_plan(plan)

        self.assertIn("wl84", str(caught.exception))
        self.assertIn("wl83", str(caught.exception))

    def test_rejects_a_missing_sdk_probe(self):
        probe = self.fixture.sdk_source_root / "apps/demo/demo_hello/board/wl83/Makefile"
        self.assertTrue(probe.is_file())
        probe.unlink()

        with self.assertRaises(BuildError) as caught:
            validate_plan(self.plan)

        self.assertIn("does not match chip 'wl83'", str(caught.exception))

    def test_rejects_a_missing_toolchain_executable(self):
        Path(self.plan["tools"]["objdump"]).unlink()

        with self.assertRaises(BuildError) as caught:
            validate_plan(self.plan)

        self.assertIn("objdump", str(caught.exception))

    def test_rejects_a_toolchain_executable_that_is_not_a_file(self):
        plan = copy.deepcopy(self.plan)
        plan["tools"]["archiver"] = canonical_path(self.fixture.open_root / "absent.cmd")

        with self.assertRaises(BuildError) as caught:
            validate_plan(plan)

        self.assertIn("archiver", str(caught.exception))

    def test_rejects_a_board_that_disagrees_with_its_choice_flag(self):
        plan = copy.deepcopy(self.plan)
        plan["board"] = "AC79_DevKitBoard"

        with self.assertRaises(BuildError) as caught:
            validate_plan(plan)

        message = str(caught.exception)
        self.assertIn("AC79_DevKitBoard", message)
        self.assertIn("CONFIG_BOARD_CHOICE_AC79_DEVKITBOARD", message)

    def test_rejects_a_source_root_outside_the_checkout(self):
        # A tree that carries the probe but is not the one the plan named as
        # its checkout: exactly the confusion between the two roots.
        elsewhere = self.fixture.open_root / "elsewhere"
        probe = elsewhere / "apps/demo/demo_hello/board/wl83/Makefile"
        probe.parent.mkdir(parents=True)
        probe.touch()
        plan = copy.deepcopy(self.plan)
        plan["sdk_source_root"] = canonical_path(elsewhere)

        with self.assertRaises(BuildError) as caught:
            validate_plan(plan)

        self.assertIn("sdk_checkout_root", str(caught.exception))

    def test_rejects_a_plan_missing_a_required_field(self):
        plan = copy.deepcopy(self.plan)
        del plan["board"]

        with self.assertRaises(BuildError) as caught:
            validate_plan(plan)

        self.assertIn("board", str(caught.exception))


class WritePlanTest(unittest.TestCase):
    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)
        self.plan = self.fixture.build_plan()
        self.target = self.fixture.open_root / "plan" / "build_plan.json"

    def test_writes_the_plan_as_json(self):
        write_plan(self.plan, self.target)

        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), self.plan)

    def test_replaces_a_sibling_temporary_file_instead_of_writing_in_place(self):
        observed = {}
        real_replace = os.replace

        def spy(source, destination):
            observed["source"] = Path(source)
            observed["destination"] = Path(destination)
            real_replace(source, destination)

        with mock.patch("os.replace", spy):
            write_plan(self.plan, self.target)

        self.assertEqual(observed["destination"], self.target)
        self.assertEqual(observed["source"].parent, self.target.parent)
        self.assertNotEqual(observed["source"], self.target)
        self.assertTrue(observed["source"].name.startswith("build_plan.json."))
        self.assertTrue(self.target.is_file())

    def test_leaves_no_temporary_file_behind(self):
        write_plan(self.plan, self.target)

        self.assertEqual([path.name for path in self.target.parent.iterdir()], [self.target.name])

    def test_a_failed_write_leaves_no_partial_plan(self):
        with mock.patch.object(build_plan_module.json, "dump", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                write_plan(self.plan, self.target)

        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.iterdir()), [])

    def test_overwrites_an_existing_plan(self):
        write_plan(self.plan, self.target)
        write_plan({**self.plan, "chip": "wl82"}, self.target)

        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8"))["chip"], "wl82")


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)

    def _run(self, arguments):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, self.fixture.environ):
            with contextlib.redirect_stderr(stderr):
                code = main(arguments)
        return code, stderr.getvalue()

    def _arguments(self, **overrides):
        arguments = {
            "--config": str(self.fixture.config),
            "--open-root": str(self.fixture.open_root),
            "--build-dir": str(self.fixture.build_dir),
            "--bin-output-dir": str(self.fixture.bin_output_dir),
        }
        arguments.update(overrides)
        return [item for pair in arguments.items() for item in pair]

    def test_writes_the_plan_to_the_default_location_and_exits_zero(self):
        code, stderr = self._run(self._arguments())

        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        plan = json.loads(self.fixture.out.read_text(encoding="utf-8"))
        self.assertEqual(plan["chip"], "wl83")
        self.assertEqual(plan["board"], "AC792N_Develop_Board")

    def test_honours_an_explicit_output_path(self):
        target = self.fixture.open_root / "custom" / "plan.json"

        code, stderr = self._run(self._arguments(**{"--out": str(target)}))

        self.assertEqual(code, 0, stderr)
        self.assertTrue(target.is_file())
        self.assertFalse(self.fixture.out.exists())

    def test_a_missing_config_warns_and_exits_zero(self):
        code, stderr = self._run(self._arguments(**{"--config": str(self.fixture.open_root / "absent")}))

        self.assertEqual(code, 0)
        self.assertIn("build plan resolver skipped", stderr)
        self.assertFalse(self.fixture.out.exists())

    def test_a_broken_toolchain_warns_and_exits_zero(self):
        empty = self.fixture.open_root / "empty-toolchain"
        empty.mkdir(parents=True, exist_ok=True)
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"JIELI_TOOL_DIR": str(empty)}):
            with contextlib.redirect_stderr(stderr):
                code = main(self._arguments())

        self.assertEqual(code, 0)
        self.assertIn("build plan resolver skipped", stderr.getvalue())
        self.assertFalse(self.fixture.out.exists())

    def test_invalid_arguments_warn_and_exit_zero(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(["--config", str(self.fixture.config)])

        self.assertEqual(code, 0)
        self.assertIn("build plan resolver skipped", stderr.getvalue())

    def test_a_config_without_a_board_warns_and_exits_zero(self):
        self.fixture.write_config(CONFIG_BOARD_CHOICE_AC792N_DEVELOP_BOARD="n")

        code, stderr = self._run(self._arguments())

        self.assertEqual(code, 0)
        self.assertIn("build plan resolver skipped", stderr)
        self.assertFalse(self.fixture.out.exists())


class ScriptEntryPointTest(unittest.TestCase):
    """toolchain_file.cmake runs the resolver as a script, not as a module."""

    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)

    def test_resolves_when_run_from_an_unrelated_directory(self):
        env = os.environ.copy()
        env.update(self.fixture.environ)
        result = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "--config",
                str(self.fixture.config),
                "--open-root",
                str(self.fixture.open_root),
                "--build-dir",
                str(self.fixture.build_dir),
                "--bin-output-dir",
                str(self.fixture.bin_output_dir),
            ],
            cwd=str(self.fixture.open_root),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        plan = json.loads(self.fixture.out.read_text(encoding="utf-8"))
        self.assertEqual(plan["schema_version"], 1)

    def test_a_broken_config_still_exits_zero(self):
        result = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "--config",
                str(self.fixture.open_root / "absent.config"),
                "--open-root",
                str(self.fixture.open_root),
                "--build-dir",
                str(self.fixture.build_dir),
                "--bin-output-dir",
                str(self.fixture.bin_output_dir),
            ],
            cwd=str(self.fixture.open_root),
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("build plan resolver skipped", result.stderr)


if __name__ == "__main__":
    unittest.main()
