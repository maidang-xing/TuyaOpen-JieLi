"""JieLi Cross-Platform Flasher Package."""

__version__ = "1.0.0"

from .crypto import cd03_cipher, crc8_maxim, crc16_ccitt, scsi_xor_cipher
from .firmware import parse_isd_config_ini, parse_uboot_header
from .flasher import JLFlasher
from .protocol import (
    SUBCMD_CHIP_RESET,
    SUBCMD_ERASE_ALL,
    SUBCMD_ERASE_BLOCK,
    SUBCMD_ERASE_SECTOR,
    SUBCMD_FLASH_DONE,
    SUBCMD_GET_CHIP_ID,
    SUBCMD_GET_CRC16,
    SUBCMD_GET_FLASH_UUID,
    SUBCMD_READ_FLASH,
    SUBCMD_WRITE_FLASH,
)
from .transport import JLSerialTransport, JLTransport, JLUsbScsiTransport
