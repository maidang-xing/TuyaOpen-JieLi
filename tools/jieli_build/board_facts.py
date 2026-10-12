"""Read the hardware facts a Jieli board declares next to itself.

The staging hooks apply board-specific values to the vendor SDK: the USB device
macro, the memory map, the log and service UARTs and the panel. Those values
used to be per-board branches inside this package. They now live with the board,
in ``boards/JIELI/<board>/jieli_board.json``, so a new board is data rather than
a platform edit.

Nothing here falls back to a default. A fact the platform acts on is a fact the
board has to state: a missing file or field is a build error naming the file and
the field, never a silently inherited value. A default would only move the
hardcoding, not remove it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .errors import BuildError

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)
BOARD_FACTS_FILENAME = "jieli_board.json"
BOARDS_RELATIVE = Path("boards") / "JIELI"


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

    ``tx_pin`` is optional: a board that keeps the vendor's default pin omits
    it, and the staging hook then leaves the vendor pin alone.
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
    ``source`` is the file the facts were read from, so a later failure can name
    it.
    """

    board: str
    usb_device: str
    memory: Memory
    log_uart: LogUart
    service_uart: Optional[ServiceUart]
    display: Optional[Display]
    source: Path


def board_facts_path(tuyaopen_root: Path, board: str) -> Path:
    """Return where a board's facts live, whether or not the file exists."""
    return Path(tuyaopen_root) / BOARDS_RELATIVE / board / BOARD_FACTS_FILENAME


def _fail(source: Path, message: str) -> BuildError:
    return BuildError(f"{source}: {message}")


def _field(data: Mapping[str, Any], source: Path, name: str) -> Any:
    if name not in data:
        raise _fail(source, f"missing required field {name!r}")
    return data[name]


def _reject_unknown(data: Mapping[str, Any], source: Path, known: set[str]) -> None:
    """A field the platform does not read is a mistake, not a harmless extra.

    Accepting it would suggest a fact is being honoured when it is not.
    """
    unknown = sorted(set(data) - known)
    if unknown:
        raise _fail(
            source,
            "unknown field(s) the platform does not read: "
            + ", ".join(repr(name) for name in unknown),
        )


def _require_str(data: Mapping[str, Any], source: Path, name: str) -> str:
    value = _field(data, source, name)
    if not isinstance(value, str) or not value:
        raise _fail(source, f"field {name!r} must be a non-empty string, found {value!r}")
    return value


def _require_int(data: Mapping[str, Any], source: Path, name: str) -> int:
    value = _field(data, source, name)
    # bool is an int subclass, but a boolean where a size belongs is a mistake.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(source, f"field {name!r} must be an integer, found {value!r}")
    return value


def _require_bool(data: Mapping[str, Any], source: Path, name: str) -> bool:
    value = _field(data, source, name)
    if not isinstance(value, bool):
        raise _fail(source, f"field {name!r} must be true or false, found {value!r}")
    return value


def _require_object(value: Any, source: Path, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(source, f"field {name!r} must be a JSON object, found {value!r}")
    return value


def _parse_memory(data: Mapping[str, Any], source: Path) -> Memory:
    memory = _require_object(_field(data, source, "memory"), source, "memory")
    _reject_unknown(memory, source, {"flash_mib", "sdram_mib", "external_ddr"})
    flash_mib = _require_int(memory, source, "flash_mib")
    sdram_mib = _require_int(memory, source, "sdram_mib")
    if flash_mib <= 0 or sdram_mib <= 0:
        raise _fail(source, f"memory sizes must be positive, found {flash_mib}/{sdram_mib} MiB")
    return Memory(
        flash_mib=flash_mib,
        sdram_mib=sdram_mib,
        external_ddr=_require_bool(memory, source, "external_ddr"),
    )


def _parse_log_uart(data: Mapping[str, Any], source: Path) -> LogUart:
    log_uart = _require_object(_field(data, source, "log_uart"), source, "log_uart")
    _reject_unknown(log_uart, source, {"port", "tx_pin"})
    port = _require_int(log_uart, source, "port")
    if port < 0:
        raise _fail(source, f"log_uart.port must not be negative, found {port}")
    tx_pin = log_uart.get("tx_pin")
    if tx_pin is not None and (not isinstance(tx_pin, str) or not tx_pin):
        raise _fail(source, f"field 'log_uart.tx_pin' must be a non-empty string, found {tx_pin!r}")
    return LogUart(port=port, tx_pin=tx_pin)


def _parse_service_uart(data: Mapping[str, Any], source: Path) -> Optional[ServiceUart]:
    value = _field(data, source, "service_uart")
    if value is None:
        return None
    service = _require_object(value, source, "service_uart")
    _reject_unknown(service, source, {"port", "tx_pin", "rx_pin"})
    port = _require_int(service, source, "port")
    if port < 0:
        raise _fail(source, f"service_uart.port must not be negative, found {port}")
    return ServiceUart(
        port=port,
        tx_pin=_require_str(service, source, "tx_pin"),
        rx_pin=_require_str(service, source, "rx_pin"),
    )


def _parse_display(data: Mapping[str, Any], source: Path) -> Optional[Display]:
    value = _field(data, source, "display")
    if value is None:
        return None
    display = _require_object(value, source, "display")
    _reject_unknown(
        display, source, {"panel_macro", "device_name", "reset_io", "backlight_io"}
    )
    return Display(
        panel_macro=_require_str(display, source, "panel_macro"),
        device_name=_require_str(display, source, "device_name"),
        reset_io=_require_str(display, source, "reset_io"),
        backlight_io=_require_str(display, source, "backlight_io"),
    )


def _parse_facts(data: Mapping[str, Any], source: Path, board: str) -> BoardFacts:
    _reject_unknown(
        data,
        source,
        {
            "schema_version",
            "board",
            "usb_device",
            "memory",
            "log_uart",
            "service_uart",
            "display",
        },
    )
    version = _field(data, source, "schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise _fail(
            source,
            f"unsupported schema_version {version!r}; this platform reads "
            f"{list(SUPPORTED_SCHEMA_VERSIONS)}",
        )
    declared = _require_str(data, source, "board")
    if declared != board:
        raise _fail(
            source,
            f"field 'board' is {declared!r} but the board directory is {board!r}; "
            f"they must match",
        )
    return BoardFacts(
        board=declared,
        usb_device=_require_str(data, source, "usb_device"),
        memory=_parse_memory(data, source),
        log_uart=_parse_log_uart(data, source),
        service_uart=_parse_service_uart(data, source),
        display=_parse_display(data, source),
        source=source,
    )


def load_board_facts(tuyaopen_root: Path, board: Optional[str]) -> BoardFacts:
    """Read and validate one board's facts, or fail explaining what is missing.

    ``board`` is the Kconfig board name, which is also the directory name under
    ``boards/JIELI``. There is deliberately no default: a build that does not
    know its board cannot honour its hardware.
    """
    if not board:
        raise BuildError(
            "no Jieli board was selected, so the staging hooks have no hardware "
            f"facts to apply; the board's facts live in "
            f"boards/JIELI/<board>/{BOARD_FACTS_FILENAME}"
        )
    source = board_facts_path(tuyaopen_root, board)
    if not source.is_file():
        raise BuildError(
            f"Jieli board {board!r} does not declare its hardware facts: "
            f"{source} is missing. Add boards/JIELI/{board}/{BOARD_FACTS_FILENAME} "
            f"(schema_version {SCHEMA_VERSION}); the platform has no default to "
            f"fall back to."
        )
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"{source}: board facts are not readable JSON: {exc}") from exc
    if not isinstance(data, Mapping):
        raise _fail(source, "board facts must be a JSON object")
    return _parse_facts(data, source, board)
