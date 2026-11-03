# JIELI TKL 适配说明

## 分层

| 层 | 位置 | 职责 |
|---|---|---|
| 厂商 SDK | `chip/wl82`、`chip/wl83` | 只读，不修改 |
| 构建期翻译 | `tools/jieli_build/` | 复制 SDK 到 `.build/jieli-staging/`，改写 vendor `board.c` |
| TKL 适配 | `tuyaos/tuyaos_adapter/` | TKL 实现 + 设备注册 |
| 板级 | 主仓 `boards/JIELI/` | Kconfig、`board_register_hardware()` |

## 设备注册模型

厂商没有运行期注册 API。`REGISTER_DEVICES()` 把 `struct dev_node[]` 放进
`.device` 段，`devices_init()` 遍历链接器符号 `_device_node_begin..end` 初始化
整张表。因此：

- 设备在**链接期**进表，在**运行期**由 `dev_open("name", priv)` 取用
- TKL 源码自己声明设备表，例：`tkl_pwm.c` 的 `REGISTER_DEVICE(pwm1_node)`
- 板级差异用 `#if defined(JIELI_SELECTED_CHIP_WL82)` / `WL83`

**配置读取时机**（决定哪些外设运行期可配）：

| 外设 | 何时读 `arg` |
|---|---|
| UART / PWM | `*_open(arg)`，运行期可改 |
| SPI / IIC | `devices_init()` 时锁存，运行期不可改 |

## 构建门控

唯一门控点是 `tuyaos_adapter/adapter_sources.txt`，每行 `源文件 [CONFIG_XXX]`。
源文件内部不要用 `#if CONFIG_*` 包整个文件。

## 板级矩阵

| | wl82 / AC79_DevKitBoard (AC791) | wl83 / AC792N_Develop_Board |
|---|---|---|
| Flash / SDRAM | 8M / 8M | 由 board facts 决定 |
| 显示 | ILI9488 320x480，8-bit EMI（MCU8080） | MIPI-DSI |
| 音频 | 无 `audio_adc/dac.h`，用自有 TKL provider | 需要 media 打开 CVP |
| UART 接收 | 无 IRQ-event ioctl，TAL/CLI 接收不可用 | 可用 |

## 硬件坑（都踩过）

**显示**
- LVGL 给的是小端 RGB565，8-bit EMI 上必须 `is_swap = true`（厂商
  `ili9488_clear_screen()` 按高字节先打包）。否则黑白正常、彩色全错。
- init 序列必须用厂商 `lcd_ili9488.c` 的 `code1[]`。TuyaOpen RGB 版序列的
  `0xB6 = 0x3B`、`MADCTL 0x48` 会让整幅画面变负片。
- 反色**不是** `0x21` INVON 造成的，厂商序列同样开 INVON。

**音频**
- PA mute 极性：PE15 高 = 功放开（LEVEL=0 覆盖时反过来），需要短接 J24。
- `audio_dac_set_volume()` 单独调用无效，必须先 `app_audio_state_switch()`。
- 板载麦克风需要 ADC 增益 19，两个麦克风不能同时打开。

## 构建与烧录

```bash
export JIELI_TOOL_DIR=platform/JIELI/tools/toolchains/<os>/pi32/bin
tos.py build
tos.py flash
```

Windows 上 PATH 需要排除 Git 的 `usr/bin`，并使用 `.venv` 的 Python。
