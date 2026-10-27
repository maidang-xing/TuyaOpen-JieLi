# tuyaopen-platform-JIELI

English | [中文](README_zh.md)

This repository is a sub-repository of tuyaopen and cannot be compiled and used independently. It will be automatically downloaded in tuyaopen. For detailed usage instructions, please refer to [https://github.com/tuya/tuyaopen](https://github.com/tuya/tuyaopen).

## JieLi Platform: AC79_DevKitBoard and AC792N_Develop_Board

| Board Name | Chip / Architecture | Status | SDK |
| --- | --- | --- | --- |
| `AC79_DevKitBoard` | AC791 / WL82 | Verified with complete TuyaOpen applications (e.g. `switch_demo`, `your_chat_bot`) | `chip/wl82/AC79_AIoT_SDK` (`release/AC79NN_SDK_V1.2.0`, tag `AC79NN_SDK_V1.2.13_2026-04-20`) |
| `AC792N_Develop_Board` | AC792N / WL83 | Verified with complete TuyaOpen applications, USB flashing, and runtime USB upgrades (USB VID:PID `3654:7857`) | `chip/wl83/AC792_SDK` (`release/AC792N_SDK_V3`, tag `AC792N_SDK_BETA_V3.1.7_2026-08-25`) |

All projects are built and managed using `tos.py` from the TuyaOpen repository root directory. The AC79 and AC792 SDK source code is included directly in the `chip/` directory.

## Board Selection and Building

Execute build commands in the root directory of the TuyaOpen workspace.

For AC79 applications (e.g. `apps/tuya_cloud/switch_demo`):

```bash
tos.py config set CONFIG_BOARD_CHOICE_AC79_DEVKITBOARD=y CONFIG_JIELI_UART_LOG_PORT=1 CONFIG_JIELI_UART_LOG_BAUDRATE=115200
tos.py build
```

For AC792 applications:

```bash
tos.py config set CONFIG_BOARD_CHOICE_AC792N_DEVELOP_BOARD=y CONFIG_JIELI_UART_LOG_PORT=0 CONFIG_JIELI_UART_LOG_BAUDRATE=115200
tos.py build
```

The resulting firmware binary includes Tuya TKL Wi-Fi/BLE, BLE provisioning, Tuya IoT cloud connectivity, and standard application features.

### Windows Portable Toolchain

The portable toolchain archive is located at `tools/toolchains/windows/pi32-2.5.2.zip`. When building on Windows without an explicit `JIELI_TOOL_DIR`, `build_setup.py` automatically extracts it to `.tools/portable-jieli-windows/pi32/`:

```text
tools/toolchains/windows/pi32-2.5.2.zip
.tools/portable-jieli-windows/pi32/bin/clang.exe  # Automatically extracted on first build
```

Users can also specify an existing toolchain directory by setting the `JIELI_TOOL_DIR` environment variable.

## Serial Port and Hardware Reference

| Board | UART Log & CLI Configuration | Flash / RAM Reference |
| --- | --- | --- |
| `AC79_DevKitBoard` | Log: UART1, TX=PB3, 115200 baud; TAL CLI: UART0, TX=PA5, RX=PA6, 115200 baud | Flash ID `5E4017` (8 MiB); standard board specification: 8 MiB SDRAM, on-chip SRAM 578 KB. Staging config: 8 MiB Flash / 8 MiB SDRAM |
| `AC792N_Develop_Board` | UART0 shared for log & Tuya CLI: TX=PD1, RX=PE11, 115200 baud | Flash ID `5E4017` (8 MiB); staging config: 8 MiB Flash / 16 MiB DDR1 |

## Flashing and Serial Monitor

After building, flash and monitor the device:

```bash
tos.py flash
tos.py monitor -p <PORT>
```

### Entering Download Mode

1. **First flash or blank chip**:
   - **AC792N**: Press and hold the `UPDATE` button while powering on or resetting to enter USB Boot mode (enumerated as `WL83 UBOOT1.00 USB Device`).
   - **AC79**: Press and hold the download key while powering on to enter WL82 USB download mode.
2. **Runtime USB Upgrade**:
   Firmware built with TuyaOpen includes USB Mass Storage slave support. When connected to the PC via the USB data port and running, executing `tos.py flash` triggers a soft-jump into download mode automatically.

## Known Limitations

**WPA3 Limitation on AC792N:**
During STA association, CPU1 MbedTLS encounters an exception. As a workaround, `CONFIG_WPA3_SUPPORT` is set to 0 in `chip/wl83/AC792_SDK/sdk/apps/common/net/wifi_conf.c`. AC792 currently does not support WPA3-only AP networks; WPA2 and lower modes operate normally.
