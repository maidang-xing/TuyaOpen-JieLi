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

# JieLi 16-bit SCSI Command Opcodes
CMD_ERASE_FLASH_BLOCK   = 0xFB00  # Erase 64KB flash block
CMD_ERASE_FLASH_SECTOR  = 0xFB01  # Erase 4KB flash sector
CMD_ERASE_FLASH_CHIP    = 0xFB02  # Erase entire flash chip
CMD_READ_STATUS         = 0xFC03  # Read loader status
CMD_WRITE_FLASH         = 0xFB04  # Write data buffer to flash
CMD_READ_FLASH          = 0xFD05  # Read data buffer from flash
CMD_WRITE_MEMORY        = 0xFB06  # Write to memory/RAM
CMD_READ_MEMORY         = 0xFD07  # Read from memory/RAM
CMD_JUMP_TO_MEMORY      = 0xFB08  # Jump to execution address
CMD_READ_KEY            = 0xFC09  # Read chip key
CMD_GET_ONLINE_DEVICE   = 0xFC0A  # Query online device type
CMD_READ_ID             = 0xFC0B  # Read device / flash ID
CMD_RUN_APP             = 0xFC0C  # Run application / chip reset
CMD_SET_FLASH_CMD       = 0xFC0D  # Set flash SPI commands
CMD_FLASH_CRC16         = 0xFC0E  # Query on-chip CRC16
CMD_WRITE_KEY           = 0xFC12  # Write chip key
CMD_FLASH_CRC16_RAW     = 0xFC13  # Query raw on-chip CRC16
CMD_GET_USB_BUFF_SIZE   = 0xFC14  # Query USB buffer size
CMD_GET_LOADER_VER      = 0xFC15  # Query loader version
CMD_GET_MASKROM_ID      = 0xFC16  # Query Mask ROM ID

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
    pad: int = 0x00,
) -> bytes:
    """Build a 16-byte JieLi SCSI Command Descriptor Block (CDB).

    Byte Layout:
    - [0..1]: Opcode / Subcommand Code (either 1-byte OP_VENDOR + 1-byte subcmd, or 2-byte command)
    - [2..5]: 32-bit Big-Endian Flash / RAM Address
    - [6..7]: 16-bit Big-Endian Data Length
    - [8..9]: 16-bit Big-Endian CRC16 of Data Payload (for write)
    - [10..15]: Extra parameters / Padding
    """
    cdb = bytearray(16)
    if subcmd > 0xFF:
        cdb[0] = (subcmd >> 8) & 0xFF
        cdb[1] = subcmd & 0xFF
    else:
        cdb[0] = OP_VENDOR
        cdb[1] = subcmd & 0xFF

    cdb[2:6] = struct.pack(">I", addr & 0xFFFFFFFF)
    cdb[6:8] = struct.pack(">H", length & 0xFFFF)
    cdb[8:10] = struct.pack(">H", crc & 0xFFFF)
    if extra:
        cdb[10:14] = struct.pack(">I", extra & 0xFFFFFFFF)
    elif pad != 0:
        for p in range(10, 16):
            cdb[p] = pad & 0xFF
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
