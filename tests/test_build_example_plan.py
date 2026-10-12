"""The build phase consumes the plan CMake resolved during configure.

Every check here has a positive and a negative side: a plan that describes this
build must be accepted, and a plan that does not must fail loudly, naming the
field, what was expected and what was found.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_example
from build_example import build, clean, load_build_plan, parse_build_params
from tools.jieli_build import build_plan as build_plan_module
from tools.jieli_build.build_plan import DEFAULT_PLAN_RELATIVE, write_plan
from tools.jieli_build.errors import BuildError

_REQUIRED_TOOLS = (
    "clang.exe",
    "lto-wrapper.exe",
    "lto-ar.exe",
    "objdump.exe",
    "objsizedump.exe",
)

_CHECKOUT_PROBES = {
    "wl82": Path("apps/demo/demo_hello/board/wl82/Makefile"),
    "wl83": Path("sdk/apps/demo/demo_hello/board/wl83/Makefile"),
}


class _Fixture:
    """A build tree whose plan was resolved by the real resolver."""

    def __init__(self, chip: str = "wl83", board: str = "AC792N_Develop_Board"):
        self.chip = chip
        self.board = board
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)

        self.open_root = root / "TuyaOpen"
        self.build_dir = self.open_root / ".build"
        self.param_dir = self.build_dir / "build"
        self.bin_output_dir = self.build_dir / "bin"
        self.config = self.build_dir / "cache" / "using.config"
        self.sdk_checkout_root = root / ("AC792_SDK" if chip == "wl83" else "AC79_AIoT_SDK")
        self.sdk_source_root = (
            self.sdk_checkout_root / "sdk" if chip == "wl83" else self.sdk_checkout_root
        )
        self.tool_dir = root / "toolchain" / "pi32v2" / "bin"
        self.plan_path = self.build_dir / DEFAULT_PLAN_RELATIVE

        for directory in ("libtls/mbedtls-3.1.0", "liblwip/lwip-2.1.2", "liblvgl/v8"):
            (self.open_root / "src" / directory).mkdir(parents=True, exist_ok=True)

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
        self.param_dir.mkdir(parents=True, exist_ok=True)
        self.write_params()

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

    def write_params(self, **overrides) -> None:
        values = {
            "OPEN_ROOT": str(self.open_root),
            "BIN_OUTPUT_DIR": str(self.bin_output_dir),
            "OPEN_HEADER_DIR": str(self.open_root / "include"),
            "OPEN_LIBS_DIR": str(self.build_dir / "lib"),
            "CONFIG_CHIP_CHOICE": self.chip,
            "CONFIG_BOARD_CHOICE": self.board,
            "CONFIG_BOARD_CHOICE_" + self.board.upper(): "y",
            "CONFIG_JIELI_UART_LOG_PORT": "1" if self.chip == "wl82" else "0",
            "CONFIG_JIELI_UART_LOG_BAUDRATE": "115200",
        }
        values.update(overrides)
        lines = [f'{key}="{value}"' for key, value in values.items()]
        (self.param_dir / "build_param.config").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    @property
    def resolver_environ(self) -> dict[str, str]:
        """The environment the configure step resolves the plan in."""
        return {
            "JIELI_SDK_ROOT": str(self.sdk_checkout_root),
            "JIELI_TOOL_DIR": str(self.tool_dir),
        }

    def resolve_plan(self, **overrides) -> dict:
        """Write the plan exactly as the configure step would."""
        plan = build_plan_module.build_plan(
            config_path=self.config,
            open_root=self.open_root,
            build_dir=self.build_dir,
            bin_output_dir=self.bin_output_dir,
            environ=self.resolver_environ,
            **overrides,
        )
        write_plan(plan, self.plan_path)
        return plan

    def rewrite_plan(self, plan: dict) -> None:
        write_plan(plan, self.plan_path)

    @property
    def params(self) -> dict[str, str]:
        return parse_build_params(self.param_dir / "build_param.config")

    def load(self, environ=None, **kwargs) -> dict:
        if environ is None:
            environ = {}
        return load_build_plan(self.param_dir, self.params, environ, **kwargs)

    def close(self) -> None:
        self._temp.cleanup()


class PlanConsumptionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)

    def test_accepts_the_plan_configure_wrote(self):
        plan = self.fixture.resolve_plan()

        loaded = self.fixture.load()

        self.assertEqual(loaded, plan)
        self.assertEqual(loaded["chip"], "wl83")
        self.assertEqual(loaded["board"], "AC792N_Develop_Board")

    def test_the_build_no_longer_carries_the_independent_resolvers(self):
        # The consumption is structural, not just behavioural: the module cannot
        # fall back to resolving the chip, the SDK or the toolchain on its own.
        for name in ("resolve_chip", "resolve_sdk_root", "resolve_tool_dir"):
            self.assertFalse(hasattr(build_example, name), name)

    def _build(self, fixture):
        """Run build() with the SDK build itself stubbed out."""
        staging_root = fixture.bin_output_dir.parent / "jieli-staging"
        build_root = staging_root / "build"
        elf = build_root / "sdk" / "cpu/wl83/tools/sdk.elf"
        elf.parent.mkdir(parents=True, exist_ok=True)
        elf.write_bytes(b"elf")

        package = fixture.build_dir / "package.bin"
        package.write_bytes(b"package")

        with mock.patch.object(build_example, "create_staging_tree", return_value=build_root) as stage:
            with mock.patch.object(build_example, "build_make_command", return_value=["make"]):
                with mock.patch.object(build_example, "_run") as run:
                    with mock.patch.object(build_example, "run_postbuild"):
                        with mock.patch.object(
                            build_example, "find_qio_artifact", return_value=package
                        ):
                            with mock.patch.object(build_example.shutil, "copy2"):
                                output = build(fixture.param_dir, fixture.params)
        return stage, run, output

    def test_build_stages_the_sdk_board_and_uart_the_plan_names(self):
        plan = self.fixture.resolve_plan()

        stage, run, output = self._build(self.fixture)

        self.assertEqual(stage.call_args.args[0], Path(plan["sdk_checkout_root"]))
        self.assertEqual(stage.call_args.kwargs["chip"].name, plan["chip"])
        self.assertEqual(stage.call_args.kwargs["board_name"], plan["board"])
        self.assertEqual(stage.call_args.kwargs["uart_log_port"], plan["uart"]["log_port"])
        self.assertEqual(
            stage.call_args.kwargs["uart_log_baudrate"], plan["uart"]["log_baudrate"]
        )
        # The toolchain the make command and PATH see is the plan's, not a
        # freshly discovered one.
        self.assertEqual(
            Path(run.call_args.args[2]["PATH"].split(os.pathsep)[0]),
            Path(plan["toolchain_root"]),
        )
        self.assertEqual(
            Path(run.call_args.args[2]["OBJDUMP"]), Path(plan["tools"]["objdump"])
        )
        self.assertEqual(output.name, "jieli_uart_hello_QIO_1.0.0.bin")

    def test_build_fails_on_a_corrupted_toolchain_root_instead_of_resolving(self):
        plan = self.fixture.resolve_plan()
        plan["toolchain_root"] = build_plan_module.canonical_path(
            self.fixture.open_root / "absent-toolchain"
        )
        self.fixture.rewrite_plan(plan)

        with self.assertRaises(BuildError) as caught:
            self._build(self.fixture)

        message = str(caught.exception)
        self.assertIn("toolchain_root", message)
        self.assertIn("absent-toolchain", message)

    def test_clean_does_not_need_a_plan(self):
        # The clean branch removes generated outputs only, so it must keep
        # working in a tree where configure never ran.
        staging_root = self.fixture.bin_output_dir.parent / "jieli-staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        (staging_root / "marker").write_text("x", encoding="utf-8")

        with mock.patch.object(build_example, "clean_staging_tree") as cleaner:
            clean(self.fixture.param_dir, self.fixture.params)

        cleaner.assert_called_once_with(staging_root)
        self.assertFalse(self.fixture.plan_path.exists())


class MissingPlanTest(unittest.TestCase):
    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)

    def test_accepts_a_plan_that_is_present(self):
        self.fixture.resolve_plan()
        self.assertTrue(self.fixture.plan_path.is_file())

        self.fixture.load()

    def test_rejects_a_missing_plan_naming_the_path(self):
        self.assertFalse(self.fixture.plan_path.exists())

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("build plan is missing", message)
        self.assertIn(str(self.fixture.plan_path), message)

    def test_a_missing_plan_message_mentions_the_sdk_root_variable(self):
        with self.assertRaises(BuildError) as caught:
            self.fixture.load(environ={"JIELI_SDK_ROOT": "C:/elsewhere"})

        self.assertIn("JIELI_SDK_ROOT", str(caught.exception))

    def test_rejects_a_plan_that_is_not_json(self):
        self.fixture.plan_path.parent.mkdir(parents=True, exist_ok=True)
        self.fixture.plan_path.write_text("{not json", encoding="utf-8")

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("not readable JSON", message)
        self.assertIn(str(self.fixture.plan_path), message)

    def test_rejects_a_plan_that_is_not_an_object(self):
        self.fixture.plan_path.parent.mkdir(parents=True, exist_ok=True)
        self.fixture.plan_path.write_text("[1, 2]", encoding="utf-8")

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        self.assertIn("not a JSON object", str(caught.exception))


class PlanVersionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)
        self.plan = self.fixture.resolve_plan()

    def test_accepts_the_schema_and_protocol_this_build_speaks(self):
        self.assertEqual(self.plan["schema_version"], 1)
        self.assertEqual(self.plan["protocol_version"], 1)

        self.fixture.load()

    def test_rejects_an_unsupported_schema_version(self):
        self.fixture.rewrite_plan({**self.plan, "schema_version": 2})

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("schema_version", message)
        self.assertIn("2", message)
        self.assertIn("[1]", message)

    def test_rejects_an_unsupported_protocol_version(self):
        self.fixture.rewrite_plan({**self.plan, "protocol_version": 7})

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("protocol_version", message)
        self.assertIn("7", message)

    def test_rejects_a_plan_from_another_platform(self):
        self.fixture.rewrite_plan({**self.plan, "platform": "T2"})

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("platform", message)
        self.assertIn("JIELI", message)
        self.assertIn("T2", message)


class PlanSelectionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)
        self.plan = self.fixture.resolve_plan()

    def test_accepts_parameters_that_match_the_plan(self):
        self.fixture.load()

    def test_rejects_a_chip_the_parameters_do_not_name(self):
        self.fixture.write_params(CONFIG_CHIP_CHOICE="wl82")

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("CONFIG_CHIP_CHOICE", message)
        self.assertIn("'wl82'", message)
        self.assertIn("'wl83'", message)

    def test_rejects_a_board_the_parameters_do_not_name(self):
        self.fixture.write_params(CONFIG_BOARD_CHOICE="AC79_DevKitBoard")

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("CONFIG_BOARD_CHOICE", message)
        self.assertIn("AC79_DevKitBoard", message)
        self.assertIn("AC792N_Develop_Board", message)


class PlanFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)
        self.plan = self.fixture.resolve_plan()

    def test_accepts_a_config_that_has_not_changed(self):
        self.fixture.load()

    def test_accepts_a_config_that_only_changed_line_endings(self):
        text = self.fixture.config.read_text(encoding="utf-8")
        self.fixture.config.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))

        self.fixture.load()

    def test_rejects_a_config_edited_after_the_plan_was_resolved(self):
        self.fixture.write_config(CONFIG_JIELI_UART_LOG_BAUDRATE="921600")

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("build plan is stale", message)
        self.assertIn(self.plan["config_digest"], message)
        self.assertIn("sha256:", message)

    def test_rejects_a_missing_evaluated_config(self):
        self.fixture.config.unlink()

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        self.assertIn("evaluated config is missing", str(caught.exception))


class PlanLocationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)
        self.plan = self.fixture.resolve_plan()

    def test_accepts_locations_that_exist(self):
        self.fixture.load()

    def test_rejects_a_sdk_source_root_that_is_gone(self):
        import shutil as _shutil

        _shutil.rmtree(self.fixture.sdk_source_root)

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("sdk_source_root", message)
        self.assertIn(str(self.fixture.sdk_source_root), message)

    def test_rejects_a_sdk_source_root_without_the_chip_probe(self):
        probe = self.fixture.sdk_source_root / "apps/demo/demo_hello/board/wl83/Makefile"
        probe.unlink()

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("sdk_source_root", message)
        self.assertIn("probe", message)

    def test_rejects_a_toolchain_root_that_is_gone(self):
        self.fixture.rewrite_plan(
            {
                **self.plan,
                "toolchain_root": build_plan_module.canonical_path(
                    self.fixture.open_root / "absent-toolchain"
                ),
            }
        )

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("toolchain_root", message)
        self.assertIn("absent-toolchain", message)

    def test_rejects_a_toolchain_without_its_compiler(self):
        compiler = Path(self.plan["tools"]["c_compiler"])
        compiler.unlink()

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("toolchain_root", message)
        self.assertIn("c_compiler", message)

    def test_rejects_a_compiler_outside_the_toolchain_root(self):
        elsewhere = self.fixture.open_root / "elsewhere" / "clang.exe"
        elsewhere.parent.mkdir(parents=True, exist_ok=True)
        elsewhere.write_bytes(b"compiler")
        self.fixture.rewrite_plan(
            {
                **self.plan,
                "tools": {
                    **self.plan["tools"],
                    "c_compiler": build_plan_module.canonical_path(elsewhere),
                },
            }
        )

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("toolchain_root", message)
        self.assertIn("c_compiler", message)

    def test_rejects_a_plan_without_an_sdk_source_root(self):
        plan = dict(self.plan)
        del plan["sdk_source_root"]
        self.fixture.rewrite_plan(plan)

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        self.assertIn("sdk_source_root", str(caught.exception))

    def test_rejects_a_plan_without_a_toolchain_root(self):
        plan = dict(self.plan)
        del plan["toolchain_root"]
        self.fixture.rewrite_plan(plan)

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        self.assertIn("toolchain_root", str(caught.exception))

    def test_rejects_a_plan_without_the_log_uart(self):
        plan = dict(self.plan)
        del plan["uart"]
        self.fixture.rewrite_plan(plan)

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("uart", message)
        self.assertIn("log_port", message)

    def test_rejects_a_log_uart_that_is_not_an_integer(self):
        self.fixture.rewrite_plan(
            {**self.plan, "uart": {"log_port": "0", "log_baudrate": 115200}}
        )

        with self.assertRaises(BuildError) as caught:
            self.fixture.load()

        message = str(caught.exception)
        self.assertIn("uart.log_port", message)


class SdkRootEnvironmentTest(unittest.TestCase):
    """JIELI_SDK_ROOT means two different directories across the two phases."""

    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)
        self.plan = self.fixture.resolve_plan()

    def test_accepts_an_unset_variable(self):
        self.fixture.load(environ={})

    def test_accepts_the_source_root_the_plan_resolved(self):
        self.fixture.load(environ={"JIELI_SDK_ROOT": self.plan["sdk_source_root"]})

    def test_rejects_a_value_the_two_phases_cannot_share(self):
        # The checkout root: CMake reads it as the source root and cannot find
        # the probe, the Python half reads it as the checkout root.
        with self.assertRaises(BuildError) as caught:
            self.fixture.load(
                environ={"JIELI_SDK_ROOT": self.fixture.sdk_checkout_root.as_posix()}
            )

        message = str(caught.exception)
        self.assertIn("JIELI_SDK_ROOT", message)
        self.assertIn(self.fixture.sdk_checkout_root.as_posix(), message)
        self.assertIn(self.plan["sdk_source_root"], message)

    def test_rejects_an_unrelated_sdk_root(self):
        with self.assertRaises(BuildError) as caught:
            self.fixture.load(environ={"JIELI_SDK_ROOT": "D:/other/AC792_SDK/sdk"})

        message = str(caught.exception)
        self.assertIn("JIELI_SDK_ROOT", message)
        self.assertIn("D:/other/AC792_SDK/sdk", message)

    def test_accepts_the_variable_when_both_phases_agree(self):
        # On wl82 the sources sit at the checkout root, so the two meanings
        # coincide and the variable is harmless.
        fixture = _Fixture(chip="wl82", board="AC79_DevKitBoard")
        self.addCleanup(fixture.close)
        plan = fixture.resolve_plan()
        self.assertEqual(plan["sdk_checkout_root"], plan["sdk_source_root"])

        fixture.load(environ={"JIELI_SDK_ROOT": plan["sdk_checkout_root"]})

    def test_a_relative_value_is_compared_as_a_path_not_a_string(self):
        self.fixture.load(environ={"JIELI_SDK_ROOT": self.plan["sdk_source_root"] + "/"})


class BuildParameterParsingTest(unittest.TestCase):
    def test_parses_the_generated_build_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "build_param.config"
            path.write_text(
                "# a comment\n"
                'OPEN_ROOT="/tmp/open"\n'
                "CONFIG_BOARD_CHOICE_AC792N_DEVELOP_BOARD=y\n"
                "\n",
                encoding="utf-8",
            )

            params = parse_build_params(path)

        self.assertEqual(params["OPEN_ROOT"], "/tmp/open")
        self.assertEqual(params["CONFIG_BOARD_CHOICE_AC792N_DEVELOP_BOARD"], "y")
        self.assertNotIn("# a comment", params)


class ScriptEntryPointTest(unittest.TestCase):
    """The hook the CMake build invokes still reports failures on stderr."""

    def setUp(self):
        self.fixture = _Fixture()
        self.addCleanup(self.fixture.close)

    def test_a_missing_plan_fails_the_build(self):
        result = build_example.main(
            ["build_example.py", str(self.fixture.param_dir), "build"]
        )

        self.assertEqual(result, 1)

    def test_clean_succeeds_without_a_plan(self):
        result = build_example.main(
            ["build_example.py", str(self.fixture.param_dir), "clean"]
        )

        self.assertEqual(result, 0)

    def test_an_unknown_mode_is_a_usage_error(self):
        result = build_example.main(["build_example.py", str(self.fixture.param_dir), "flash"])

        self.assertEqual(result, 2)


if __name__ == "__main__":
    unittest.main()
