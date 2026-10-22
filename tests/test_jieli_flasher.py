"""Unit tests for JieLi Flasher components."""

import struct
import sys
import unittest
from pathlib import Path

# Ensure repository root and tools are on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jieli_flasher.crypto import cd03_cipher, crc8_maxim, crc16_ccitt, scsi_xor_cipher
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
