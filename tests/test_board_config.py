"""The board-config hooks read their values from the board's facts."""

import tempfile
import unittest
from pathlib import Path

from tools.jieli_build.board_config import (
    configure_lcd_board_header,
    configure_log_uart_block,
    configure_log_uart_baudrate,
    configure_service_uart,
)
from tools.jieli_build.board_facts import BoardFacts, Display, LogUart, Memory, ServiceUart
from tools.jieli_build.errors import BuildError


def facts_with(**overrides) -> BoardFacts:
    """A board whose facts a test can vary one field at a time."""
    values = {
        "board": "Test_Board",
        "usb_device": "TCFG_FUSB_DEVICE",
        "memory": Memory(flash_mib=8, sdram_mib=8, external_ddr=False),
        "log_uart": LogUart(port=1, tx_pin="IO_PORTB_03"),
        "service_uart": ServiceUart(port=0, tx_pin="IO_PORTA_05", rx_pin="IO_PORTA_06"),
        "display": None,
    }
    values.update(overrides)
    return BoardFacts(source=Path("jieli_board.json"), **values)


class ServiceUartTest(unittest.TestCase):
    BOARD = (
        '#include "app_config.h"\n'
        "REGISTER_DEVICES(device_table) = {\n};\n"
        "UART2_PLATFORM_DATA_BEGIN(uart2_data)\n"
        "UART2_PLATFORM_DATA_END();\n"
    )

    def _board(self, temp_dir) -> Path:
        board = Path(temp_dir) / "board.c"
        board.write_text(self.BOARD, encoding="utf-8")
        return board

    def test_writes_the_boards_pins_and_port_group(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = self._board(temp_dir)

            configure_service_uart(board, facts_with())

            content = board.read_text(encoding="utf-8")
            self.assertIn(".port = PORTA_5_6,", content)
            self.assertIn(".tx_pin = IO_PORTA_05,", content)
            self.assertIn(".rx_pin = IO_PORTA_06,", content)
            self.assertIn('{"uart0", &uart_dev_ops, (void *)&uart0_data },', content)

    def test_a_different_port_is_named_after_its_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = self._board(temp_dir)
            facts = facts_with(service_uart=ServiceUart(0, "IO_PORTB_07", "IO_PORTB_08"))

            configure_service_uart(board, facts)

            content = board.read_text(encoding="utf-8")
            self.assertIn(".port = PORTB_7_8,", content)
            self.assertIn('{"uart0", &uart_dev_ops, (void *)&uart0_data },', content)

    def test_pins_on_different_ports_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = self._board(temp_dir)
            facts = facts_with(service_uart=ServiceUart(0, "IO_PORTA_05", "IO_PORTB_06"))

            with self.assertRaisesRegex(BuildError, "same-port"):
                configure_service_uart(board, facts)


class LogUartTest(unittest.TestCase):
    def test_block_hook_asserts_the_documented_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = Path(temp_dir) / "board.c"
            board.write_text("UART2_PLATFORM_DATA_BEGIN(uart2_data)\n", encoding="utf-8")

            with self.assertRaisesRegex(BuildError, "logs on SDK UART1"):
                configure_log_uart_block(board, 0, 115200, facts_with())

    def test_baudrate_hook_asserts_the_documented_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            header = Path(temp_dir) / "board_demo.h"
            header.write_text("#define TCFG_UART0_BAUDRATE 1000000\n", encoding="utf-8")
            facts = facts_with(log_uart=LogUart(port=0, tx_pin=None))

            configure_log_uart_baudrate(header, 0, 115200, facts)

            self.assertIn("#define TCFG_UART0_BAUDRATE 115200", header.read_text(encoding="utf-8"))

    def test_baudrate_hook_refuses_a_port_the_board_does_not_log_on(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            header = Path(temp_dir) / "board_demo.h"
            header.write_text("#define TCFG_UART0_BAUDRATE 1000000\n", encoding="utf-8")

            with self.assertRaisesRegex(BuildError, "logs on SDK UART1"):
                configure_log_uart_baudrate(header, 0, 115200, facts_with())


class LcdBoardHeaderTest(unittest.TestCase):
    PANEL = Display(
        panel_macro="TCFG_LCD_MIPI_ST7701S_480x800",
        device_name="MIPI_480x800_ST7701S",
        reset_io="IO_PORTB_00",
        backlight_io="IO_PORTB_01",
    )

    def test_selects_the_boards_panel_and_pins(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            header = Path(temp_dir) / "board_demo.h"
            header.write_text("#ifndef BOARD_DEMO_H\n#endif\n", encoding="utf-8")

            configure_lcd_board_header(header, facts_with(display=self.PANEL))

            content = header.read_text(encoding="utf-8")
            self.assertIn("#define TCFG_LCD_MIPI_ST7701S_480x800    1", content)
            self.assertIn('"MIPI_480x800_ST7701S"', content)
            self.assertIn("TCFG_LCD_RESET_IO                   IO_PORTB_00", content)
            self.assertIn("TCFG_LCD_BL_IO                      IO_PORTB_01", content)

    def test_a_board_with_no_panel_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            header = Path(temp_dir) / "board_demo.h"
            header.write_text("#ifndef BOARD_DEMO_H\n#endif\n", encoding="utf-8")

            with self.assertRaisesRegex(BuildError, "declares no display panel"):
                configure_lcd_board_header(header, facts_with())


if __name__ == "__main__":
    unittest.main()
