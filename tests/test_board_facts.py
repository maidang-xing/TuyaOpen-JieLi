"""The board facts reader: what it accepts, and how loudly it rejects the rest."""

import json
import tempfile
import unittest
from pathlib import Path

from tools.jieli_build.board_facts import (
    BOARD_FACTS_FILENAME,
    SCHEMA_VERSION,
    BoardFacts,
    board_facts_path,
    load_board_facts,
)
from tools.jieli_build.chip_profiles import PLATFORM_ROOT
from tools.jieli_build.errors import BuildError

TUYAOPEN_ROOT = PLATFORM_ROOT.parents[1]


def complete_facts(board: str = "Test_Board") -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "board": board,
        "usb_device": "TCFG_HUSB_DEVICE",
        "memory": {"flash_mib": 8, "sdram_mib": 16, "external_ddr": True},
        "log_uart": {"port": 0},
        "service_uart": None,
        "display": {
            "panel_macro": "TCFG_LCD_MIPI_ST7701S_480x800",
            "device_name": "MIPI_480x800_ST7701S",
            "reset_io": "IO_PORTB_00",
            "backlight_io": "IO_PORTB_01",
        },
    }


class BoardFactsTest(unittest.TestCase):
    def _write(self, root: Path, board: str, data) -> Path:
        path = root / "boards" / "JIELI" / board / BOARD_FACTS_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def _load(self, data, board: str = "Test_Board") -> BoardFacts:
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write(Path(temp_dir), board, data)
            return load_board_facts(Path(temp_dir), board)

    # -- the happy path ---------------------------------------------------

    def test_reads_every_fact_into_a_typed_value(self):
        facts = self._load(complete_facts())

        self.assertIsInstance(facts, BoardFacts)
        self.assertEqual(facts.board, "Test_Board")
        self.assertEqual(facts.usb_device, "TCFG_HUSB_DEVICE")
        self.assertEqual(
            (facts.memory.flash_mib, facts.memory.sdram_mib, facts.memory.external_ddr),
            (8, 16, True),
        )
        self.assertEqual(facts.log_uart.port, 0)
        self.assertIsNone(facts.log_uart.tx_pin)
        self.assertIsNone(facts.service_uart)
        self.assertEqual(facts.display.device_name, "MIPI_480x800_ST7701S")
        self.assertEqual(facts.display.reset_io, "IO_PORTB_00")
        self.assertEqual(facts.display.backlight_io, "IO_PORTB_01")

    def test_a_board_with_a_service_uart_and_no_panel(self):
        data = complete_facts()
        data["display"] = None
        data["service_uart"] = {
            "port": 0,
            "tx_pin": "IO_PORTA_05",
            "rx_pin": "IO_PORTA_06",
        }
        data["log_uart"] = {"port": 1, "tx_pin": "IO_PORTB_03"}

        facts = self._load(data)

        self.assertIsNone(facts.display)
        self.assertEqual(facts.service_uart.tx_pin, "IO_PORTA_05")
        self.assertEqual(facts.service_uart.rx_pin, "IO_PORTA_06")
        self.assertEqual(facts.log_uart.tx_pin, "IO_PORTB_03")

    def test_board_facts_path_follows_the_catalog_layout(self):
        self.assertEqual(
            board_facts_path(Path("/root"), "Some_Board"),
            Path("/root/boards/JIELI/Some_Board/jieli_board.json"),
        )

    # -- failure paths ----------------------------------------------------

    def test_a_missing_file_fails_and_says_what_to_add(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(BuildError) as caught:
                load_board_facts(Path(temp_dir), "Absent_Board")

        message = str(caught.exception)
        self.assertIn("Absent_Board", message)
        self.assertIn("boards/JIELI/Absent_Board/jieli_board.json", message)
        self.assertIn("does not declare its hardware facts", message)

    def test_no_board_selected_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(BuildError, "no Jieli board was selected"):
                load_board_facts(Path(temp_dir), None)

    def test_an_unsupported_schema_version_fails(self):
        data = complete_facts()
        data["schema_version"] = 2

        with self.assertRaises(BuildError) as caught:
            self._load(data)

        self.assertIn("schema_version", str(caught.exception))

    def test_the_board_field_must_match_the_directory(self):
        data = complete_facts(board="Other_Board")

        with self.assertRaises(BuildError) as caught:
            self._load(data, board="Test_Board")

        message = str(caught.exception)
        self.assertIn("'Other_Board'", message)
        self.assertIn("'Test_Board'", message)

    def test_a_missing_required_field_fails_naming_it(self):
        for field in ("usb_device", "memory", "log_uart", "service_uart", "display"):
            with self.subTest(field=field):
                data = complete_facts()
                del data[field]

                with self.assertRaises(BuildError) as caught:
                    self._load(data)

                message = str(caught.exception)
                self.assertIn("missing required field", message)
                self.assertIn(repr(field), message)

    def test_an_unknown_field_is_rejected(self):
        # A field the platform does not read would suggest a fact is honoured
        # when it is not.
        data = complete_facts()
        data["screen_brightness"] = 42

        with self.assertRaises(BuildError) as caught:
            self._load(data)

        self.assertIn("unknown field", str(caught.exception))
        self.assertIn("screen_brightness", str(caught.exception))

    def test_a_memory_size_must_be_a_positive_integer(self):
        for value in ("8", 0, -4, True):
            with self.subTest(value=value):
                data = complete_facts()
                data["memory"]["flash_mib"] = value

                with self.assertRaises(BuildError):
                    self._load(data)

    def test_external_ddr_must_be_a_boolean(self):
        data = complete_facts()
        data["memory"]["external_ddr"] = "true"

        with self.assertRaisesRegex(BuildError, "external_ddr"):
            self._load(data)

    def test_a_service_uart_must_name_both_pins(self):
        data = complete_facts()
        data["service_uart"] = {"port": 0, "tx_pin": "IO_PORTA_05"}

        with self.assertRaisesRegex(BuildError, "rx_pin"):
            self._load(data)

    def test_a_display_must_name_all_four_fields(self):
        data = complete_facts()
        del data["display"]["backlight_io"]

        with self.assertRaisesRegex(BuildError, "backlight_io"):
            self._load(data)

    def test_unreadable_json_fails(self):
        with self.assertRaisesRegex(BuildError, "not readable JSON"):
            self._load("{ this is not json")

    def test_a_non_object_document_fails(self):
        with self.assertRaisesRegex(BuildError, "must be a JSON object"):
            self._load("[1, 2, 3]")

    # -- the real catalog -------------------------------------------------

    @unittest.skipUnless(
        (TUYAOPEN_ROOT / "boards/JIELI/AC79_DevKitBoard/jieli_board.json").is_file()
        and (TUYAOPEN_ROOT / "boards/JIELI/AC792N_Develop_Board/jieli_board.json").is_file(),
        "the board catalog is not checked out next to this platform",
    )
    def test_the_catalog_boards_all_load(self):
        for board in ("AC79_DevKitBoard", "AC792N_Develop_Board"):
            with self.subTest(board=board):
                facts = load_board_facts(TUYAOPEN_ROOT, board)
                self.assertEqual(facts.board, board)


if __name__ == "__main__":
    unittest.main()
