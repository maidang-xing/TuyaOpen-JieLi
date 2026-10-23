"""JieLi Communication Protocol Constants & Packet Definitions."""

import struct

# Vendor SCSI Opcode
OP_VENDOR = 0xFC

# Subcommand definitions for OP_VENDOR (0xFC)
SUBCMD_ERASE_BLOCK       = 0x00  # Erase 64KB block
SUBCMD_ERASE_SECTOR      = 0x01  # Erase 4KB sector
SUBCMD_ERASE_ALL         = 0x02  # Full chip erase
SUBCMD_WRITE_FLASH       = 0x04  # Write data buffer to flash
SUBCMD_READ_FLASH        = 0x05  # Read data buffer from flash
SUBCMD_WRITE_RAM         = 0x06  # Write to RAM
SUBCMD_READ_RAM          = 0x07  # Read from RAM
SUBCMD_CPU_RUN           = 0x08  # Jump to execution address
SUBCMD_CHIP_RESET        = 0x0C  # Reset device (reboot)
SUBCMD_GET_CRC16         = 0x13  # Query hardware CRC16 of flash/data area
SUBCMD_GET_MAX_PAGE      = 0x14  # Query flash maximum page size (usually 256)
SUBCMD_FLASH_DONE        = 0x17  # Signal programming completion
SUBCMD_GET_ERASE_UNIT    = 0x40  # Query erase unit size
SUBCMD_ERASE_PAGE        = 0x42  # Erase 256-byte page
SUBCMD_PUSH_EXT_PROGRAM  = 0x44  # Push external program / loader
SUBCMD_GET_CHIP_ID       = 0x83  # Query chip ID and hardware version
SUBCMD_GET_FLASH_UUID    = 0x84  # Query SPI flash 16-byte UUID
SUBCMD_READ_EFUSE        = 0xA0  # Read eFuse
SUBCMD_WRITE_EFUSE       = 0xA1  # Write eFuse

# Known JieLi USB Vendor IDs
JIELI_USB_VIDS = [
    0x3654,  # Zhuhai JieLi Technology Co., Ltd.
    0x4C4A,  # 'JL' JieLi Bootloader / UBOOT
    0x0483,  # STMicroelectronics (common fallback / maskrom)
    0x485A,  # 'HZ' / JieLi alternate
]

# USB Mass Storage Bulk-Only Transport (BOT) Signatures
CBW_SIGNATURE = 0x43425355  # 'USBC' in little-endian
CSW_SIGNATURE = 0x53425355  # 'USBS' in little-endian

CBW_LENGTH = 31
CSW_LENGTH = 13

# CSW Status codes
CSW_STATUS_PASSED = 0x00
CSW_STATUS_FAILED = 0x01
CSW_STATUS_PHASE_ERROR = 0x02

# Serial Bootloader Sync Tokens
UART_SYNC_CMD = b"TX21"
UART_ACK_BYTE = 0x79
UART_NACK_BYTE = 0x1F


def build_cdb(
    subcmd: int,
    addr: int = 0,
    length: int = 0,
    crc: int = 0,
    extra: int = 0,
) -> bytes:
    """Build a 16-byte JieLi SCSI Command Descriptor Block (CDB).

    Byte Layout:
    - [0]: 0xFC (Vendor Command Opcode)
    - [1]: Subcommand Code
    - [2..5]: 32-bit Big-Endian Flash / RAM Address
    - [6..7]: 16-bit Big-Endian Data Length
    - [8..9]: 16-bit Big-Endian CRC16 of Data Payload (for write)
    - [10..15]: Extra parameters / Padding (0)
    """
    cdb = bytearray(16)
    cdb[0] = OP_VENDOR
    cdb[1] = subcmd & 0xFF
    cdb[2:6] = struct.pack(">I", addr & 0xFFFFFFFF)
    cdb[6:8] = struct.pack(">H", length & 0xFFFF)
    cdb[8:10] = struct.pack(">H", crc & 0xFFFF)
    if extra:
        cdb[10:14] = struct.pack(">I", extra & 0xFFFFFFFF)
    return bytes(cdb)


def build_cbw(
    tag: int,
    transfer_length: int,
    flags: int,
    cdb: bytes,
    lun: int = 0,
) -> bytes:
    """Build a standard 31-byte USB Mass Storage Command Block Wrapper (CBW).

    Flags:
    - 0x00: Host-to-Device (Data-Out)
    - 0x80: Device-to-Host (Data-In)
    """
    assert len(cdb) <= 16
    cdb_padded = cdb.ljust(16, b"\x00")
    return struct.pack(
        "<IIIBBB16s",
        CBW_SIGNATURE,
        tag & 0xFFFFFFFF,
        transfer_length & 0xFFFFFFFF,
        flags & 0xFF,
        lun & 0x0F,
        len(cdb),
        cdb_padded,
    )


def parse_csw(raw_csw: bytes) -> tuple[int, int, int]:
    """Parse a 13-byte USB Mass Storage Command Status Wrapper (CSW).

    Returns:
    (tag, data_residue, status)
    """
    if len(raw_csw) < CSW_LENGTH:
        raise ValueError(f"CSW packet too short: {len(raw_csw)} bytes (expected {CSW_LENGTH})")

    sig, tag, residue, status = struct.unpack("<IIIB", raw_csw[:CSW_LENGTH])
    if sig != CSW_SIGNATURE:
        raise ValueError(f"Invalid CSW signature: 0x{sig:08X} (expected 0x{CSW_SIGNATURE:08X})")

    return tag, residue, status
