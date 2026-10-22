"""JieLi Firmware & Configuration Handling.

Supports:
- Parsing `isd_config.ini` parameters and partition layouts.
- SDFILE file system header construction and parsing.
- UBOOT header construction and CRC validation.
- Unpacking and packing `.ufw` / `.fw` firmware packages.
"""

from __future__ import annotations

import configparser
import io
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .crypto import crc16_ccitt


@dataclass
class SdfileEntry:
    name: str
    addr: int
    length: int
    data_crc: int
    attr: int = 0
    index: int = 0
    data: bytes = b""


@dataclass
class UbootHeader:
    version: int
    data_size: int
    boot_addr: int
    offset: int
    data_crc: int
    head_crc: int


def parse_isd_config_ini(filepath: str | Path) -> dict[str, str | dict]:
    """Parse JieLi isd_config.ini or rule file.

    Handles JieLi specific syntax quirks:
    - Trailing semicolons
    - Comments: //, #, and bracketed inline notes [note]
    - Multiple sections: [EXTRA_CFG_PARAM], [SYS_CFG_PARAM], [RESERVED_CONFIG], etc.
    """
    path = Path(filepath)
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    raw_text = path.read_text(encoding="utf-8", errors="replace")

    # Clean lines
    cleaned_lines = []
    current_section = "EXTRA_CFG_PARAM"
    result: dict[str, dict[str, str]] = {current_section: {}}

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue

        # Skip full-line comments
        if line.startswith(("//", "#", ";")):
            continue

        # Section header
        sec_match = re.match(r"^\[([A-Za-z0-9_]+)\]", line)
        if sec_match:
            current_section = sec_match.group(1)
            if current_section not in result:
                result[current_section] = {}
            continue

        # Strip inline comments
        # Strip // and # comments
        line = re.sub(r"//.*$", "", line)
        line = re.sub(r"#.*$", "", line)
        # Strip [inline comments] unless it's a section
        line = re.sub(r"\[.*?\]", "", line)

        line = line.strip().rstrip(";")
        if "=" in line:
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'\"")
            result[current_section][key] = val

    return result


def parse_uboot_header(uboot_bytes: bytes, verify_crc: bool = True) -> UbootHeader:
    """Parse and validate a 16-byte uboot.boot header."""
    if len(uboot_bytes) < 16:
        raise ValueError(f"Uboot binary too small: {len(uboot_bytes)} bytes")

    version, size, boot_addr, offset, data_crc, head_crc = struct.unpack(
        "<HHIIHH", uboot_bytes[:16]
    )

    # Validate data CRC if requested and data is present
    if verify_crc and len(uboot_bytes) >= 16 + size:
        actual_crc = crc16_ccitt(uboot_bytes[16 : 16 + size])
        if actual_crc != data_crc:
            raise ValueError(
                f"Uboot data CRC mismatch: expected 0x{data_crc:04X}, computed 0x{actual_crc:04X}"
            )

    return UbootHeader(
        version=version,
        data_size=size,
        boot_addr=boot_addr,
        offset=offset,
        data_crc=data_crc,
        head_crc=head_crc,
    )


def build_sdfile_head(entry: SdfileEntry) -> bytes:
    """Build a 32-byte SDFILE_FILE_HEAD.

    Layout:
    u16 head_crc
    u16 data_crc
    u32 addr
    u32 len
    u8  attr
    u8  res
    u16 index
    char name[16]
    """
    raw_name = entry.name.encode("ascii", errors="replace")[:16].ljust(16, b"\x00")
    payload = struct.pack(
        "<HIIBBH16s",
        entry.data_crc & 0xFFFF,
        entry.addr & 0xFFFFFFFF,
        entry.length & 0xFFFFFFFF,
        entry.attr & 0xFF,
        0,
        entry.index & 0xFFFF,
        raw_name,
    )
    head_crc = crc16_ccitt(payload)
    return struct.pack("<H", head_crc) + payload


def build_flash_head_v2(
    flash_size: int,
    pid: str = "JieLi_TuyaOpen",
    vid: str = "0.01",
    fs_version: int = 1,
    align_modulus: int = 16,
) -> bytes:
    """Build a 32-byte SDFILEJL_FLASH_HEAD_V2 struct.

    Layout:
    u16 u16Crc
    u16 u16SizeForBurner
    char vid[4]
    u32 u32FlashSize
    u8  u8FsVersion
    u8  u8BlockAlingnModulus
    u8  u8Res
    u8  u8SpecialOptFlag
    char pid[16]
    """
    vid_b = vid.encode("ascii", errors="replace")[:4].ljust(4, b"\x00")
    pid_b = pid.encode("ascii", errors="replace")[:16].ljust(16, b"\x00")
    payload = struct.pack(
        "<H4sIBBBB16s",
        32,  # size for burner
        vid_b,
        flash_size & 0xFFFFFFFF,
        fs_version & 0xFF,
        align_modulus & 0xFF,
        0,
        0,
        pid_b,
    )
    crc = crc16_ccitt(payload)
    return struct.pack("<H", crc) + payload
