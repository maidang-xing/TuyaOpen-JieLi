import tempfile
import unittest
from pathlib import Path

from tools.jieli_build.board_config import configure_board_memory
from tools.jieli_build.board_facts import BoardFacts, LogUart, Memory
from tools.jieli_build.errors import BuildError


def board_facts(flash_mib: int, sdram_mib: int, external_ddr: bool) -> BoardFacts:
    """A board whose only interesting fact is its memory map."""
    return BoardFacts(
        board="Test_Board",
        usb_device="TCFG_FUSB_DEVICE",
        memory=Memory(flash_mib=flash_mib, sdram_mib=sdram_mib, external_ddr=external_ddr),
        log_uart=LogUart(port=0, tx_pin=None),
        service_uart=None,
        display=None,
    )


class BoardMemoryConfigTest(unittest.TestCase):
    """The sizes are the board's facts; the file they land in is the chip's."""

    def test_configures_flash_and_sdram_sizes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_config = Path(temp_dir) / "app_config.h"
            app_config.write_text(
                "#define __FLASH_SIZE__    (4 * 1024 * 1024)\n"
                "#define __SDRAM_SIZE__    (2 * 1024 * 1024)\n",
                encoding="utf-8",
            )

            configure_board_memory(app_config, board_facts(8, 8, False))

            content = app_config.read_text(encoding="utf-8")
            self.assertIn("#define __FLASH_SIZE__    (8 * 1024 * 1024)", content)
            self.assertIn("#define __SDRAM_SIZE__    (8 * 1024 * 1024)", content)

    def test_rejects_a_config_missing_a_memory_macro(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_config = Path(temp_dir) / "app_config.h"
            app_config.write_text("#define __FLASH_SIZE__ (4 * 1024 * 1024)\n", encoding="utf-8")

            with self.assertRaises(BuildError):
                configure_board_memory(app_config, board_facts(8, 8, False))


class ExternalDdrMemoryConfigTest(unittest.TestCase):
    CHIP = (
        "#define __FLASH_SIZE__    (1 * 1024 * 1024)\n"
        "#define __SDRAM_SIZE__    (2 * 1024 * 1024)\n"
    )
    BOARD = "#define CONFIG_NO_SDRAM_ENABLE\n"

    def _write(self, temp_dir, chip_content=CHIP, board_content=BOARD):
        chip = Path(temp_dir) / "chip_cfg.h"
        board = Path(temp_dir) / "board_demo.h"
        chip.write_text(chip_content, encoding="utf-8")
        board.write_text(board_content, encoding="utf-8")
        return chip, board

    def test_configures_the_boards_own_flash_and_sdram_sizes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            chip, board = self._write(temp_dir)

            configure_board_memory(chip, board_facts(8, 16, True), ddr_config_file=board)

            content = chip.read_text(encoding="utf-8")
            self.assertIn("#define __FLASH_SIZE__    (8 * 1024 * 1024)", content)
            self.assertIn("#define __SDRAM_SIZE__    (16 * 1024 * 1024)", content)

    def test_targets_macros_by_name_not_by_first_occurrence(self):
        # A decoy carries the same literal before __FLASH_SIZE__. Rewriting the
        # first "(1 * 1024 * 1024)" would resize the decoy and leave flash alone.
        with tempfile.TemporaryDirectory() as temp_dir:
            chip, board = self._write(
                temp_dir,
                "#define __DECOY_SIZE__    (1 * 1024 * 1024)\n"
                "#define __FLASH_SIZE__    (1 * 1024 * 1024)\n"
                "#define __SDRAM_SIZE__    (2 * 1024 * 1024)\n",
            )

            configure_board_memory(chip, board_facts(8, 16, True), ddr_config_file=board)

            content = chip.read_text(encoding="utf-8")
            self.assertIn("#define __DECOY_SIZE__    (1 * 1024 * 1024)", content)
            self.assertIn("#define __FLASH_SIZE__    (8 * 1024 * 1024)", content)
            self.assertIn("#define __SDRAM_SIZE__    (16 * 1024 * 1024)", content)

    def test_enables_external_ddr_on_the_board_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            chip, board = self._write(temp_dir)

            configure_board_memory(chip, board_facts(8, 16, True), ddr_config_file=board)

            content = board.read_text(encoding="utf-8")
            self.assertNotIn("CONFIG_NO_SDRAM_ENABLE", content)
            self.assertIn("External DDR is enabled", content)

    def test_leaves_the_sdram_switch_alone_when_the_board_has_no_ddr(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            chip, board = self._write(temp_dir)

            configure_board_memory(chip, board_facts(8, 8, False), ddr_config_file=board)

            self.assertIn("CONFIG_NO_SDRAM_ENABLE", board.read_text(encoding="utf-8"))

    def test_rejects_a_chip_config_missing_a_memory_macro(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            chip, board = self._write(
                temp_dir, chip_content="#define __FLASH_SIZE__ (1 * 1024 * 1024)\n"
            )

            with self.assertRaises(BuildError):
                configure_board_memory(chip, board_facts(8, 16, True), ddr_config_file=board)

    def test_rejects_a_board_config_without_the_sdram_switch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            chip, board = self._write(temp_dir, board_content="/* no switch here */\n")

            with self.assertRaises(BuildError):
                configure_board_memory(chip, board_facts(8, 16, True), ddr_config_file=board)

    def test_rejects_ddr_when_the_chip_has_no_board_profile_to_enable_it(self):
        # A chip whose SDK enables DDR elsewhere passes no ddr_config_file; a
        # board that declares DDR must then fail rather than be silently staged
        # with a map the SDK will not use.
        with tempfile.TemporaryDirectory() as temp_dir:
            chip, _ = self._write(temp_dir)

            with self.assertRaises(BuildError):
                configure_board_memory(chip, board_facts(8, 16, True))

    def test_rejects_a_duplicate_macro_definition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            chip, board = self._write(
                temp_dir,
                "#define __FLASH_SIZE__    (1 * 1024 * 1024)\n"
                "#define __FLASH_SIZE__    (1 * 1024 * 1024)\n"
                "#define __SDRAM_SIZE__    (2 * 1024 * 1024)\n",
            )

            with self.assertRaises(BuildError):
                configure_board_memory(chip, board_facts(8, 16, True), ddr_config_file=board)

    def test_accepts_crlf_line_endings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            chip = Path(temp_dir) / "chip_cfg.h"
            board = Path(temp_dir) / "board_demo.h"
            # write_bytes, because write_text would translate the newlines away.
            chip.write_bytes(
                b"#define __FLASH_SIZE__    (1 * 1024 * 1024)\r\n"
                b"#define __SDRAM_SIZE__    (2 * 1024 * 1024)\r\n"
            )
            board.write_bytes(b"#define CONFIG_NO_SDRAM_ENABLE\r\n")

            configure_board_memory(chip, board_facts(8, 16, True), ddr_config_file=board)

            content = chip.read_text(encoding="utf-8")
            self.assertIn("#define __FLASH_SIZE__    (8 * 1024 * 1024)", content)
            self.assertIn("#define __SDRAM_SIZE__    (16 * 1024 * 1024)", content)
            self.assertNotIn("CONFIG_NO_SDRAM_ENABLE", board.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
