"""JieLi High-Level Flashing Engine.

Coordinates the complete workflow:
1. Device probe (Chip ID, Flash UUID, Capacity, Sector size).
2. Flash erase (Sector 4K / Block 64K / Full chip).
3. Chunked flashing with CRC16 validation per block.
4. On-chip hardware CRC verification.
5. Programming completion notification & chip reset.
"""

from __future__ import annotations

import logging
import math
import struct
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from .crypto import crc16_ccitt
from .protocol import (
    SUBCMD_CHIP_RESET,
    SUBCMD_ERASE_ALL,
    SUBCMD_ERASE_BLOCK,
    SUBCMD_ERASE_SECTOR,
    SUBCMD_FLASH_DONE,
    SUBCMD_GET_CHIP_ID,
    SUBCMD_GET_CRC16,
    SUBCMD_GET_ERASE_UNIT,
    SUBCMD_GET_FLASH_UUID,
    SUBCMD_GET_MAX_PAGE,
    SUBCMD_READ_FLASH,
    SUBCMD_WRITE_FLASH,
    build_cdb,
)
from .transport import JLTransport, TransportError

logger = logging.getLogger("jieli_flasher")

# Default chunk size for writing (aligned with 4KB sector)
DEFAULT_CHUNK_SIZE = 4096


class FlasherError(RuntimeError):
    """Flasher operation exception."""


_last_phase = ""
_last_pct = -1


def print_progress(
    phase: str,
    current: int,
    total: int,
    start_time: float,
    width: int = 40,
) -> None:
    """Render progress compatible with both interactive terminals and TuyaOpen tos.py."""
    global _last_phase, _last_pct
    if total <= 0:
        return

    ratio = min(1.0, current / total)
    percent = int(ratio * 100)

    # Emit TuyaOpen tos.py hook tags
    if phase != _last_phase:
        _last_phase = phase
        _last_pct = -1
        print(f"[phase] {phase}", flush=True)

    if percent != _last_pct:
        _last_pct = percent
        print(f"[progress] {percent}%", flush=True)


class JLFlasher:
    """High-level flasher controller."""

    def __init__(self, transport: JLTransport):
        self.transport = transport
        self.chip_id: str = ""
        self.flash_uuid: bytes = b""
        self.sector_size: int = 4096
        self.block_size: int = 65536
        self.max_page_size: int = 256

    def probe(self) -> dict[str, object]:
        """Probe the connected JieLi chip and flash memory."""
        logger.info("Probing device info...")

        # 1. Query Chip ID (0x83)
        cdb_chip_id = build_cdb(SUBCMD_GET_CHIP_ID)
        try:
            raw_id = self.transport.send_cmd(cdb_chip_id, data_in_len=16)
            self.chip_id = raw_id.split(b"\x00")[0].decode("latin1", errors="replace").strip()
            logger.info(f"Chip ID / Version: {self.chip_id}")
        except Exception as exc:
            logger.warning(f"Could not read Chip ID: {exc}")
            self.chip_id = "Unknown JieLi"

        # 2. Query Flash UUID (0x84)
        cdb_uuid = build_cdb(SUBCMD_GET_FLASH_UUID)
        try:
            raw_uuid = self.transport.send_cmd(cdb_uuid, data_in_len=16)
            self.flash_uuid = raw_uuid[:16]
            uuid_str = self.flash_uuid.hex().upper()
            logger.info(f"Flash UUID: {uuid_str}")
        except Exception as exc:
            logger.debug(f"Flash UUID query: {exc}")

        # 3. Query Max Page Size (0x14)
        try:
            cdb_page = build_cdb(SUBCMD_GET_MAX_PAGE)
            resp = self.transport.send_cmd(cdb_page, data_in_len=4)
            if len(resp) >= 2:
                page_size = struct.unpack(">H", resp[:2])[0]
                if page_size > 0:
                    self.max_page_size = page_size
                    logger.debug(f"Flash Max Page Size: {page_size} bytes")
        except Exception:
            pass

        return {
            "chip_id": self.chip_id,
            "flash_uuid": self.flash_uuid.hex().upper(),
            "sector_size": self.sector_size,
            "max_page_size": self.max_page_size,
        }

    def erase_sector(self, addr: int) -> None:
        """Erase a single 4KB sector."""
        cdb = build_cdb(SUBCMD_ERASE_SECTOR, addr=addr)
        self.transport.send_cmd(cdb, timeout_ms=3000)

    def erase_block(self, addr: int) -> None:
        """Erase a 64KB block."""
        cdb = build_cdb(SUBCMD_ERASE_BLOCK, addr=addr)
        self.transport.send_cmd(cdb, timeout_ms=5000)

    def erase_all(self) -> None:
        """Erase entire flash chip."""
        logger.info("Performing full chip erase (this may take several seconds)...")
        cdb = build_cdb(SUBCMD_ERASE_ALL)
        self.transport.send_cmd(cdb, timeout_ms=30000)
        logger.info("Full chip erase complete.")

    def erase_range(
        self,
        addr: int,
        length: int,
        progress_cb: Optional[Callable[[str, int, int, float], None]] = None,
    ) -> None:
        """Erase flash range from addr to addr + length, optimizing 64K blocks and 4K sectors."""
        start_addr = addr & ~(self.sector_size - 1)
        end_addr = (addr + length + self.sector_size - 1) & ~(self.sector_size - 1)
        total_bytes = end_addr - start_addr

        logger.info(
            f"Erasing flash range 0x{start_addr:08X} - 0x{end_addr:08X} "
            f"({total_bytes / 1024.0:.1f} KB)..."
        )

        curr = start_addr
        start_time = time.time()
        erased = 0

        while curr < end_addr:
            # Check if aligned on 64KB block and has at least 64KB remaining
            if (curr & (self.block_size - 1) == 0) and (curr + self.block_size <= end_addr):
                self.erase_block(curr)
                curr += self.block_size
                erased += self.block_size
            else:
                self.erase_sector(curr)
                curr += self.sector_size
                erased += self.sector_size

            if progress_cb:
                progress_cb("Erasing", erased, total_bytes, start_time)

    def write_flash_chunk(self, addr: int, chunk: bytes, max_retries: int = 3) -> None:
        """Write a single chunk of data with CRC16 verification."""
        chunk_len = len(chunk)
        chunk_crc = crc16_ccitt(chunk)

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                cdb = build_cdb(
                    subcmd=SUBCMD_WRITE_FLASH,
                    addr=addr,
                    length=chunk_len,
                    crc=chunk_crc,
                )
                self.transport.send_cmd(cdb, data_out=chunk, timeout_ms=5000)
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    f"Write chunk error at 0x{addr:08X} (attempt {attempt}/{max_retries}): {exc}"
                )
                time.sleep(0.05 * attempt)

        raise FlasherError(
            f"Failed to write flash chunk at 0x{addr:08X} after {max_retries} attempts: {last_error}"
        )

    def write_flash(
        self,
        start_addr: int,
        data: bytes,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        progress_cb: Optional[Callable[[str, int, int, float], None]] = None,
    ) -> None:
        """Write entire data buffer to flash at start_addr in chunk_size segments."""
        total_len = len(data)
        logger.info(
            f"Writing 0x{total_len:X} bytes ({total_len / 1024.0:.1f} KB) "
            f"to flash starting at 0x{start_addr:08X}..."
        )

        offset = 0
        start_time = time.time()

        while offset < total_len:
            size = min(chunk_size, total_len - offset)
            chunk = data[offset : offset + size]
            addr = start_addr + offset

            self.write_flash_chunk(addr, chunk)
            offset += size

            if progress_cb:
                progress_cb("Writing", offset, total_len, start_time)

    def get_onchip_crc(self, addr: int, length: int) -> int:
        """Query on-chip hardware CRC16 for the flash range."""
        cdb = build_cdb(SUBCMD_GET_CRC16, addr=addr, length=length)
        resp = self.transport.send_cmd(cdb, data_in_len=16, timeout_ms=10000)
        # Parse CRC16 from response (typically first 2 or 4 bytes)
        if len(resp) >= 2:
            return struct.unpack(">H", resp[:2])[0]
        raise FlasherError("Failed to receive CRC16 from chip.")

    def verify_flash(
        self,
        start_addr: int,
        expected_data: bytes,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        use_hardware_crc: bool = True,
        progress_cb: Optional[Callable[[str, int, int, float], None]] = None,
    ) -> bool:
        """Verify written data against flash using hardware CRC or block read."""
        total_len = len(expected_data)
        expected_crc = crc16_ccitt(expected_data)
        logger.info(
            f"Verifying {total_len} bytes at 0x{start_addr:08X} "
            f"(Expected CRC16: 0x{expected_crc:04X})..."
        )

        if use_hardware_crc:
            try:
                chip_crc = self.get_onchip_crc(start_addr, total_len)
                logger.info(f"On-chip hardware CRC16: 0x{chip_crc:04X}")
                if chip_crc == expected_crc:
                    logger.info("Hardware CRC16 verification PASSED.")
                    return True
                logger.warning(
                    f"Hardware CRC mismatch: expected 0x{expected_crc:04X}, got 0x{chip_crc:04X}. "
                    "Falling back to block read verification..."
                )
            except Exception as exc:
                logger.warning(f"Hardware CRC query failed ({exc}); falling back to read verify.")

        # Fallback: Read and compare block-by-block
        offset = 0
        start_time = time.time()
        while offset < total_len:
            size = min(chunk_size, total_len - offset)
            addr = start_addr + offset
            cdb = build_cdb(SUBCMD_READ_FLASH, addr=addr, length=size)
            read_chunk = self.transport.send_cmd(cdb, data_in_len=size, timeout_ms=5000)
            expected_chunk = expected_data[offset : offset + size]

            if read_chunk != expected_chunk:
                raise FlasherError(
                    f"Verification failed at offset 0x{addr:08X}: read mismatch."
                )

            offset += size
            if progress_cb:
                progress_cb("Verifying", offset, total_len, start_time)

        logger.info("Flash readback verification PASSED.")
        return True

    def flash_done(self) -> None:
        """Notify chip that programming is finished."""
        cdb = build_cdb(SUBCMD_FLASH_DONE)
        try:
            self.transport.send_cmd(cdb, timeout_ms=2000)
        except Exception:
            pass

    def reset(self, delay_ms: int = 500) -> None:
        """Send chip reset command with reboot delay."""
        logger.info(f"Resetting device (delay: {delay_ms} ms)...")
        cdb = build_cdb(SUBCMD_CHIP_RESET, addr=delay_ms)
        try:
            self.transport.send_cmd(cdb, timeout_ms=2000)
        except Exception:
            # Device resets immediately and USB/Serial disconnects, which is expected
            pass
        logger.info("Device reset triggered.")

    def flash_firmware(
        self,
        firmware_bytes: bytes,
        start_addr: int = 0,
        verify: bool = True,
        reboot: bool = True,
        reboot_delay: int = 500,
        full_erase: bool = False,
    ) -> None:
        """Execute complete flashing sequence."""
        total_len = len(firmware_bytes)
        if total_len == 0:
            raise FlasherError("Cannot flash empty firmware image.")

        # Step 1: Probe device
        info = self.probe()

        # Step 2: Erase
        if full_erase:
            self.erase_all()
        else:
            self.erase_range(start_addr, total_len, progress_cb=print_progress)

        # Step 3: Write Flash
        self.write_flash(
            start_addr=start_addr,
            data=firmware_bytes,
            progress_cb=print_progress,
        )

        # Step 4: Verify
        if verify:
            self.verify_flash(
                start_addr=start_addr,
                expected_data=firmware_bytes,
                progress_cb=print_progress,
            )

        # Step 5: Finalize & Reset
        self.flash_done()
        if reboot:
            self.reset(delay_ms=reboot_delay)
