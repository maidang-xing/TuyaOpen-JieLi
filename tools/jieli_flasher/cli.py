"""JieLi Flasher Command Line Interface."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .flasher import JLFlasher
from .transport import JLSerialTransport, JLTransport, JLUsbScsiTransport

logger = logging.getLogger("jieli_flasher")


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jieli_flasher",
        description="JieLi (杰理) Cross-Platform Firmware Flashing Tool (Windows, Linux, macOS)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Positional config argument (compatible with vendor isd_download.exe syntax)
    parser.add_argument(
        "config_pos",
        nargs="?",
        default=None,
        metavar="CONFIG_INI",
        help="Optional positional path to isd_config.ini",
    )

    # Device & Transport Options
    t_group = parser.add_argument_group("Transport Options")
    t_group.add_argument(
        "-m", "--mode",
        choices=["auto", "usb", "serial"],
        default="auto",
        help="Communication mode: auto (default), usb (Mass Storage BOT), serial (UART)",
    )
    t_group.add_argument(
        "-p", "--port",
        type=str,
        default=None,
        help="Serial port device path (e.g. /dev/ttyUSB0, COM3)",
    )
    t_group.add_argument(
        "-b", "--baud",
        type=int,
        default=1000000,
        help="Serial communication baudrate (default: 1000000)",
    )
    t_group.add_argument(
        "--init-baud",
        type=int,
        default=9600,
        help="Serial initial handshake baudrate (default: 9600)",
    )

    # Target Options
    target_group = parser.add_argument_group("Target Options")
    target_group.add_argument(
        "-dev", "--chip",
        type=str,
        default="wl82",
        help="Target chip architecture (e.g. wl82, wl83, ac791n, ac792n)",
    )
    target_group.add_argument(
        "-boot", "--boot-addr",
        type=lambda x: int(x, 0),
        default=0x0,
        help="Boot address in hex or decimal (e.g. 0x1c02000 for wl82, 0x103000 for wl83)",
    )

    # Firmware & File Options
    file_group = parser.add_argument_group("Firmware & Files")
    file_group.add_argument(
        "-f", "--file",
        type=str,
        default=None,
        help="Firmware file to flash (raw .bin, .ufw, or app.bin)",
    )
    file_group.add_argument(
        "-wflash",
        nargs=2,
        metavar=("ADDR", "BINFILE"),
        help="Raw flash write: specify <addr> <binfile>",
    )
    file_group.add_argument(
        "-c", "--config",
        type=str,
        default=None,
        help="Path to isd_config.ini configuration file",
    )
    file_group.add_argument(
        "-uboot",
        type=str,
        default=None,
        help="Path to uboot.boot bootloader file",
    )
    file_group.add_argument(
        "-app",
        nargs="+",
        default=None,
        help="Path to application binary and optional secondary files (e.g. app.bin cfg_tool.bin)",
    )
    file_group.add_argument(
        "-cfg",
        type=str,
        default=None,
        help="Path to cfg_tool.bin configuration binary",
    )
    file_group.add_argument(
        "-res",
        nargs="*",
        default=[],
        help="Resource directories/files (e.g. cfg, ui_res, audlogo)",
    )

    # Vendor Legacy / Compatibility Options (accepted and ignored gracefully)
    compat_group = parser.add_argument_group("Vendor Compatibility Flags")
    compat_group.add_argument("-gen2", action="store_true", help=argparse.SUPPRESS)
    compat_group.add_argument("-tonorflash", action="store_true", help=argparse.SUPPRESS)
    compat_group.add_argument("-to-sdcard", action="store_true", help=argparse.SUPPRESS)
    compat_group.add_argument("-div1", action="store_true", help=argparse.SUPPRESS)
    compat_group.add_argument("-wait", type=int, default=300, help=argparse.SUPPRESS)
    compat_group.add_argument("-extend-bin", action="store_true", help=argparse.SUPPRESS)
    compat_group.add_argument("-output-bin", type=str, default=None, help=argparse.SUPPRESS)
    compat_group.add_argument("-no-app-bin-enc", action="store_true", help=argparse.SUPPRESS)
    compat_group.add_argument("-update_files", type=str, default=None, help=argparse.SUPPRESS)

    # Operations & Controls
    op_group = parser.add_argument_group("Flashing Operations")
    op_group.add_argument(
        "--probe", "--info",
        action="store_true",
        help="Probe connected device and display Chip ID, Flash UUID, and Flash parameters",
    )
    op_group.add_argument(
        "--format",
        choices=["all", "vm"],
        default=None,
        help="Erase flash: all (full chip erase) or vm (erase virtual machine region)",
    )
    op_group.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip flash data verification after writing",
    )
    op_group.add_argument(
        "-reboot",
        type=int,
        default=500,
        help="Send reset command after flashing with delay in ms (default: 500, 0 to disable)",
    )
    op_group.add_argument(
        "--no-reboot",
        action="store_true",
        help="Do not reboot chip after flashing",
    )
    op_group.add_argument(
        "--read-flash",
        nargs=3,
        metavar=("ADDR", "LENGTH", "OUTPUT"),
        help="Read flash memory to file: specify <addr> <length> <output_file>",
    )

    # Verbosity & Logging
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose / debug logging output",
    )

    return parser


def select_transport(args: argparse.Namespace) -> JLTransport:
    """Select and instantiate transport based on mode and availability."""
    mode = args.mode
    wait_val = getattr(args, "wait", 30)
    wait_sec = float(wait_val) if wait_val <= 60 else (float(wait_val) / 10.0)

    if mode == "auto":
        # Check if USB device is detected
        usb_devices = JLUsbScsiTransport.find_devices()
        if usb_devices:
            logger.info(f"Auto-selected USB mode ({len(usb_devices)} JieLi USB device(s) found).")
            return JLUsbScsiTransport(wait_seconds=wait_sec)
        elif args.port:
            logger.info(f"Auto-selected Serial mode on port {args.port}.")
            return JLSerialTransport(
                port=args.port,
                baudrate=args.baud,
                init_baudrate=args.init_baud,
            )
        else:
            # Default to USB transport (it will probe on connect)
            return JLUsbScsiTransport(wait_seconds=wait_sec)
    elif mode == "usb":
        return JLUsbScsiTransport(wait_seconds=wait_sec)
    elif mode == "serial":
        if not args.port:
            raise ValueError("Serial port (-p/--port) must be specified when mode is serial.")
        return JLSerialTransport(
            port=args.port,
            baudrate=args.baud,
            init_baudrate=args.init_baud,
        )
    else:
        raise ValueError(f"Unknown transport mode: {mode}")


def main(argv: list[str] | None = None) -> int:
    parser = create_parser()
    args, unknown = parser.parse_known_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if unknown and args.verbose:
        logger.debug(f"Ignored legacy options: {unknown}")

    # Resolve config file path (positional or -c)
    config_path = args.config or args.config_pos

    print("=" * 65)
    print("      JieLi (杰理) Cross-Platform Flashing Tool v1.0.0")
    print("        Supports Windows, Linux, and macOS (USB & UART)")
    print("=" * 65)

    transport = None
    try:
        transport = select_transport(args)
        transport.connect()
        flasher = JLFlasher(transport)

        # 1. Device Probe (if requested)
        if args.probe:
            info = flasher.probe()
            print(f"[*] Chip Model: {info['chip_id']}")
            if info["flash_uuid"]:
                print(f"[*] Flash UUID: {info['flash_uuid']}")
            return 0

        # 2. Read flash if requested
        if args.read_flash:
            addr = int(args.read_flash[0], 0)
            length = int(args.read_flash[1], 0)
            out_path = Path(args.read_flash[2])
            print(f"[*] Reading {length} bytes from flash 0x{addr:08X} to {out_path}...")
            # Read in 4KB chunks
            data = bytearray()
            offset = 0
            while offset < length:
                chunk_len = min(4096, length - offset)
                cdb = flasher.transport.send_cmd(
                    cdb=flasher.transport.build_cdb(0x05, addr=addr + offset, length=chunk_len),
                    data_in_len=chunk_len,
                )
                data.extend(cdb)
                offset += chunk_len
            out_path.write_bytes(data)
            print(f"[+] Flash read saved to {out_path} ({len(data)} bytes).")
            return 0

        # 3. Full Erase if requested
        if args.format == "all":
            flasher.erase_all()

        # 4. Handle Raw Flash Write (-wflash ADDR FILE)
        if args.wflash:
            target_addr = int(args.wflash[0], 0)
            target_file = Path(args.wflash[1])
            if not target_file.is_file():
                raise FileNotFoundError(f"File not found: {target_file}")
            fw_bytes = target_file.read_bytes()
            print(f"[*] Raw flashing {target_file} ({len(fw_bytes)} bytes) at 0x{target_addr:08X}...")
            flasher.flash_firmware(
                firmware_bytes=fw_bytes,
                start_addr=target_addr,
                verify=not args.no_verify,
                reboot=not args.no_reboot and (args.reboot > 0),
                reboot_delay=args.reboot,
            )
            print("[+] Flashing completed successfully!")
            return 0

        # 5. Handle Standard Firmware File (-f / --file)
        if args.file:
            target_file = Path(args.file)
            if not target_file.is_file():
                raise FileNotFoundError(f"Firmware file not found: {target_file}")
            fw_bytes = target_file.read_bytes()
            start_addr = args.boot_addr
            print(f"[*] Flashing firmware {target_file} ({len(fw_bytes)} bytes) at offset 0x{start_addr:08X}...")
            flasher.flash_firmware(
                firmware_bytes=fw_bytes,
                start_addr=start_addr,
                verify=not args.no_verify,
                reboot=not args.no_reboot and (args.reboot > 0),
                reboot_delay=args.reboot,
            )
            print("[+] Firmware flashed and verified successfully!")
            return 0

        # 6. Handle multi-part SDK inputs (-uboot, -app, -cfg)
        if args.app or args.uboot:
            if args.uboot:
                uboot_p = Path(args.uboot)
                if uboot_p.is_file():
                    uboot_data = uboot_p.read_bytes()
                    print(f"[*] Flashing uboot ({len(uboot_data)} bytes) at 0x00000000...")
                    flasher.erase_range(0x0, len(uboot_data), progress_cb=flasher.print_progress if hasattr(flasher, "print_progress") else None)
                    flasher.write_flash(0x0, uboot_data)

            app_files = []
            if args.app:
                app_files.extend(args.app if isinstance(args.app, list) else [args.app])
            if args.cfg and args.cfg not in app_files:
                app_files.append(args.cfg)

            for idx, app_file in enumerate(app_files):
                app_p = Path(app_file)
                if app_p.is_file():
                    app_data = app_p.read_bytes()
                    # First file is main application, secondary files (like cfg_tool.bin) follow
                    if idx == 0:
                        app_addr = args.boot_addr if args.boot_addr else 0x2000
                    else:
                        app_addr = 0x10000 + (idx - 1) * 0x10000
                    print(f"[*] Flashing {app_p.name} ({len(app_data)} bytes) at 0x{app_addr:08X}...")
                    flasher.erase_range(app_addr, len(app_data))
                    flasher.write_flash(app_addr, app_data)

            flasher.flash_done()
            if not args.no_reboot and args.reboot > 0:
                flasher.reset(delay_ms=args.reboot)
            print("[+] Multi-part firmware flashing completed!")
            return 0

        print("[!] No action specified. Use -f <firmware.bin>, -wflash <addr> <file>, or --probe.")
        return 0

    except Exception as exc:
        logger.error(f"Error during flashing: {exc}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1
    finally:
        if transport:
            transport.close()
