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

from .crypto import crc16_ccitt, jl_crc_cipher, jl_sfc_cipher
from .protocol import (
    CMD_ERASE_FLASH_BLOCK,
    CMD_ERASE_FLASH_CHIP,
    CMD_ERASE_FLASH_SECTOR,
    CMD_JUMP_TO_MEMORY,
    CMD_READ_FLASH,
    CMD_RUN_APP,
    CMD_WRITE_FLASH,
    CMD_WRITE_MEMORY,
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

        # 1. Query Flash ID (0xFC0B)
        try:
            cdb_id = build_cdb(0xFC0B, pad=0xFF)
            raw_id = self.transport.send_cmd(cdb_id, data_in_len=16, timeout_ms=1000)
            if len(raw_id) >= 3:
                flash_id = int.from_bytes(raw_id[:3], "big")
                if flash_id != 0:
                    self.chip_id = f"JieLi Flash (ID: 0x{flash_id:06X})"
                    logger.info(f"Flash Chip ID: 0x{flash_id:06X}")
        except Exception:
            pass

        if not self.chip_id:
            # Fallback query Chip ID (0x83)
            cdb_chip_id = build_cdb(SUBCMD_GET_CHIP_ID)
            try:
                raw_id = self.transport.send_cmd(cdb_chip_id, data_in_len=16, timeout_ms=1000)
                self.chip_id = raw_id.split(b"\x00")[0].decode("latin1", errors="replace").strip()
                logger.info(f"Chip ID / Version: {self.chip_id}")
            except Exception as exc:
                logger.debug(f"Could not read Chip ID: {exc}")
                self.chip_id = "JieLi Device"

        # 2. Query Flash UUID (0x84)
        cdb_uuid = build_cdb(SUBCMD_GET_FLASH_UUID)
        try:
            raw_uuid = self.transport.send_cmd(cdb_uuid, data_in_len=16, timeout_ms=1000)
            self.flash_uuid = raw_uuid[:16]
            uuid_str = self.flash_uuid.hex().upper()
            logger.info(f"Flash UUID: {uuid_str}")
        except Exception as exc:
            logger.debug(f"Flash UUID query: {exc}")

        # 3. Query Max Page Size (0x14)
        try:
            cdb_page = build_cdb(SUBCMD_GET_MAX_PAGE)
            resp = self.transport.send_cmd(cdb_page, data_in_len=4, timeout_ms=1000)
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
        try:
            cdb = build_cdb(SUBCMD_ERASE_SECTOR, addr=addr)
            self.transport.send_cmd(cdb, timeout_ms=3000)
        except Exception:
            cdb_fb = build_cdb(CMD_ERASE_FLASH_SECTOR, addr=addr, pad=0xFF)
            self.transport.send_cmd(cdb_fb, timeout_ms=3000)

    def erase_block(self, addr: int) -> None:
        """Erase a 64KB block."""
        try:
            cdb = build_cdb(SUBCMD_ERASE_BLOCK, addr=addr)
            self.transport.send_cmd(cdb, timeout_ms=5000)
        except Exception:
            cdb_fb = build_cdb(CMD_ERASE_FLASH_BLOCK, addr=addr, pad=0xFF)
            self.transport.send_cmd(cdb_fb, timeout_ms=5000)

    def erase_all(self) -> None:
        """Erase entire flash chip."""
        logger.info("Performing full chip erase (this may take several seconds)...")
        try:
            cdb = build_cdb(SUBCMD_ERASE_ALL)
            self.transport.send_cmd(cdb, timeout_ms=30000)
        except Exception:
            cdb_fb = build_cdb(CMD_ERASE_FLASH_CHIP, pad=0xFF)
            self.transport.send_cmd(cdb_fb, timeout_ms=30000)
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
                try:
                    cdb = build_cdb(
                        SUBCMD_WRITE_FLASH,
                        addr=addr,
                        length=chunk_len,
                        crc=chunk_crc,
                    )
                    self.transport.send_cmd(cdb, data_out=chunk, timeout_ms=5000)
                    return
                except Exception:
                    cdb_fb = build_cdb(
                        CMD_WRITE_FLASH,
                        addr=addr,
                        length=chunk_len,
                        crc=chunk_crc,
                        pad=0xFF,
                    )
                    self.transport.send_cmd(cdb_fb, data_out=chunk, timeout_ms=5000)
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

    def read_flash(self, addr: int, length: int) -> bytes:
        """Read data from flash memory."""
        data = bytearray()
        offset = 0
        while offset < length:
            chunk_len = min(4096, length - offset)
            curr_addr = addr + offset
            try:
                cdb = build_cdb(CMD_READ_FLASH, addr=curr_addr, length=chunk_len, pad=0xFF)
                chunk = self.transport.send_cmd(cdb, data_in_len=chunk_len, timeout_ms=5000)
            except Exception:
                cdb_fb = build_cdb(SUBCMD_READ_FLASH, addr=curr_addr, length=chunk_len)
                chunk = self.transport.send_cmd(cdb_fb, data_in_len=chunk_len, timeout_ms=5000)
            data.extend(chunk)
            offset += chunk_len
        return bytes(data)

    def get_onchip_crc(self, addr: int, length: int) -> int:
        """Query on-chip hardware CRC16 for the flash range."""
        cdb = build_cdb(SUBCMD_GET_CRC16, addr=addr, length=length)
        resp = self.transport.send_cmd(cdb, data_in_len=16, timeout_ms=10000)
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
                logger.debug(f"Hardware CRC query note ({exc}); falling back to read verify.")

        # Fallback: Read and compare block-by-block
        readback = self.read_flash(start_addr, total_len)
        if readback != expected_data:
            # Locate first mismatch for clear diagnostic
            for i in range(min(len(readback), len(expected_data))):
                if readback[i] != expected_data[i]:
                    raise FlasherError(
                        f"Flash verification mismatch at byte offset 0x{start_addr + i:08X}: "
                        f"expected 0x{expected_data[i]:02X}, read 0x{readback[i]:02X}"
                    )
            raise FlasherError(
                f"Flash verification length mismatch: read {len(readback)} bytes vs expected {len(expected_data)}"
            )

        if progress_cb:
            progress_cb("Verifying", total_len, total_len, time.time())
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
        try:
            cdb_sub = build_cdb(SUBCMD_CHIP_RESET, addr=delay_ms)
            self.transport.send_cmd(cdb_sub, timeout_ms=2000)
        except Exception:
            try:
                cdb = build_cdb(CMD_RUN_APP, addr=1, pad=0xFF)
                self.transport.send_cmd(cdb, timeout_ms=2000)
            except Exception:
                pass
        logger.info("Device reset triggered.")

    def _locate_loader(self, loader_name: str) -> Optional[Path]:
        """Locate RAM loader binary."""
        here = Path(__file__).resolve().parent / "loaders" / loader_name
        if here.is_file():
            return here

        sdk_dirs = [
            Path(__file__).resolve().parents[1] / "chip" / "wl82" / "AC79_AIoT_SDK" / "cpu" / "wl82" / "tools" / loader_name,
            Path(__file__).resolve().parents[1] / "chip" / "wl83" / "AC792_SDK" / "sdk" / "cpu" / "wl83" / "tools" / loader_name,
            Path(__file__).resolve().parents[1] / "chip" / "wl82" / "AC79_AIoT_SDK" / "cpu" / "wl82" / "tools" / "loader_tools" / loader_name,
        ]
        for candidate in sdk_dirs:
            if candidate.is_file():
                return candidate
        return None

    def stage_ram_loader(self, chip_name: str = "wl82", boot_addr: int = 0) -> None:
        """Upload and execute RAM loader in chip memory."""
        chip = chip_name.lower()
        if "83" in chip or "792" in chip:
            loader_name = "wl83loader.bin"
            default_addr = 0x103000
            mengli = False
        else:
            loader_name = "wl82loader.bin"
            default_addr = 0x1C02000
            mengli = True

        target_addr = boot_addr if boot_addr > 0 else default_addr
        loader_path = self._locate_loader(loader_name)
        if not loader_path or not loader_path.is_file():
            raise FlasherError(f"Cannot find RAM loader binary: {loader_name}")

        loader_bytes = loader_path.read_bytes()
        logger.info(f"Uploading {loader_name} ({len(loader_bytes)} bytes) to RAM 0x{target_addr:08X}...")

        block_size = 512
        offset = 0
        while offset < len(loader_bytes):
            block = bytearray(loader_bytes[offset : offset + block_size])
            if mengli:
                jl_crc_cipher(block, 0, len(block))

            addr = target_addr + offset
            block_crc = crc16_ccitt(block)
            cdb = build_cdb(CMD_WRITE_MEMORY, addr=addr, length=len(block), crc=block_crc, pad=0xFF)
            self.transport.send_cmd(cdb, data_out=bytes(block), timeout_ms=3000)
            offset += len(block)

        logger.info(f"Executing RAM loader at 0x{target_addr:08X}...")
        cdb_jump = build_cdb(CMD_JUMP_TO_MEMORY, addr=target_addr, length=1, pad=0xFF)
        try:
            self.transport.send_cmd(cdb_jump, data_in_len=16, timeout_ms=3000)
        except Exception:
            pass

        time.sleep(0.1)
        logger.info("RAM loader execution started.")

    def prepare_device(self, chip_name: str = "wl82", boot_addr: int = 0) -> None:
        """Probe device and ensure RAM loader is running if target is in Mask ROM mode."""
        inq_product = ""
        inq_vendor = ""
        if hasattr(self.transport, "inquiry"):
            try:
                inq_vendor, inq_product, _ = self.transport.inquiry()
            except Exception:
                pass

        ident = f"{inq_vendor} {inq_product}".upper()
        if "UBOOT" in ident or not inq_product:
            # Auto-detect whether target is WL82/AC791 or WL83/AC792
            effective_chip = chip_name.lower()
            if "83" in ident or "792" in ident:
                effective_chip = "wl83"
            elif "80" in ident or "82" in ident or "791" in ident:
                effective_chip = "wl82"

            logger.info(
                f"Target is in Mask ROM mode ({inq_vendor} {inq_product}). Staging RAM loader for {effective_chip}..."
            )
            self.stage_ram_loader(chip_name=effective_chip, boot_addr=boot_addr)

    def flash_app(
        self,
        app_bin: bytes,
        cfg_tool_bin: Optional[bytes] = None,
        progress_cb: Optional[Callable[[str, int, int, float], None]] = None,
    ) -> None:
        """Flash application binary and configuration into the JieLi SFC filesystem."""
        logger.info("Reading flash filesystem layout at 0x4000...")
        flash_hdr = bytearray(self.read_flash(0x4000, 4096))
        jl_sfc_cipher(flash_hdr, 0, len(flash_hdr), base=0, key=0xFFFF)

        # Check if flash at 0x4000 is blank (freshly erased or uninitialized)
        if flash_hdr[:16] == b"\xff" * 16 or flash_hdr[:4] == b"\x00\x00\x00\x00":
            logger.info("Flash layout at 0x4000 is uninitialized; creating clean app_area_head template...")
            flash_hdr = bytearray(b"\xff" * 4096)
            area_dcrc = 0x0000
        else:
            area_dcrc = struct.unpack_from("<H", flash_hdr, 0x02)[0]

        app_len = len(app_bin)
        app_offset = 0x120
        app_dcrc = crc16_ccitt(app_bin)

        cfg_len = len(cfg_tool_bin) if cfg_tool_bin else 0
        cfg_offset = app_offset + app_len

        # Update Entry 1 (app.bin) at 0x20
        app_name = b"app.bin\x00" + b"\xff" * 8
        app_hdr_body = struct.pack("<HIIBBH16s", app_dcrc, app_offset, app_len, 0x82, 0xFF, 0, app_name)
        app_hcrc = crc16_ccitt(app_hdr_body)
        flash_hdr[0x20:0x40] = struct.pack("<H30s", app_hcrc, app_hdr_body)

        # Update Entry 2 (cfg_tool.bin) at 0x40
        if cfg_tool_bin:
            cfg_dcrc = crc16_ccitt(cfg_tool_bin)
            cfg_name = b"cfg_tool.bin\x00" + b"\xff" * 3
            cfg_hdr_body = struct.pack("<HIIBBH16s", cfg_dcrc, cfg_offset, cfg_len, 0x82, 0xFF, 0, cfg_name)
            cfg_hcrc = crc16_ccitt(cfg_hdr_body)
            flash_hdr[0x40:0x60] = struct.pack("<H30s", cfg_hcrc, cfg_hdr_body)

        # Update Entry 0 (app_area_head) at 0x00
        area_total_len = cfg_offset + cfg_len
        area_dcrc = struct.unpack_from("<H", flash_hdr, 0x02)[0]
        area_name = b"app_area_head\x00\xff\xff"
        area_hdr_body = struct.pack("<HIIBBH16s", area_dcrc, 0x02000120, area_total_len, 0x83, 0xFF, 0, area_name)
        area_hcrc = crc16_ccitt(area_hdr_body)
        flash_hdr[0x00:0x20] = struct.pack("<H30s", area_hcrc, area_hdr_body)

        payload = bytearray(0x120 + area_total_len)
        payload[:0x120] = flash_hdr[:0x120]
        payload[app_offset : app_offset + app_len] = app_bin
        if cfg_tool_bin:
            payload[cfg_offset : cfg_offset + cfg_len] = cfg_tool_bin

        aligned_len = ((len(payload) + 4095) // 4096) * 4096
        payload += b"\xff" * (aligned_len - len(payload))

        logger.info(f"Encrypting {aligned_len} bytes with JieLi SFC cipher...")
        jl_sfc_cipher(payload, 0, len(payload), base=0, key=0xFFFF)

        self.erase_range(0x4000, aligned_len, progress_cb=progress_cb)
        self.write_flash(0x4000, bytes(payload), progress_cb=progress_cb)
        self.verify_flash(0x4000, bytes(payload), progress_cb=progress_cb)

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

        # Step 1: Erase
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
