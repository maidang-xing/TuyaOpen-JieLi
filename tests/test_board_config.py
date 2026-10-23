"""The board-config hooks read their values from the board's facts."""

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.jieli_build.board_config import (
    UART_PORT_GROUPS,
    WL82_LOWPOWER_SEL,
    WL82_STA_PS_MACRO,
    WL83_LOWPOWER_FUNCTION,
    WL83_STA_PS_VALUE,
    _check_ps_mode,
    configure_lcd_board_header,
    configure_log_uart_block,
    configure_log_uart_baudrate,
    configure_lowpower,
    configure_service_uart,
)
from tools.jieli_build.board_facts import BoardFacts, Display, LogUart, Memory, ServiceUart
from tools.jieli_build.chip_profiles import PLATFORM_ROOT
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
    return BoardFacts(**values)


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
            facts = facts_with(service_uart=ServiceUart(0, "IO_PORTB_06", "IO_PORTB_07"))

            configure_service_uart(board, facts)

            content = board.read_text(encoding="utf-8")
            self.assertIn(".port = PORTB_6_7,", content)
            self.assertIn('{"uart0", &uart_dev_ops, (void *)&uart0_data },', content)

    def test_a_port_group_the_sdk_does_not_define_is_rejected(self):
        # Same port, valid pin syntax, but no macro: asm/uart.h lists PORTB_6_7
        # and not PORTB_7_8, so synthesising the name from the pins would emit an
        # identifier the vendor header never defines.
        with tempfile.TemporaryDirectory() as temp_dir:
            board = self._board(temp_dir)
            facts = facts_with(service_uart=ServiceUart(0, "IO_PORTB_07", "IO_PORTB_08"))

            with self.assertRaisesRegex(BuildError, "PORTB_7_8.*does not define"):
                configure_service_uart(board, facts)

    def test_pins_on_different_ports_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            board = self._board(temp_dir)
            facts = facts_with(service_uart=ServiceUart(0, "IO_PORTA_05", "IO_PORTB_06"))

            with self.assertRaisesRegex(BuildError, "same-port"):
                configure_service_uart(board, facts)

    @unittest.skipUnless(
        (PLATFORM_ROOT / "chip/wl82/AC79_AIoT_SDK/include_lib/driver/cpu/wl82/asm/uart.h").is_file(),
        "the wl82 SDK checkout is not present",
    )
    def test_the_port_group_table_matches_the_sdk_header(self):
        # UART_PORT_GROUPS is a transcription of the vendor header. When the SDK
        # is on disk, compare against it, so a vendor update that adds or removes
        # a group fails here rather than inside SDK code at compile time.
        header = (
            PLATFORM_ROOT / "chip/wl82/AC79_AIoT_SDK/include_lib/driver/cpu/wl82/asm/uart.h"
        ).read_text(encoding="utf-8", errors="replace")

        self.assertEqual(
            sorted(set(re.findall(r"\bPORT[A-Z]_\d+_\d+\b", header))),
            sorted(UART_PORT_GROUPS),
        )


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


class LowPowerTest(unittest.TestCase):
    """The sleep macro is rewritten, not appended to.

    Both vendor profiles already define it, so a second #define would warn on
    redefinition and leave the earlier value in force for everything above it -
    the build would look configured and sleep would stay closed.
    """

    WL82_APP_CONFIG = (
        "#ifndef APP_CONFIG_H\n#define APP_CONFIG_H\n"
        "#define TCFG_LOWPOWER_BTOSC_DISABLE\t\t\t0\n"
        "#define TCFG_LOWPOWER_LOWPOWER_SEL\t\t\t0//(RF_SLEEP_EN|RF_FORCE_SYS_SLEEP_EN|SYS_SLEEP_EN)\n"
        "#define TCFG_LOWPOWER_VDDIOM_LEVEL\t\t\tVDDIOM_VOL_32V\n"
        "#endif\n"
    )
    WL83_BOARD_HEADER = (
        "#ifndef BOARD_DEMO_H\n#define BOARD_DEMO_H\n"
        "#define TCFG_LOWPOWER_FUNCTION              LOWPOWER_CLOSE\n"
        "#define TCFG_LOWPOWER_OSC_TYPE              OSC_TYPE_LRC\n"
        "#endif\n"
    )

    # The decision the vendor writes in apps/common/net/wifi_conf.c, reduced to
    # its shape. The check cuts this block out of the file by its markers, so
    # the fixture has to keep the same three-way form and the PSMode= strings.
    #
    # wl82 (wifi_conf.c:519-525, :561-567):
    WL82_WIFI_CONF = (
        "#if defined CONFIG_LOW_POWER_ENABLE\n"
        '#define WL_STA_SLEEP\t2\n'
        "#elif defined CONFIG_WIFI_IDLE_RESUME_BASEBAND_ENABLE\n"
        '#define WL_STA_SLEEP\t1//2\n'
        "#else\n"
        '#define WL_STA_SLEEP\t0\n'
        "#endif\n"
        "static const char WL_STA_DAT[] = {\n"
        '#if WL_STA_SLEEP == 1\n'
        '    "PSMode=Legacy_PSP\\n"\n'
        "#elif WL_STA_SLEEP == 2\n"
        '    "PSMode=Fast_PSP\\n"\n'
        "#else\n"
        '    "PSMode=CAM\\n"\n'
        "#endif\n"
        "};\n"
    )

    # wl83 (wifi_conf.c:565-575, :610-616): the STA mode comes from the same
    # macro as the chip's sleep level, through an integer comparison. The enum
    # is part of the fixture on purpose: the comparison only means what the
    # vendor thinks it means if LOWPOWER_CLOSE is known to the preprocessor,
    # and the real board header chain reaches it through power_api.h.
    WL83_WIFI_CONF = (
        "typedef enum { LOWPOWER_CLOSE, SLEEP_EN, DEEP_SLEEP_EN } lowpower_config_t;\n"
        "#if TCFG_LOWPOWER_FUNCTION != LOWPOWER_CLOSE\n"
        '#define WL_STA_SLEEP\t1\n'
        "#else\n"
        '#define WL_STA_SLEEP\t0\n'
        "#endif\n"
        "static const char WL_STA_DAT[] = {\n"
        '#if WL_STA_SLEEP == 1\n'
        '    "PSMode=Legacy_PSP\\n"\n'
        "#elif WL_STA_SLEEP == 2\n"
        '    "PSMode=Fast_PSP\\n"\n'
        "#else\n"
        '    "PSMode=CAM\\n"\n'
        "#endif\n"
        "};\n"
    )

    def _staged(self, temp_dir, name, text) -> Path:
        path = Path(temp_dir) / name
        path.write_text(text, encoding="utf-8")
        return path

    def _run(self, chip, app_config_text, header_text, wifi_conf_text):
        """Stage the three files and run the hook, inside a temp dir."""
        with tempfile.TemporaryDirectory() as temp_dir:
            app_config = self._staged(temp_dir, "app_config.h", app_config_text)
            header = self._staged(temp_dir, "board_demo.h", header_text)
            wifi_conf = self._staged(temp_dir, "wifi_conf.c", wifi_conf_text)
            configure_lowpower(chip, app_config, header, wifi_conf=wifi_conf,
                               compiler=shutil.which("clang") or shutil.which("gcc"))
            return (
                app_config.read_text(encoding="utf-8"),
                header.read_text(encoding="utf-8"),
            )

    def test_wl82_opens_the_vendor_mask_and_keeps_the_rest_of_the_block(self):
        content, header = self._run(
            "wl82", self.WL82_APP_CONFIG, self.WL83_BOARD_HEADER, self.WL82_WIFI_CONF
        )
        self.assertIn(
            "#define TCFG_LOWPOWER_LOWPOWER_SEL "
            "SYS_SLEEP_EN|SYS_SLEEP_BY_IDLE|RF_SLEEP_EN|RF_FORCE_SYS_SLEEP_EN",
            content,
        )
        self.assertEqual(content.count("TCFG_LOWPOWER_LOWPOWER_SEL"), 1)
        self.assertIn("#define TCFG_LOWPOWER_VDDIOM_LEVEL\t\t\tVDDIOM_VOL_32V", content)
        # The other chip's file is untouched.
        self.assertIn("LOWPOWER_CLOSE", header)

    def test_wl82_mask_is_the_value_the_vendor_boards_ship(self):
        # SYS_SLEEP_EN|SYS_SLEEP_BY_IDLE|RF_SLEEP_EN|RF_FORCE_SYS_SLEEP_EN is
        # what apps/demo/demo_ble and demo_edr enable behind
        # CONFIG_LOW_POWER_ENABLE: BIT(1)|BIT(2)|BIT(3)|BIT(4) == 30.
        self.assertEqual(
            WL82_LOWPOWER_SEL,
            "SYS_SLEEP_EN|SYS_SLEEP_BY_IDLE|RF_SLEEP_EN|RF_FORCE_SYS_SLEEP_EN",
        )

    def test_wl82_turns_on_station_power_save(self):
        content, _ = self._run(
            "wl82", self.WL82_APP_CONFIG, self.WL83_BOARD_HEADER, self.WL82_WIFI_CONF
        )
        # Without this the station compiles to PSMode=CAM: the sleep mask would
        # only apply to moments the radio is idle anyway.
        self.assertIn(f"#define {WL82_STA_PS_MACRO}", content)
        self.assertEqual(content.count(f"#define {WL82_STA_PS_MACRO}"), 1)

    def test_wl83_selects_plain_sleep_as_a_literal(self):
        _, content = self._run(
            "wl83", self.WL82_APP_CONFIG, self.WL83_BOARD_HEADER, self.WL83_WIFI_CONF
        )
        self.assertIn("#define TCFG_LOWPOWER_FUNCTION 1 /* SLEEP_EN */", content)
        self.assertNotIn("LOWPOWER_CLOSE", content)
        self.assertEqual(content.count("TCFG_LOWPOWER_FUNCTION"), 1)
        self.assertIn("#define TCFG_LOWPOWER_OSC_TYPE              OSC_TYPE_LRC", content)

    def test_wl83_value_must_be_a_literal_not_the_enum_name(self):
        # apps/common/net/wifi_conf.c gates config_wifi_lowpower_enable and
        # WL_STA_SLEEP on "#if TCFG_LOWPOWER_FUNCTION != LOWPOWER_CLOSE". Both
        # names are enum constants, so in the preprocessor both sides read as
        # 0 and the branches stay compiled out - the literal is what reaches
        # them.
        self.assertTrue(WL83_LOWPOWER_FUNCTION.startswith("1 "))
        self.assertNotIn("SLEEP_EN|", WL83_LOWPOWER_FUNCTION.replace("/* SLEEP_EN */", ""))

    def test_a_config_that_still_compiles_to_cam_is_rejected(self):
        # The enum-name spelling is exactly the failure the real SDK has, so it
        # must fail the check rather than ship. Written by hand because the
        # hook itself never produces it.
        with tempfile.TemporaryDirectory() as temp_dir:
            header = self._staged(
                temp_dir, "board_demo.h",
                self.WL83_BOARD_HEADER.replace("LOWPOWER_CLOSE\n#define TCFG_LOWPOWER_OSC",
                                               "SLEEP_EN\n#define TCFG_LOWPOWER_OSC"),
            )
            wifi_conf = self._staged(temp_dir, "wifi_conf.c", self.WL83_WIFI_CONF)

            with self.assertRaisesRegex(BuildError, "PSMode=CAM.*Legacy_PSP"):
                _check_ps_mode(header, wifi_conf, WL83_STA_PS_VALUE,
                               compiler=shutil.which("clang") or shutil.which("gcc"))

    def test_a_missing_wifi_conf_is_a_build_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_config = self._staged(temp_dir, "app_config.h", self.WL82_APP_CONFIG)
            header = self._staged(temp_dir, "board_demo.h", self.WL83_BOARD_HEADER)

            with self.assertRaisesRegex(BuildError, "wifi_conf.c is required"):
                configure_lowpower("wl82", app_config, header)

    def test_a_missing_macro_is_a_build_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_config = self._staged(temp_dir, "app_config.h", "#ifndef A\n#endif\n")
            header = self._staged(temp_dir, "board_demo.h", self.WL83_BOARD_HEADER)
            wifi_conf = self._staged(temp_dir, "wifi_conf.c", self.WL82_WIFI_CONF)

            with self.assertRaisesRegex(BuildError, "no #define TCFG_LOWPOWER_LOWPOWER_SEL"):
                configure_lowpower("wl82", app_config, header, wifi_conf=wifi_conf)

    def test_an_unknown_chip_is_a_build_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_config = self._staged(temp_dir, "app_config.h", self.WL82_APP_CONFIG)
            header = self._staged(temp_dir, "board_demo.h", self.WL83_BOARD_HEADER)

            with self.assertRaisesRegex(BuildError, "no Jieli low-power profile"):
                configure_lowpower("wl84", app_config, header)


if __name__ == "__main__":
    unittest.main()
