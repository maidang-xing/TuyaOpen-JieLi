"""The board facts reader: what it accepts, and how loudly it rejects the rest.

The facts are Kconfig settings under boards/JIELI/<board>/Kconfig, so the reader
takes the evaluated config mapping rather than a file of its own.
"""

import unittest

from tools.jieli_build.board_facts import (
    KCONFIG_PREFIX,
    PANELS,
    SYMBOL_PREFIX,
    USB_DEVICE_MACROS,
    board_facts_digest,
    load_board_facts,
)
from tools.jieli_build.chip_profiles import PLATFORM_ROOT
from tools.jieli_build.errors import BuildError

TUYAOPEN_ROOT = PLATFORM_ROOT.parents[1]

# A board that declares everything, in the shape the evaluated config carries:
# Kconfig writes CONFIG_<name>=<value> and omits a symbol the board does not
# declare.
FULL_BOARD = {
    "CONFIG_BOARD_CHOICE": "Test_Board",
    "CONFIG_JIELI_BOARD_USB_ROLE": "host",
    "CONFIG_JIELI_BOARD_FLASH_MIB": "8",
    "CONFIG_JIELI_BOARD_SDRAM_MIB": "16",
    "CONFIG_JIELI_BOARD_EXTERNAL_DDR": "y",
    "CONFIG_JIELI_BOARD_LOG_UART_PORT": "0",
    "CONFIG_JIELI_BOARD_LOG_UART_TX_PIN": "",
    "CONFIG_JIELI_BOARD_HAS_SERVICE_UART": "y",
    "CONFIG_JIELI_BOARD_SERVICE_UART_PORT": "0",
    "CONFIG_JIELI_BOARD_SERVICE_UART_TX_PIN": "IO_PORTA_05",
    "CONFIG_JIELI_BOARD_SERVICE_UART_RX_PIN": "IO_PORTA_06",
    "CONFIG_JIELI_BOARD_HAS_DISPLAY": "y",
    "CONFIG_JIELI_BOARD_DISPLAY_PANEL": "MIPI_480x800_ST7701S",
    "CONFIG_JIELI_BOARD_DISPLAY_RESET_IO": "IO_PORTB_00",
    "CONFIG_JIELI_BOARD_DISPLAY_BACKLIGHT_IO": "IO_PORTB_01",
}


def config_with(**overrides) -> dict[str, str]:
    values = dict(FULL_BOARD)
    for name, value in overrides.items():
        key = name if name.startswith("CONFIG_") else SYMBOL_PREFIX + name
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return values


class BoardFactsTest(unittest.TestCase):
    def test_reads_every_fact_into_a_typed_value(self):
        facts = load_board_facts(config_with())

        self.assertEqual(facts.board, "Test_Board")
        self.assertEqual(facts.usb_device, "TCFG_HUSB_DEVICE")
        self.assertEqual(facts.memory.flash_mib, 8)
        self.assertEqual(facts.memory.sdram_mib, 16)
        self.assertTrue(facts.memory.external_ddr)
        self.assertEqual(facts.log_uart.port, 0)
        self.assertIsNone(facts.log_uart.tx_pin)
        self.assertEqual(facts.service_uart.port, 0)
        self.assertEqual(facts.service_uart.tx_pin, "IO_PORTA_05")
        self.assertEqual(facts.display.panel_macro, "TCFG_LCD_MIPI_ST7701S_480x800")
        self.assertEqual(facts.display.device_name, "MIPI_480x800_ST7701S")
        self.assertEqual(facts.display.reset_io, "IO_PORTB_00")
        self.assertEqual(facts.display.backlight_io, "IO_PORTB_01")

    def test_the_usb_role_maps_onto_the_vendors_macro(self):
        for role, macro in USB_DEVICE_MACROS.items():
            with self.subTest(role=role):
                facts = load_board_facts(config_with(USB_ROLE=role))
                self.assertEqual(facts.usb_device, macro)

    def test_an_unknown_usb_role_is_rejected(self):
        with self.assertRaisesRegex(BuildError, "USB_ROLE.*expected one of"):
            load_board_facts(config_with(USB_ROLE="otg"))

    def test_an_unknown_panel_is_rejected(self):
        with self.assertRaisesRegex(BuildError, "DISPLAY_PANEL.*no vendor macro"):
            load_board_facts(config_with(DISPLAY_PANEL="ILI9488"))

    def test_the_panel_macro_is_not_the_board_s_business(self):
        # The board names the model; which macro and driver that means is the
        # platform's mapping. So every known model has an entry.
        for model, (macro, device) in PANELS.items():
            with self.subTest(model=model):
                facts = load_board_facts(config_with(DISPLAY_PANEL=model))
                self.assertEqual(facts.display.panel_macro, macro)
                self.assertEqual(facts.display.device_name, device)

    def test_a_board_with_a_service_uart_and_no_panel(self):
        facts = load_board_facts(
            config_with(
                HAS_DISPLAY=None,
                DISPLAY_PANEL=None,
                DISPLAY_RESET_IO=None,
                DISPLAY_BACKLIGHT_IO=None,
            )
        )

        self.assertIsNone(facts.display)
        self.assertIsNotNone(facts.service_uart)

    def test_a_board_with_a_panel_and_no_service_uart(self):
        facts = load_board_facts(
            config_with(
                HAS_SERVICE_UART=None,
                SERVICE_UART_PORT=None,
                SERVICE_UART_TX_PIN=None,
                SERVICE_UART_RX_PIN=None,
            )
        )

        self.assertIsNone(facts.service_uart)
        self.assertIsNotNone(facts.display)

    def test_no_board_selected_fails(self):
        with self.assertRaisesRegex(BuildError, "CONFIG_BOARD_CHOICE is missing"):
            load_board_facts(config_with(**{"CONFIG_BOARD_CHOICE": ""}))

    def test_a_missing_setting_fails_naming_the_symbol(self):
        with self.assertRaisesRegex(BuildError, "CONFIG_JIELI_BOARD_FLASH_MIB"):
            load_board_facts(config_with(FLASH_MIB=None))

    def test_a_setting_that_is_not_an_integer_fails(self):
        with self.assertRaisesRegex(BuildError, "not an integer"):
            load_board_facts(config_with(SDRAM_MIB="sixteen"))

    def test_a_memory_size_must_be_positive(self):
        with self.assertRaisesRegex(BuildError, "must be positive"):
            load_board_facts(config_with(FLASH_MIB="0"))

    def test_a_negative_uart_port_fails(self):
        with self.assertRaisesRegex(BuildError, "must not be negative"):
            load_board_facts(config_with(LOG_UART_PORT="-1"))

    def test_a_service_uart_must_name_both_pins(self):
        with self.assertRaisesRegex(BuildError, "SERVICE_UART_RX_PIN"):
            load_board_facts(config_with(SERVICE_UART_RX_PIN=None))

    def test_a_display_must_name_its_pins(self):
        with self.assertRaisesRegex(BuildError, "DISPLAY_RESET_IO"):
            load_board_facts(config_with(DISPLAY_RESET_IO=None))

    def test_an_unset_flag_means_the_feature_is_absent(self):
        # Kconfig omits a bool the board leaves at n, rather than writing
        # "# X is not set" into the build parameters. Absent has to mean false.
        facts = load_board_facts(config_with(HAS_SERVICE_UART=None))
        self.assertIsNone(facts.service_uart)

    def test_the_digest_follows_every_field(self):
        base = board_facts_digest(load_board_facts(config_with()))

        for override in (
            {"USB_ROLE": "device"},
            {"FLASH_MIB": "16"},
            {"SDRAM_MIB": "8"},
            {"EXTERNAL_DDR": None},
            {"LOG_UART_PORT": "1"},
            {"LOG_UART_TX_PIN": "IO_PORTB_03"},
            {"HAS_SERVICE_UART": None},
            {"DISPLAY_PANEL": None, "HAS_DISPLAY": None},
        ):
            with self.subTest(override=override):
                changed = board_facts_digest(load_board_facts(config_with(**override)))
                self.assertNotEqual(base, changed)

    def test_the_digest_is_stable_for_the_same_facts(self):
        self.assertEqual(
            board_facts_digest(load_board_facts(config_with())),
            board_facts_digest(load_board_facts(config_with())),
        )


class BoardKconfigTest(unittest.TestCase):
    """The reader and the board Kconfigs have to agree on the symbol names.

    The reader builds each name from SYMBOL_PREFIX and a suffix, so a typo in
    either place would only show up as a missing setting at build time.
    """

    BOARD_DIRS = ("AC792N_Develop_Board", "AC79_DevKitBoard")

    def _kconfig_text(self, board: str) -> str:
        path = TUYAOPEN_ROOT / "boards" / "JIELI" / board / "Kconfig"
        if not path.is_file():
            self.skipTest(f"board Kconfig not present: {path}")
        return path.read_text(encoding="utf-8")

    def test_every_symbol_the_reader_asks_for_is_declared(self):
        wanted = {
            "USB_ROLE",
            "FLASH_MIB",
            "SDRAM_MIB",
            "EXTERNAL_DDR",
            "LOG_UART_PORT",
            "LOG_UART_TX_PIN",
            "HAS_SERVICE_UART",
            "HAS_DISPLAY",
        }
        for board in self.BOARD_DIRS:
            declared = self._kconfig_text(board)
            for suffix in sorted(wanted):
                with self.subTest(board=board, symbol=suffix):
                    self.assertIn(f"config {KCONFIG_PREFIX}{suffix}", declared)

    def test_the_nested_symbols_are_declared_by_the_board_that_needs_them(self):
        nested = {
            "AC792N_Develop_Board": ("DISPLAY_PANEL", "DISPLAY_RESET_IO"),
            "AC79_DevKitBoard": ("SERVICE_UART_TX_PIN", "SERVICE_UART_RX_PIN"),
        }
        for board, suffixes in nested.items():
            declared = self._kconfig_text(board)
            for suffix in suffixes:
                with self.subTest(board=board, symbol=suffix):
                    self.assertIn(f"config {KCONFIG_PREFIX}{suffix}", declared)

    def test_no_board_facts_json_is_left_behind(self):
        # The facts moved into Kconfig; a stray file would be a second source.
        for board in self.BOARD_DIRS:
            path = TUYAOPEN_ROOT / "boards" / "JIELI" / board / "jieli_board.json"
            with self.subTest(board=board):
                self.assertFalse(path.exists(), path)
