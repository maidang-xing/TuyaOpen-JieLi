"""The staging tree survives a build whose inputs did not move.

Deleting the staging tree costs the vendor make every one of its objects, so
the tree is reused whenever the inputs it was built from still match. These
tests pin both halves of that: reuse when nothing moved, and a full restage for
every kind of input that can move.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.jieli_build.audio_profile import AUDIO_PROFILES
from tools.jieli_build.board_facts import (
    BoardFacts, Display, LogUart, Memory, ServiceUart,
)
from tools.jieli_build.chip_profiles import JIELI_CHIPS, PLATFORM_ROOT
from tools.jieli_build.sdk_overlay import (
    STAGING_STATE_NAME,
    create_staging_tree,
    staging_input_components,
    staging_input_digest,
    staging_tree_is_current,
)


def synthetic_facts(board: str, chip_name: str) -> dict:
    """Board facts for the synthetic checkout, shaped like the real catalog's."""
    if chip_name == "wl82":
        return {
            "schema_version": 1,
            "board": board,
            "usb_device": "TCFG_FUSB_DEVICE",
            "memory": {"flash_mib": 8, "sdram_mib": 8, "external_ddr": False},
            "log_uart": {"port": 1, "tx_pin": "IO_PORTB_03"},
            "service_uart": {"port": 0, "tx_pin": "IO_PORTA_05", "rx_pin": "IO_PORTA_06"},
            "display": None,
        }
    return {
        "schema_version": 1,
        "board": board,
        "usb_device": "TCFG_USB_DEVICE",
        "memory": {"flash_mib": 8, "sdram_mib": 16, "external_ddr": True},
        "log_uart": {"port": 0},
        "service_uart": None,
        "display": None,
    }



def facts_for(chip_name: str, board: str) -> BoardFacts:
    """Facts that satisfy staging, mirroring what the board's Kconfig declares.

    These tests are about the staging tree and its digest, not about the
    reader, so the values only have to be the ones the staging hooks assert
    against.
    """
    if chip_name == "wl82":
        return BoardFacts(
            board=board,
            usb_device="TCFG_FUSB_DEVICE",
            memory=Memory(flash_mib=8, sdram_mib=8, external_ddr=False),
            log_uart=LogUart(port=1, tx_pin="IO_PORTB_03"),
            service_uart=ServiceUart(port=0, tx_pin="IO_PORTA_05", rx_pin="IO_PORTA_06"),
            display=None,
        )
    return BoardFacts(
        board=board,
        usb_device="TCFG_HUSB_DEVICE",
        memory=Memory(flash_mib=8, sdram_mib=16, external_ddr=True),
        log_uart=LogUart(port=0, tx_pin=None),
        service_uart=None,
        display=Display(
            panel_macro="TCFG_LCD_MIPI_ST7701S_480x800",
            device_name="MIPI_480x800_ST7701S",
            reset_io="IO_PORTB_00",
            backlight_io="IO_PORTB_01",
        ),
    )


class StagingReuseTest(unittest.TestCase):
    """Reuse and restage decisions, driven through create_staging_tree itself."""

    def _checkout(self, root: Path, chip, board_name: str) -> Path:
        """Write a synthetic SDK and the board files staging reads."""
        sdk = root / "sdk"
        vendor = sdk / chip.sdk_source_relative
        for name in ("include_lib", "lib", "tools"):
            (vendor / name).mkdir(parents=True)
            (vendor / name / "placeholder").write_text("input", encoding="utf-8")
        liba = vendor / "cpu" / chip.cpu / "liba"
        liba.mkdir(parents=True)
        (liba / "cpu.a").write_text("archive", encoding="utf-8")
        (vendor / "cpu" / chip.cpu / "sdk_ld.c").write_text("ld", encoding="utf-8")
        movable = vendor / "apps/common/movable"
        movable.mkdir(parents=True)
        (movable / "section.c").write_text("sections", encoding="utf-8")
        # A subdirectory of apps/common is linked, not copied, so its content is
        # never staged bytes; one is written here to pin that distinction.
        net = vendor / "apps/common/net"
        net.mkdir(parents=True)
        (net / "wifi.c").write_text("wifi", encoding="utf-8")

        profile = AUDIO_PROFILES.get(chip.name)
        if profile is not None:
            for tree in profile.staged_vendor_trees:
                (vendor / tree).mkdir(parents=True, exist_ok=True)
            for required in profile.staged_vendor_files:
                (vendor / required).parent.mkdir(parents=True, exist_ok=True)
                (vendor / required).write_text("stub", encoding="utf-8")
            board_profile = root / "tuyaopen" / "boards/JIELI" / profile.audio_board_name
            board_profile.mkdir(parents=True, exist_ok=True)
            (board_profile / "audio_config.h").write_text(
                "#define TUYA_AUDIO_STUB 1\n", encoding="utf-8"
            )
        facts_path = root / "tuyaopen" / "boards/JIELI" / board_name / "jieli_board.json"
        facts_path.parent.mkdir(parents=True, exist_ok=True)
        facts_path.write_text(
            json.dumps(synthetic_facts(board_name, chip.name)), encoding="utf-8"
        )

        demo_root = vendor / "apps/demo/demo_hello"
        board = demo_root / "board" / chip.cpu
        (demo_root / "include").mkdir(parents=True)
        board.mkdir(parents=True)
        (demo_root / "include/app_config.h").write_text(
            "#ifndef APP_CONFIG_H\n"
            "#define __FLASH_SIZE__ 1\n#define __SDRAM_SIZE__ 1\n"
            "#endif\n",
            encoding="utf-8",
        )
        sources = "    ../../../../../apps/demo/demo_hello/app_main.c\n"
        if chip.demo_lists_example_sources:
            sources = (
                "    ../../../../../apps/common/example/system/os/os_test.c \\\n" + sources
            )
        (board / "Makefile").write_text(
            "c_SRC_FILES := \\\n" + sources + "c_OBJS    :=\n", encoding="utf-8"
        )
        (board / "board.c").write_text(
            '#include "app_config.h"\n'
            '#include "asm/includes.h"\n'
            "REGISTER_DEVICES(device_table) = {\n};\n"
            "void board_early_init(void) { devices_init(); }\n"
            "void board_init(void)\n{\n}\n"
            "void debug_uart_init(void) { uart_init(&uart2_data); }\n"
            "UART2_PLATFORM_DATA_BEGIN(uart2_data)\n"
            "UART2_PLATFORM_DATA_END();\n",
            encoding="utf-8",
        )
        if chip.name == "wl83":
            (board / "board_demo.h").write_text(
                "#ifndef BOARD_DEMO_H\n#define BOARD_DEMO_H\n"
                "#define TCFG_UART0_BAUDRATE 115200\n"
                "#define CONFIG_NO_SDRAM_ENABLE\n#endif\n",
                encoding="utf-8",
            )
            (board / "chip_cfg.h").write_text(
                "#define __FLASH_SIZE__ 1\n#define __SDRAM_SIZE__ 1\n", encoding="utf-8"
            )
        (demo_root / "app_main.c").write_text("void app_main(void) {}\n", encoding="utf-8")
        return sdk

    def _stage(self, root: Path, chip, facts=None, platform_root=None):
        """Stage once and return (staging_root, build_root, sdk)."""
        profile = AUDIO_PROFILES.get(chip.name)
        selected = profile.audio_board_name if profile else "Synthetic_Board"
        facts = facts or facts_for(chip.name, selected)
        sdk = self._checkout(root, chip, selected)
        staging_root = root / "jieli-staging"
        build_root = create_staging_tree(
            sdk,
            staging_root,
            root / "tuyaopen",
            tuya_lib_dir=root / "libs",
            uart_log_port=1 if chip.name == "wl82" else 0,
            platform_root=platform_root or PLATFORM_ROOT,
            chip=chip,
            facts=facts,
        )
        return staging_root, build_root, sdk

    @staticmethod
    def _sentinel(staging_root: Path) -> Path:
        """A file that only a restage can remove: rmtree takes it with the tree."""
        marker = staging_root / "build" / ".restaged-sentinel"
        marker.write_text("staged", encoding="utf-8")
        return marker

    def _tree_state(self, root: Path) -> dict[str, tuple[int, int]]:
        """Every staged file's size and mtime, so a rewrite is visible."""
        return {
            path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_a_second_stage_with_unchanged_inputs_reuses_the_tree(self):
        for chip in JIELI_CHIPS.values():
            with self.subTest(chip=chip.name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                staging_root, build_root, _ = self._stage(root, chip)
                sentinel = self._sentinel(staging_root)
                before = self._tree_state(build_root)

                again = create_staging_tree(
                    root / "sdk",
                    staging_root,
                    root / "tuyaopen",
                    tuya_lib_dir=root / "libs",
                    uart_log_port=1 if chip.name == "wl82" else 0,
                    chip=chip,
                    facts=facts_for(chip.name, AUDIO_PROFILES[chip.name].audio_board_name),
                )

                self.assertEqual(again, build_root)
                # The sentinel surviving is the proof: a restage would have
                # removed the tree it lives in.
                self.assertTrue(sentinel.is_file())
                self.assertEqual(self._tree_state(build_root), before)

    def test_a_changed_board_fact_restages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chip = JIELI_CHIPS["wl82"]
            staging_root, build_root, _ = self._stage(root, chip)
            sentinel = self._sentinel(staging_root)

            # The facts come from the evaluated config now, so a changed fact
            # is a different facts object rather than an edited file.
            changed = BoardFacts(
                board="AC79_DevKitBoard",
                usb_device="TCFG_FUSB_DEVICE",
                memory=Memory(flash_mib=32, sdram_mib=8, external_ddr=False),
                log_uart=LogUart(port=1, tx_pin="IO_PORTB_03"),
                service_uart=ServiceUart(port=0, tx_pin="IO_PORTA_05", rx_pin="IO_PORTA_06"),
                display=None,
            )

            create_staging_tree(
                root / "sdk", staging_root, root / "tuyaopen",
                tuya_lib_dir=root / "libs", uart_log_port=1,
                chip=chip, facts=changed,
            )

            self.assertFalse(sentinel.exists())
            app_config = build_root / "apps/demo/demo_hello/include/app_config.h"
            self.assertIn(
                "__FLASH_SIZE__ (32 * 1024 * 1024)", app_config.read_text(encoding="utf-8")
            )

    def test_a_changed_uart_baudrate_restages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chip = JIELI_CHIPS["wl82"]
            staging_root, build_root, _ = self._stage(root, chip)
            sentinel = self._sentinel(staging_root)

            create_staging_tree(
                root / "sdk", staging_root, root / "tuyaopen",
                tuya_lib_dir=root / "libs", uart_log_port=1, uart_log_baudrate=921600,
                chip=chip, facts=facts_for(chip.name, "AC79_DevKitBoard"),
            )

            self.assertFalse(sentinel.exists())
            board = build_root / chip.board_build_relative / "board.c"
            self.assertIn(".baudrate = 921600,", board.read_text(encoding="utf-8"))

    def test_a_changed_audio_config_restages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chip = JIELI_CHIPS["wl82"]
            staging_root, _, _ = self._stage(root, chip)
            sentinel = self._sentinel(staging_root)

            header = root / "tuyaopen/boards/JIELI/AC79_DevKitBoard/audio_config.h"
            header.write_text("#define TUYA_AUDIO_STUB 2\n", encoding="utf-8")

            create_staging_tree(
                root / "sdk", staging_root, root / "tuyaopen",
                tuya_lib_dir=root / "libs", uart_log_port=1,
                chip=chip, facts=facts_for(chip.name, "AC79_DevKitBoard"),
            )

            self.assertFalse(sentinel.exists())

    def test_a_changed_copied_vendor_file_restages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chip = JIELI_CHIPS["wl82"]
            staging_root, build_root, sdk = self._stage(root, chip)
            sentinel = self._sentinel(staging_root)

            (sdk / "cpu/wl82/sdk_ld.c").write_text("edited linker source", encoding="utf-8")

            create_staging_tree(
                root / "sdk", staging_root, root / "tuyaopen",
                tuya_lib_dir=root / "libs", uart_log_port=1,
                chip=chip, facts=facts_for(chip.name, "AC79_DevKitBoard"),
            )

            self.assertFalse(sentinel.exists())
            self.assertEqual(
                (build_root / "cpu/wl82/sdk_ld.c").read_text(encoding="utf-8"),
                "edited linker source",
            )

    def test_a_removed_copied_vendor_file_restages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chip = JIELI_CHIPS["wl82"]
            staging_root, build_root, sdk = self._stage(root, chip)
            self.assertTrue((build_root / "cpu/wl82/sdk_ld.c").is_file())
            sentinel = self._sentinel(staging_root)

            (sdk / "cpu/wl82/sdk_ld.c").unlink()

            create_staging_tree(
                root / "sdk", staging_root, root / "tuyaopen",
                tuya_lib_dir=root / "libs", uart_log_port=1,
                chip=chip, facts=facts_for(chip.name, "AC79_DevKitBoard"),
            )

            self.assertFalse(sentinel.exists())
            self.assertFalse((build_root / "cpu/wl82/sdk_ld.c").exists())

    def test_a_change_under_a_linked_vendor_directory_is_not_a_restage(self):
        # apps/common's subdirectories are linked into staging, so the compiler
        # reads them straight from the checkout and their content is never
        # staged bytes. Covering them would only cost rebuilds.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chip = JIELI_CHIPS["wl82"]
            staging_root, _, sdk = self._stage(root, chip)
            sentinel = self._sentinel(staging_root)

            (sdk / "apps/common/net/wifi.c").write_text("edited wifi", encoding="utf-8")

            create_staging_tree(
                root / "sdk", staging_root, root / "tuyaopen",
                tuya_lib_dir=root / "libs", uart_log_port=1,
                chip=chip, facts=facts_for(chip.name, "AC79_DevKitBoard"),
            )

            self.assertTrue(sentinel.is_file())

    def test_switching_chip_restages_without_carrying_the_old_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wl82 = JIELI_CHIPS["wl82"]
            wl83 = JIELI_CHIPS["wl83"]
            staging_root, build_root, _ = self._stage(root, wl82)
            self.assertIn(
                "JIELI_SELECTED_CHIP_WL82",
                (build_root / wl82.board_build_relative / "Makefile").read_text(encoding="utf-8"),
            )
            sentinel = self._sentinel(staging_root)

            second = self._checkout(root, wl83, "AC792N_Develop_Board")
            again = create_staging_tree(
                second, staging_root, root / "tuyaopen",
                tuya_lib_dir=root / "libs", uart_log_port=0,
                chip=wl83, facts=facts_for("wl83", "AC792N_Develop_Board"),
            )

            self.assertFalse(sentinel.exists())
            self.assertEqual(again, staging_root / "build")
            makefile = (again / wl83.sdk_source_relative / wl83.board_build_relative / "Makefile")
            self.assertIn("JIELI_SELECTED_CHIP_WL83", makefile.read_text(encoding="utf-8"))
            self.assertFalse((again / wl82.board_build_relative).exists())

    def test_an_unreadable_state_file_forces_a_restage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chip = JIELI_CHIPS["wl82"]
            staging_root, _, _ = self._stage(root, chip)
            sentinel = self._sentinel(staging_root)
            (staging_root / STAGING_STATE_NAME).write_text("{ not json", encoding="utf-8")

            create_staging_tree(
                root / "sdk", staging_root, root / "tuyaopen",
                tuya_lib_dir=root / "libs", uart_log_port=1,
                chip=chip, facts=facts_for(chip.name, "AC79_DevKitBoard"),
            )

            self.assertFalse(sentinel.exists())

    def test_a_state_file_from_another_version_forces_a_restage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chip = JIELI_CHIPS["wl82"]
            staging_root, _, _ = self._stage(root, chip)
            sentinel = self._sentinel(staging_root)
            state_path = staging_root / STAGING_STATE_NAME
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["version"] = 999
            state_path.write_text(json.dumps(state), encoding="utf-8")

            create_staging_tree(
                root / "sdk", staging_root, root / "tuyaopen",
                tuya_lib_dir=root / "libs", uart_log_port=1,
                chip=chip, facts=facts_for(chip.name, "AC79_DevKitBoard"),
            )

            self.assertFalse(sentinel.exists())

    def test_a_partially_deleted_tree_forces_a_restage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chip = JIELI_CHIPS["wl82"]
            staging_root, _, _ = self._stage(root, chip)
            shutil.rmtree(staging_root / "build")

            self.assertFalse(
                staging_tree_is_current(staging_root, "sha256:anything")
            )

            build_root = create_staging_tree(
                root / "sdk", staging_root, root / "tuyaopen",
                tuya_lib_dir=root / "libs", uart_log_port=1,
                chip=chip, facts=facts_for(chip.name, "AC79_DevKitBoard"),
            )
            self.assertTrue((build_root / "cpu/wl82/sdk_ld.c").is_file())

    def test_a_changed_staging_script_restages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chip = JIELI_CHIPS["wl82"]
            platform_root = root / "platform"
            shutil.copytree(PLATFORM_ROOT / "tuyaos", platform_root / "tuyaos")
            shutil.copytree(
                PLATFORM_ROOT / "tools" / "jieli_build",
                platform_root / "tools" / "jieli_build",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            shutil.copy2(PLATFORM_ROOT / "build_example.py", platform_root / "build_example.py")

            staging_root, _, _ = self._stage(root, chip, platform_root=platform_root)
            sentinel = self._sentinel(staging_root)

            script = platform_root / "tools" / "jieli_build" / "board_config.py"
            script.write_text(
                script.read_text(encoding="utf-8") + "\n# an edit\n", encoding="utf-8"
            )

            create_staging_tree(
                root / "sdk", staging_root, root / "tuyaopen",
                tuya_lib_dir=root / "libs", uart_log_port=1,
                platform_root=platform_root, chip=chip, facts=facts_for(chip.name, "AC79_DevKitBoard"),
            )

            self.assertFalse(sentinel.exists())

    def test_a_changed_adapter_tree_restages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chip = JIELI_CHIPS["wl82"]
            platform_root = root / "platform"
            shutil.copytree(PLATFORM_ROOT / "tuyaos", platform_root / "tuyaos")
            staging_root, _, _ = self._stage(root, chip, platform_root=platform_root)
            sentinel = self._sentinel(staging_root)

            entry = platform_root / "tuyaos" / "tuyaos_adapter" / "src" / "driver" / "tkl_uart.c"
            entry.write_text(
                entry.read_text(encoding="utf-8") + "\n/* an edit */\n", encoding="utf-8"
            )

            create_staging_tree(
                root / "sdk", staging_root, root / "tuyaopen",
                tuya_lib_dir=root / "libs", uart_log_port=1,
                platform_root=platform_root, chip=chip, facts=facts_for(chip.name, "AC79_DevKitBoard"),
            )

            self.assertFalse(sentinel.exists())


class StagingInputComponentTest(unittest.TestCase):
    """The component digest itself, without a full staging run."""

    def _write_inputs(self, root: Path) -> None:
        """Lay out the smallest platform tree staging_input_components reads."""
        platform_root = root / "platform"
        (platform_root / "tools" / "jieli_build").mkdir(parents=True, exist_ok=True)
        (platform_root / "tools" / "jieli_build" / "board_config.py").write_text(
            "one\n", encoding="utf-8"
        )
        (platform_root / "build_example.py").write_text("staging\n", encoding="utf-8")
        (platform_root / "tuyaos" / "entry").mkdir(parents=True, exist_ok=True)
        (platform_root / "tuyaos" / "entry" / "jieli_app_entry.c").write_text(
            "entry\n", encoding="utf-8"
        )
        (platform_root / "tuyaos" / "tuyaos_adapter").mkdir(parents=True, exist_ok=True)
        (platform_root / "tuyaos" / "tuyaos_adapter" / "adapter_sources.txt").write_text(
            "src/a.c\n", encoding="utf-8"
        )
        (root / "jieli_board.json").write_text("{}", encoding="utf-8")

    def _components(self, root: Path, chip=None) -> dict[str, str]:
        chip = chip or JIELI_CHIPS["wl82"]
        return staging_input_components(
            sdk_root=root / "sdk",
            tuyaopen_root=root / "tuyaopen",
            platform_root=root / "platform",
            chip=chip,
            board_name="AC79_DevKitBoard",
            board_facts="sha256:" + "0" * 64,
            audio_config_source=None,
            header_dir=None,
            tuya_lib_dir=root / "libs",
            uart_log_port=1,
            uart_log_baudrate=115200,
        )

    def test_an_edited_staging_script_changes_the_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_inputs(root)
            before = staging_input_digest(**self._components(root))
            (root / "platform/tools/jieli_build/board_config.py").write_text(
                "two\n", encoding="utf-8"
            )
            self.assertNotEqual(staging_input_digest(**self._components(root)), before)

    def test_an_edited_build_example_changes_the_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_inputs(root)
            before = staging_input_digest(**self._components(root))
            (root / "platform/build_example.py").write_text("edited\n", encoding="utf-8")
            self.assertNotEqual(staging_input_digest(**self._components(root)), before)

    def test_python_caches_do_not_change_the_digest(self):
        # A test run rewrites __pycache__ with fresh embedded timestamps; that
        # must not invalidate every staging tree the tests then inspect.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_inputs(root)
            before = staging_input_digest(**self._components(root))
            cache = root / "platform/tools/jieli_build/__pycache__"
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "board_config.cpython-314.pyc").write_bytes(b"\x00\x01\x02")
            self.assertEqual(staging_input_digest(**self._components(root)), before)

    def test_a_missing_input_tree_is_not_an_empty_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_inputs(root)
            before = staging_input_digest(**self._components(root))
            shutil.rmtree(root / "platform/tuyaos/entry")
            self.assertNotEqual(staging_input_digest(**self._components(root)), before)

    def test_a_new_staging_script_changes_the_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_inputs(root)
            before = staging_input_digest(**self._components(root))
            (root / "platform/tools/jieli_build/new_hook.py").write_text(
                "added\n", encoding="utf-8"
            )
            self.assertNotEqual(staging_input_digest(**self._components(root)), before)

    def test_the_vendor_fingerprint_notices_a_size_and_an_mtime_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_inputs(root)
            copied = root / "sdk" / "cpu" / "wl82"
            copied.mkdir(parents=True)
            source = copied / "sdk_ld.c"
            source.write_text("ld", encoding="utf-8")
            before = staging_input_digest(**self._components(root))

            source.write_text("a longer linker source", encoding="utf-8")
            after_size = staging_input_digest(**self._components(root))
            self.assertNotEqual(after_size, before)

            # Same length, new mtime: the metadata still sees it.
            source.write_text("ld", encoding="utf-8")
            os.utime(source, (1, 1))
            after_mtime = staging_input_digest(**self._components(root))
            self.assertNotEqual(after_mtime, before)

    def test_an_unchanged_tree_digests_the_same(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_inputs(root)
            copied = root / "sdk" / "cpu" / "wl82"
            copied.mkdir(parents=True)
            (copied / "sdk_ld.c").write_text("ld", encoding="utf-8")
            self.assertEqual(
                staging_input_digest(**self._components(root)),
                staging_input_digest(**self._components(root)),
            )


if __name__ == "__main__":
    unittest.main()
