"""Unit tests for JieLi Flasher components."""

import struct
import sys
import unittest
from pathlib import Path

# Ensure repository root and tools are on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jieli_flasher.crypto import (
    cd03_cipher,
    crc8_maxim,
    crc16_ccitt,
    jl_crc_cipher,
    jl_enc_cipher,
    jl_sfc_cipher,
    scsi_xor_cipher,
)
from jieli_flasher.flasher import JLFlasher
from jieli_flasher.firmware import (
    SdfileEntry,
    build_flash_head_v2,
    build_sdfile_head,
    parse_isd_config_ini,
    parse_uboot_header,
)
from jieli_flasher.protocol import (
    CBW_SIGNATURE,
    CSW_SIGNATURE,
    CSW_STATUS_PASSED,
    OP_VENDOR,
    SUBCMD_CHIP_RESET,
    SUBCMD_ERASE_SECTOR,
    SUBCMD_GET_CHIP_ID,
    SUBCMD_GET_CRC16,
    SUBCMD_GET_FLASH_UUID,
    SUBCMD_READ_FLASH,
    SUBCMD_WRITE_FLASH,
    build_cbw,
    build_cdb,
    parse_csw,
)


class TestJieLiCrypto(unittest.TestCase):
    def test_crc16_standard_vector(self):
        # Standard CRC16-CCITT False on b"123456789" is 0x29B1 or 0x31C3 depending on model
        # JieLi table: 0x1021 nibble table with init=0
        vec = b"123456789"
        crc = crc16_ccitt(vec)
        self.assertEqual(crc, 0x31C3)

    def test_crc16_uboot_file(self):
        uboot_path = Path("TuyaOpen-JieLi/chip/wl82/AC79_AIoT_SDK/cpu/wl82/tools/uboot.boot")
        if uboot_path.is_file():
            data = uboot_path.read_bytes()
            header = parse_uboot_header(data)
            # Stored data CRC should match computed CRC of data portion
            computed = crc16_ccitt(data[16 : 16 + header.data_size])
            self.assertEqual(computed, header.data_crc)

    def test_crc8(self):
        vec = b"TX21"
        crc = crc8_maxim(vec)
        self.assertIsInstance(crc, int)
        self.assertTrue(0 <= crc <= 0xFF)

    def test_cd03_cipher_symmetry(self):
        plain = bytearray(b"Hello JieLi Firmware Payload 1234567890!@#$%^&*()")
        key = 0x5A5A
        encrypted = cd03_cipher(bytearray(plain), key)
        self.assertNotEqual(plain, encrypted)
        decrypted = cd03_cipher(bytearray(encrypted), key)
        self.assertEqual(plain, decrypted)

    def test_scsi_xor_cipher_symmetry(self):
        plain = bytearray(b"SixteenByteBlockTestForScsiPayload1234567890")
        key = b"0123456789ABCDEF"
        enc = scsi_xor_cipher(bytearray(plain), key)
        self.assertNotEqual(plain, enc)
        dec = scsi_xor_cipher(bytearray(enc), key)
        self.assertEqual(plain, dec)

    def test_jl_crc_cipher_symmetry(self):
        plain = bytearray(b"TestMengLiCipherPayload" * 10)
        copy_data = bytearray(plain)
        jl_crc_cipher(copy_data, 0, len(copy_data))
        self.assertNotEqual(plain, copy_data)
        jl_crc_cipher(copy_data, 0, len(copy_data))
        self.assertEqual(plain, copy_data)

    def test_jl_sfc_cipher_symmetry(self):
        plain = bytearray(b"TestSfcCipherPayloadDataBlock123456" * 8)
        copy_data = bytearray(plain)
        jl_sfc_cipher(copy_data, 0, len(copy_data), base=0x4000)
        self.assertNotEqual(plain, copy_data)
        jl_sfc_cipher(copy_data, 0, len(copy_data), base=0x4000)
        self.assertEqual(plain, copy_data)


class TestJieLiProtocol(unittest.TestCase):
    def test_build_cdb_erase_sector(self):
        cdb = build_cdb(SUBCMD_ERASE_SECTOR, addr=0x1C02000)
        self.assertEqual(len(cdb), 16)
        self.assertEqual(cdb[0], OP_VENDOR)
        self.assertEqual(cdb[1], SUBCMD_ERASE_SECTOR)
        self.assertEqual(cdb[2:6], b"\x01\xc0\x20\x00")

    def test_build_cdb_write_flash(self):
        payload = b"A" * 4096
        crc = crc16_ccitt(payload)
        cdb = build_cdb(SUBCMD_WRITE_FLASH, addr=0x2000, length=4096, crc=crc)
        self.assertEqual(len(cdb), 16)
        self.assertEqual(cdb[0], OP_VENDOR)
        self.assertEqual(cdb[1], SUBCMD_WRITE_FLASH)
        self.assertEqual(cdb[2:6], b"\x00\x00\x20\x00")
        self.assertEqual(cdb[6:8], b"\x10\x00")
        self.assertEqual(int.from_bytes(cdb[8:10], "big"), crc)

    def test_build_cbw_and_parse_csw(self):
        cdb = build_cdb(SUBCMD_GET_CHIP_ID)
        cbw = build_cbw(tag=0x12345678, transfer_length=16, flags=0x80, cdb=cdb)
        self.assertEqual(len(cbw), 31)

        sig, tag, length, flags, lun, cdb_len = struct.unpack("<IIIBBB", cbw[:15])
        self.assertEqual(sig, CBW_SIGNATURE)
        self.assertEqual(tag, 0x12345678)
        self.assertEqual(length, 16)
        self.assertEqual(flags, 0x80)

        # Mock valid CSW response
        csw_bytes = struct.pack("<IIIB", CSW_SIGNATURE, 0x12345678, 0, CSW_STATUS_PASSED)
        res_tag, residue, status = parse_csw(csw_bytes)
        self.assertEqual(res_tag, 0x12345678)
        self.assertEqual(residue, 0)
        self.assertEqual(status, CSW_STATUS_PASSED)

    def test_build_cdb_16bit_with_padding(self):
        # 16-bit opcode 0xFB01 with 0xFF padding
        cdb = build_cdb(0xFB01, addr=0x4000, pad=0xFF)
        self.assertEqual(len(cdb), 16)
        self.assertEqual(cdb[0], 0xFB)
        self.assertEqual(cdb[1], 0x01)
        self.assertEqual(cdb[2:6], b"\x00\x00\x40\x00")

    def test_locate_loader_binaries(self):
        flasher = JLFlasher(None)
        p82 = flasher._locate_loader("wl82loader.bin")
        p83 = flasher._locate_loader("wl83loader.bin")
        self.assertIsNotNone(p82)
        self.assertTrue(p82.is_file())
        self.assertIsNotNone(p83)
        self.assertTrue(p83.is_file())

    def test_prepare_device_auto_detects_chip_from_inquiry(self):
        class MockTransport:
            def __init__(self, product):
                self.product = product
                self.commands = []

            def inquiry(self):
                return ("JieLi", self.product, "1.00")

            def send_cmd(self, cdb, data_out=None, data_in_len=0, timeout_ms=3000):
                self.commands.append((cdb, data_out))
                return b"\x00" * data_in_len

        # Test WL83 auto-detection
        t83 = MockTransport("WL83UBOOT1.00")
        f83 = JLFlasher(t83)
        f83.prepare_device(chip_name="wl82")  # Passed wl82, but inquiry is WL83
        # Should upload wl83loader.bin (starts with target addr 0x00103000 in CDB bytes 2..6)
        self.assertTrue(len(t83.commands) > 0)
        first_cdb = t83.commands[0][0]
        addr = struct.unpack(">I", first_cdb[2:6])[0]
        self.assertEqual(addr, 0x00103000)

        # Test WL82 / AC791 auto-detection
        t82 = MockTransport("WL80UBOOT1.00")
        f82 = JLFlasher(t82)
        f82.prepare_device(chip_name="wl83")  # Passed wl83, but inquiry is WL80
        self.assertTrue(len(t82.commands) > 0)
        first_cdb_82 = t82.commands[0][0]
        addr_82 = struct.unpack(">I", first_cdb_82[2:6])[0]
        self.assertEqual(addr_82, 0x01C02000)


class TestJieLiSoftJump(unittest.TestCase):
    def test_sg_io_soft_jump_sends_opcode_0xfd(self):
        from unittest.mock import MagicMock, patch
        from jieli_flasher.transport import JLSgIoTransport

        mock_fd = 99
        transfers = []

        def mock_sg_io_transfer(fd, cdb, **kwargs):
            transfers.append((fd, cdb))
            return b""

        with patch("os.open", return_value=mock_fd), \
             patch("os.close") as mock_close, \
             patch.object(JLSgIoTransport, "_sg_io_transfer", side_effect=mock_sg_io_transfer):
            res = JLSgIoTransport.trigger_soft_boot("/dev/sg9")
            self.assertTrue(res)
            self.assertEqual(len(transfers), 1)
            self.assertEqual(transfers[0][0], mock_fd)
            # Check opcode is 0xFD
            self.assertEqual(transfers[0][1][0], 0xFD)
            mock_close.assert_called_once_with(mock_fd)

    def test_usb_scsi_soft_jump_sends_ep0_control_transfer(self):
        from unittest.mock import MagicMock
        from jieli_flasher.transport import JLUsbScsiTransport

        mock_dev = MagicMock()
        mock_dev.idVendor = 0x3654
        mock_dev.idProduct = 0x5155
        mock_dev.manufacturer = "JieLi"
        mock_dev.product = "UDISK"

        ctrl_calls = []

        def mock_ctrl_transfer(**kwargs):
            ctrl_calls.append(kwargs)
            return 0

        mock_dev.ctrl_transfer.side_effect = mock_ctrl_transfer

        res = JLUsbScsiTransport.trigger_soft_boot_device(mock_dev)
        self.assertTrue(res)
        self.assertTrue(len(ctrl_calls) > 0)
        # Check wValue = 0xFC, wIndex = 0xFE
        self.assertEqual(ctrl_calls[0].get("wValue"), 0x00FC)
        self.assertEqual(ctrl_calls[0].get("wIndex"), 0x00FE)


class TestJieLiFirmware(unittest.TestCase):
    def test_parse_isd_config_loader(self):
        cfg_path = Path("TuyaOpen-JieLi/chip/wl83/AC792_SDK/sdk/cpu/wl83/tools/isd_config_loader.ini")
        if cfg_path.is_file():
            parsed = parse_isd_config_ini(cfg_path)
            self.assertIn("EXTRA_CFG_PARAM", parsed)
            extra = parsed["EXTRA_CFG_PARAM"]
            self.assertEqual(extra.get("CHIP_NAME"), "AC792N")
            self.assertEqual(extra.get("DOWNLOAD_MODEL"), "usb")
            self.assertEqual(extra.get("ENTRY"), "0x8000120")

    def test_sdfile_head_builder(self):
        entry = SdfileEntry(
            name="app.bin",
            addr=0x2000,
            length=1024,
            data_crc=0xABCD,
            index=1,
        )
        head = build_sdfile_head(entry)
        self.assertEqual(len(head), 32)
        # Verify head_crc matches payload crc
        head_crc = int.from_bytes(head[:2], "little")
        calc_crc = crc16_ccitt(head[2:])
        self.assertEqual(head_crc, calc_crc)


if __name__ == "__main__":
    unittest.main()
