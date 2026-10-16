"""Read the hardware facts a Jieli board declares in its Kconfig.

The staging hooks apply board-specific values to the vendor SDK: the USB device
macro, the memory map, the log and service UARTs and the panel. Those values
used to be per-board branches inside this package, and briefly a JSON file
beside each board. They are now ordinary Kconfig settings under
``boards/JIELI/<board>/Kconfig``, so the platform reads them from the evaluated
config the way it reads every other setting, and a new board is data rather
than a platform edit.

Values that would name a vendor macro stay semantic in Kconfig -- ``usb_role``
is ``host`` or ``device``, the panel is its model -- and the mapping to the
vendor's vocabulary lives here, in the platform. Pin names are the SDK's own
vocabulary and the board writes them as the vendor headers spell them.

Nothing falls back to a default. A fact the platform acts on is a fact the
board has to state: a missing setting is a build error naming the symbol, never
a silently inherited value. A default would only move the hardcoding.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Mapping, Optional

from .errors import BuildError

# The namespace a board declares its facts in. Both boards declare the same
# names, so the platform reads one set whichever is selected; Kconfig resolves
# each value from the node the board choice makes visible, and a setting the
# selected board does not declare simply is not there.
# The Kconfig spelling, and the one the evaluated config carries.
KCONFIG_PREFIX = "JIELI_BOARD_"
SYMBOL_PREFIX = "CONFIG_" + KCONFIG_PREFIX

# What the board's semantic usb_role maps onto.
USB_DEVICE_MACROS = {
    "host": "TCFG_HUSB_DEVICE",
    "device": "TCFG_FUSB_DEVICE",
}

# Panel model -> (the vendor macro that selects its driver, the device name the
# vendor's LCD registration matches on). A board names the model; which macro
# and which driver that means is the SDK's business, not the board's.
PANELS = {
    "MIPI_480x800_ST7701S": ("TCFG_LCD_MIPI_ST7701S_480x800", "MIPI_480x800_ST7701S"),
}


@dataclass(frozen=True)
class Memory:
    """The board's memory map: on-chip flash and external SDRAM, in MiB."""

    flash_mib: int
    sdram_mib: int
    # True only when the board actually populates the external DDR. The vendor
    # SDK otherwise disables it through CONFIG_NO_SDRAM_ENABLE.
    external_ddr: bool


@dataclass(frozen=True)
class LogUart:
    """The SDK UART the board logs on.

    ``tx_pin`` is optional: a board that keeps the vendor's default pin leaves
    it empty, and the staging hook then leaves the vendor pin alone.
    """

    port: int
    tx_pin: Optional[str]


@dataclass(frozen=True)
class ServiceUart:
    """The second UART the TAL CLI is routed to."""

    port: int
    tx_pin: str
    rx_pin: str


@dataclass(frozen=True)
class Display:
    """The MIPI-DSI panel the board carries."""

    panel_macro: str
    device_name: str
    reset_io: str
    backlight_io: str


@dataclass(frozen=True)
class BoardFacts:
    """Every board-level fact the staging hooks act on.

    ``service_uart`` and ``display`` are None for a board that has neither.
    """

    board: str
    usb_device: str
    memory: Memory
    log_uart: LogUart
    service_uart: Optional[ServiceUart]
    display: Optional[Display]


def board_facts_digest(facts: BoardFacts) -> str:
    """Render the facts so a staging tree can record which ones it was built from.

    The staging inputs are named components and this is the one that covers the
    board's facts. It is a digest of the resolved facts rather than of a file,
    because there is no longer a file: the values come from the evaluated
    config, and what staging acts on is the result.
    """
    rendered = json.dumps(asdict(facts), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _symbol(name: str) -> str:
    return SYMBOL_PREFIX + name


def _text(params: Mapping[str, str], name: str) -> str:
    return params.get(_symbol(name), "").strip()


def _require(params: Mapping[str, str], name: str) -> str:
    value = _text(params, name)
    if not value:
        raise BuildError(
            f"the selected board does not declare {_symbol(name)}; add it to "
            f"boards/JIELI/<board>/Kconfig"
        )
    return value


def _require_int(params: Mapping[str, str], name: str) -> int:
    raw = _require(params, name)
    try:
        return int(raw)
    except ValueError as exc:
        raise BuildError(f"{_symbol(name)} is not an integer: {raw!r}") from exc


def _flag(params: Mapping[str, str], name: str) -> bool:
    """A Kconfig bool. Unset means the symbol is absent, which is false."""
    return params.get(_symbol(name), "").strip() in ("y", "true", "True")


def _parse_memory(params: Mapping[str, str]) -> Memory:
    flash = _require_int(params, "FLASH_MIB")
    sdram = _require_int(params, "SDRAM_MIB")
    for name, value in (("FLASH_MIB", flash), ("SDRAM_MIB", sdram)):
        if value <= 0:
            raise BuildError(f"{_symbol(name)} must be positive, found {value}")
    return Memory(
        flash_mib=flash,
        sdram_mib=sdram,
        external_ddr=_flag(params, "EXTERNAL_DDR"),
    )


def _parse_log_uart(params: Mapping[str, str]) -> LogUart:
    port = _require_int(params, "LOG_UART_PORT")
    if port < 0:
        raise BuildError(f"{_symbol('LOG_UART_PORT')} must not be negative, found {port}")
    return LogUart(port=port, tx_pin=_text(params, "LOG_UART_TX_PIN") or None)


def _parse_service_uart(params: Mapping[str, str]) -> Optional[ServiceUart]:
    if not _flag(params, "HAS_SERVICE_UART"):
        return None
    port = _require_int(params, "SERVICE_UART_PORT")
    if port < 0:
        raise BuildError(
            f"{_symbol('SERVICE_UART_PORT')} must not be negative, found {port}"
        )
    return ServiceUart(
        port=port,
        tx_pin=_require(params, "SERVICE_UART_TX_PIN"),
        rx_pin=_require(params, "SERVICE_UART_RX_PIN"),
    )


def _parse_display(params: Mapping[str, str]) -> Optional[Display]:
    if not _flag(params, "HAS_DISPLAY"):
        return None
    model = _require(params, "DISPLAY_PANEL")
    known = PANELS.get(model)
    if known is None:
        raise BuildError(
            f"{_symbol('DISPLAY_PANEL')} names panel {model!r}, which this "
            f"platform has no vendor macro for. It knows: {', '.join(sorted(PANELS))}"
        )
    panel_macro, device_name = known
    return Display(
        panel_macro=panel_macro,
        device_name=device_name,
        reset_io=_require(params, "DISPLAY_RESET_IO"),
        backlight_io=_require(params, "DISPLAY_BACKLIGHT_IO"),
    )


def _parse_usb_device(params: Mapping[str, str]) -> str:
    role = _require(params, "USB_ROLE")
    macro = USB_DEVICE_MACROS.get(role)
    if macro is None:
        raise BuildError(
            f"{_symbol('USB_ROLE')} is {role!r}; expected one of "
            f"{', '.join(sorted(USB_DEVICE_MACROS))}"
        )
    return macro


def load_board_facts(params: Mapping[str, str]) -> BoardFacts:
    """Read the selected board's facts out of the evaluated config.

    ``params`` is the build's config mapping -- the same one the build plan and
    the staging hooks already read. The board name comes from
    CONFIG_BOARD_CHOICE, which the platform has already confirmed against its
    Kconfig choice flag.
    """
    board = params.get("CONFIG_BOARD_CHOICE", "").strip()
    if not board:
        raise BuildError("CONFIG_BOARD_CHOICE is missing, so no board facts apply")
    return BoardFacts(
        board=board,
        usb_device=_parse_usb_device(params),
        memory=_parse_memory(params),
        log_uart=_parse_log_uart(params),
        service_uart=_parse_service_uart(params),
        display=_parse_display(params),
    )
