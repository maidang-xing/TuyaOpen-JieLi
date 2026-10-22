"""JieLi Cryptographic & Checksum Algorithms.

Implements:
1. CRC16-CCITT (polynomial 0x1021, init 0x0000) used for packet validation,
   flash verification, and header integrity.
2. CRC8 (polynomial 0x31 / 0x8c reversed, init 0x0000) used for serial frames.
3. CD03 16-bit LFSR stream cipher (jl_crypto::cd03_crc_encode).
4. SCSI XOR block cipher (jl_crypto::usb_scsi_crc_encode).
"""

from typing import Union

# Precomputed 4-bit nibble lookup table for CRC16-CCITT (poly 0x1021)
_CRC16_NIBBLE_TABLE = [
    0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5, 0x60C6, 0x70E7,
    0x8108, 0x9129, 0xA14A, 0xB16B, 0xC18C, 0xD1AD, 0xE1CE, 0xF1EF
]


def crc16_ccitt(data: Union[bytes, bytearray, memoryview], init_crc: int = 0) -> int:
    """Calculate 16-bit CRC-CCITT (False, poly=0x1021, init=0x0000).

    Matches JieLi hardware CRC accelerator and isd_download CRC implementation.
    """
    crc = init_crc & 0xFFFF
    for b in data:
        # High nibble
        idx = ((crc >> 12) ^ (b >> 4)) & 0x0F
        crc = ((crc << 4) ^ _CRC16_NIBBLE_TABLE[idx]) & 0xFFFF
        # Low nibble
        idx = ((crc >> 12) ^ (b & 0x0F)) & 0x0F
        crc = ((crc << 4) ^ _CRC16_NIBBLE_TABLE[idx]) & 0xFFFF
    return crc


def crc8_maxim(data: Union[bytes, bytearray, memoryview], init_crc: int = 0) -> int:
    """Calculate 8-bit CRC (poly=0x31 / reversed 0x8C, init=0x00).

    Used in JieLi serial bootloader frame synchronization.
    """
    crc = init_crc & 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8C
            else:
                crc >>= 1
    return crc & 0xFF


def cd03_cipher(data: bytes | bytearray, key: int, offset: int = 0) -> bytearray:
    """JieLi proprietary CD03 LFSR stream cipher (jl_crypto::cd03_crc_encode).

    Involutory (symmetric) stream XOR cipher: running twice restores original data.
    """
    if not data:
        return bytearray()

    buf = bytearray(data)
    bx = key
    if offset:
        bx ^= (offset >> 2)
    bx &= 0xFFFF

    # First byte is XORed with lower byte of initial key
    buf[0] ^= (bx & 0xFF)

    # Subsequent bytes advance through 16-bit LFSR
    for i in range(1, len(buf)):
        ah = (bx >> 8) & 0xFF
        al = bx & 0xFF

        dl = (ah >> 7) ^ 0x10
        cl = (((dl & 1) << 3) ^ ah) & 0xFF
        cl = ((cl << 1) | (al >> 7)) & 0xFF

        new_al = (((dl & 1) << 4) ^ al) & 0xFF
        new_al = ((new_al << 1) | (dl & 1)) & 0xFF

        buf[i] ^= new_al
        bx = (cl << 8) | new_al

    return buf


def scsi_xor_cipher(data: bytes | bytearray, key16: bytes) -> bytearray:
    """JieLi 16-byte SCSI payload XOR obfuscation (jl_crypto::usb_scsi_crc_encode)."""
    if not data:
        return bytearray()
    if not key16:
        return bytearray(data)

    buf = bytearray(data)
    klen = len(key16)
    for i in range(len(buf)):
        buf[i] ^= key16[i % klen]
    return buf
