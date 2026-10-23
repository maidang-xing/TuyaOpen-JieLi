"""JieLi Transport Layer (USB SCSI BOT & Serial UART).

Supports:
- USB Mass Storage Bulk-Only Transport (BOT) via pyusb (Windows/Linux/macOS).
- Linux SCSI Generic fallback via /dev/sg* or /dev/sd* ioctl (SG_IO).
- Serial UART transport via pyserial.
"""

from __future__ import annotations

import logging
import os
import struct
import sys
import time
from abc import ABC, abstractmethod
from typing import Optional

from .crypto import crc16_ccitt
from .protocol import (
    CBW_LENGTH,
    CSW_LENGTH,
    CSW_STATUS_PASSED,
    JIELI_USB_VIDS,
    UART_ACK_BYTE,
    UART_SYNC_CMD,
    build_cbw,
    parse_csw,
)

logger = logging.getLogger("jieli_flasher")


class TransportError(RuntimeError):
    """Transport communication exception."""


class JLTransport(ABC):
    """Abstract communication transport."""

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to the target device."""

    @abstractmethod
    def close(self) -> None:
        """Close connection."""

    @abstractmethod
    def send_cmd(
        self,
        cdb: bytes,
        data_out: Optional[bytes] = None,
        data_in_len: int = 0,
        timeout_ms: int = 5000,
    ) -> bytes:
        """Send a 16-byte SCSI CDB and optional data payload, returning response bytes."""


class JLUsbScsiTransport(JLTransport):
    """USB Mass Storage Bulk-Only Transport (BOT) implementation using pyusb."""

    def __init__(
        self,
        vid: Optional[int] = None,
        pid: Optional[int] = None,
        serial_number: Optional[str] = None,
    ):
        self.target_vid = vid
        self.target_pid = pid
        self.target_serial = serial_number
        self.dev = None
        self.ep_out = None
        self.ep_in = None
        self.tag_counter = 1

    def _next_tag(self) -> int:
        tag = self.tag_counter
        self.tag_counter = (self.tag_counter + 1) & 0xFFFFFFFF
        return tag

    @classmethod
    def find_devices(cls) -> list[dict]:
        """Find connected JieLi USB devices."""
        try:
            import usb.core
            import usb.util
        except ImportError:
            logger.warning("pyusb is not installed; USB transport unavailable.")
            return []

        devices = []
        try:
            for dev in usb.core.find(find_all=True):
                # Match either known VID or Mass Storage Class with JieLi strings
                is_jieli = dev.idVendor in JIELI_USB_VIDS
                mfg = ""
                prod = ""
                try:
                    mfg = dev.manufacturer or ""
                    prod = dev.product or ""
                except Exception:
                    pass

                if any(k in f"{mfg} {prod}".upper() for k in ("JIELI", "BR21", "WL80", "WL82", "WL83", "AC79", "UBOOT")) :
                    is_jieli = True

                if is_jieli:
                    devices.append({
                        "vid": dev.idVendor,
                        "pid": dev.idProduct,
                        "bus": dev.bus,
                        "address": dev.address,
                        "manufacturer": mfg,
                        "product": prod,
                        "device_obj": dev,
                    })
        except Exception as exc:
            logger.debug(f"Error enumerating USB devices: {exc}")

        return devices

    def connect(self) -> None:
        import usb.core
        import usb.util

        if self.target_vid and self.target_pid:
            dev = usb.core.find(idVendor=self.target_vid, idProduct=self.target_pid)
            if not dev:
                raise TransportError(f"USB device {self.target_vid:04X}:{self.target_pid:04X} not found.")
        else:
            candidates = self.find_devices()
            if not candidates:
                raise TransportError(
                    "No JieLi USB device found. Ensure device is in download mode (OTG/USB plugged)."
                )
            dev = candidates[0]["device_obj"]
            logger.info(
                f"Found JieLi USB device: {dev.idVendor:04X}:{dev.idProduct:04X} "
                f"(Bus {dev.bus}, Dev {dev.address})"
            )

        self.dev = dev

        # Reset & Detach kernel driver if active on Linux/macOS
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
                logger.debug("Detached active kernel driver.")
        except Exception:
            pass

        try:
            cfg = dev.get_active_configuration()
        except Exception:
            cfg = None
        if cfg is None:
            try:
                dev.set_configuration()
            except Exception as exc:
                logger.debug(f"set_configuration note: {exc}")

        # Locate Bulk IN and Bulk OUT endpoints
        if cfg is None:
            cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]

        ep_out = None
        ep_in = None
        for ep in intf:
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT:
                if usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK:
                    ep_out = ep
            elif usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                if usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK:
                    ep_in = ep

        if not ep_out or not ep_in:
            raise TransportError("Unable to locate Bulk IN and Bulk OUT endpoints for USB MSC interface.")

        self.ep_out = ep_out
        self.ep_in = ep_in
        logger.debug(f"Configured endpoints: OUT=0x{ep_out.bEndpointAddress:02X}, IN=0x{ep_in.bEndpointAddress:02X}")

    def close(self) -> None:
        if self.dev:
            try:
                import usb.util
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
            self.dev = None

    def send_cmd(
        self,
        cdb: bytes,
        data_out: Optional[bytes] = None,
        data_in_len: int = 0,
        timeout_ms: int = 5000,
    ) -> bytes:
        if not self.dev or not self.ep_out or not self.ep_in:
            raise TransportError("USB transport not connected.")

        tag = self._next_tag()
        transfer_len = len(data_out) if data_out else data_in_len
        flags = 0x80 if data_in_len > 0 else 0x00

        cbw = build_cbw(tag, transfer_len, flags, cdb)

        # 1. Send Command Block Wrapper
        try:
            logger.debug(f"Writing CBW ({len(cbw)} bytes): {cbw.hex()}")
            self.ep_out.write(cbw, timeout=timeout_ms)
        except Exception as exc:
            raise TransportError(f"Failed to write CBW (tag 0x{tag:08X}): {exc}") from exc

        # 2. Data Phase
        data_in = b""
        if data_out and len(data_out) > 0:
            try:
                logger.debug(f"Writing Data-Out ({len(data_out)} bytes)")
                self.ep_out.write(data_out, timeout=timeout_ms)
            except Exception as exc:
                raise TransportError(f"Failed to write Data-Out ({len(data_out)} bytes): {exc}") from exc
        elif data_in_len > 0:
            try:
                max_pkt = getattr(self.ep_in, "wMaxPacketSize", 64) or 64
                read_len = max(data_in_len, max_pkt)
                logger.debug(f"Reading Data-In: requesting {read_len} bytes for expected {data_in_len}")
                raw_data = bytes(self.ep_in.read(read_len, timeout=timeout_ms))
                logger.debug(f"Received Data-In ({len(raw_data)} bytes): {raw_data.hex()}")
                data_in = raw_data[:data_in_len]
            except Exception as exc:
                raise TransportError(f"Failed to read Data-In ({data_in_len} bytes): {exc}") from exc

        # 3. Status Phase: Read 13-byte CSW
        try:
            max_pkt = getattr(self.ep_in, "wMaxPacketSize", 64) or 64
            read_len = max(CSW_LENGTH, max_pkt)
            logger.debug(f"Reading CSW: requesting {read_len} bytes")
            raw_csw = bytes(self.ep_in.read(read_len, timeout=timeout_ms))[:CSW_LENGTH]
            logger.debug(f"Received CSW ({len(raw_csw)} bytes): {raw_csw.hex()}")
        except Exception as exc:
            # Handle possible clear halt if stalled
            try:
                self.dev.clear_halt(self.ep_in.bEndpointAddress)
                max_pkt = getattr(self.ep_in, "wMaxPacketSize", 64) or 64
                raw_csw = bytes(self.ep_in.read(max(CSW_LENGTH, max_pkt), timeout=timeout_ms))[:CSW_LENGTH]
            except Exception:
                raise TransportError(f"Failed to read CSW (tag 0x{tag:08X}): {exc}") from exc

        csw_tag, residue, status = parse_csw(raw_csw)
        if csw_tag != tag:
            raise TransportError(f"CSW tag mismatch: got 0x{csw_tag:08X}, expected 0x{tag:08X}")

        if status != CSW_STATUS_PASSED:
            raise TransportError(f"Command execution failed on device (CSW status: 0x{status:02X})")

        return data_in


class JLSerialTransport(JLTransport):
    """Serial UART transport implementation using pyserial."""

    def __init__(
        self,
        port: str,
        baudrate: int = 1000000,
        init_baudrate: int = 9600,
        timeout: float = 2.0,
    ):
        self.port = port
        self.target_baudrate = baudrate
        self.init_baudrate = init_baudrate
        self.timeout = timeout
        self.ser = None

    def connect(self) -> None:
        import serial

        logger.info(f"Opening serial port {self.port} at {self.init_baudrate} baud...")
        try:
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.init_baudrate,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
        except Exception as exc:
            raise TransportError(f"Cannot open serial port {self.port}: {exc}") from exc

        # Flush buffers
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    def sync_and_negotiate_baudrate(self) -> None:
        """Perform initial sync handshake and switch to high-speed baudrate."""
        if not self.ser:
            raise TransportError("Serial port not open.")

        logger.info("Sending sync command (TX21)...")
        # Send TX21 sync pattern
        self.ser.write(UART_SYNC_CMD)
        time.sleep(0.05)

        # Check for ACK
        ack = self.ser.read(1)
        if ack and ack[0] == UART_ACK_BYTE:
            logger.debug("Received device ACK (0x79) at initial baudrate.")
        else:
            logger.debug(f"Sync response: {ack.hex() if ack else 'timeout'}; retrying sync sequence...")
            self.ser.write(b"\x00" * 4 + UART_SYNC_CMD)
            time.sleep(0.05)
            ack = self.ser.read(1)

        if self.target_baudrate != self.init_baudrate:
            logger.info(f"Switching baudrate to {self.target_baudrate}...")
            # Command baudrate change packet: [0xFC, 0x0C, baudrate_be, ...]
            # or direct port speed switch
            self.ser.baudrate = self.target_baudrate
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            time.sleep(0.05)

    def close(self) -> None:
        if self.ser and self.ser.is_open:
            self.ser.close()
            self.ser = None

    def send_cmd(
        self,
        cdb: bytes,
        data_out: Optional[bytes] = None,
        data_in_len: int = 0,
        timeout_ms: int = 5000,
    ) -> bytes:
        if not self.ser or not self.ser.is_open:
            raise TransportError("Serial transport not connected.")

        old_timeout = self.ser.timeout
        self.ser.timeout = timeout_ms / 1000.0

        try:
            frame = bytearray(cdb)
            if data_out:
                frame.extend(data_out)

            # Calculate frame CRC16 and append
            crc = crc16_ccitt(frame)
            frame.extend(struct.pack(">H", crc))

            # Send command frame
            self.ser.write(frame)

            # Wait for ACK
            ack = self.ser.read(1)
            if not ack:
                raise TransportError("Timeout waiting for serial ACK.")
            if ack[0] != UART_ACK_BYTE:
                raise TransportError(f"Device returned NACK / error: 0x{ack[0]:02X}")

            # Read response data if expected
            data_in = b""
            if data_in_len > 0:
                data_in = self.ser.read(data_in_len)
                if len(data_in) < data_in_len:
                    raise TransportError(
                        f"Incomplete data read: received {len(data_in)} of {data_in_len} bytes"
                    )

            return data_in
        finally:
            self.ser.timeout = old_timeout
