# tuyaopen-platform-JIELI

[English](README.md) | 中文

该仓库为 tuyaopen 子仓库，无法单独编译与使用，会在 tuyaopen 中自动下载，详细使用方法参考 [https://github.com/tuya/tuyaopen](https://github.com/tuya/tuyaopen)。

## JieLi 平台：AC79_DevKitBoard 与 AC792N_Develop_Board

| 统一板名 | 芯片/平台 | 状态 | SDK |
| --- | --- | --- | --- |
| `AC79_DevKitBoard` | AC791 / WL82 | 完整 TuyaOpen 应用（如 `switch_demo`、`your_chat_bot`）构建与运行已验证 | `chip/wl82/AC79_AIoT_SDK`（`release/AC79NN_SDK_V1.2.0`，tag `AC79NN_SDK_V1.2.13_2026-04-20`）|
| `AC792N_Develop_Board` | AC792N / WL83 | 完整 TuyaOpen 应用构建、USB 烧录及运行时 USB 升级已验证（USB VID:PID `3654:7857`） | `chip/wl83/AC792_SDK`（`release/AC792N_SDK_V3`，tag `AC792N_SDK_BETA_V3.1.7_2026-08-25`）|

所有工程均从 TuyaOpen 仓库根目录使用 `tos.py` 构建与管理。AC79 与 AC792 SDK 源码直接纳入 JieLi 平台仓库的 `chip/` 目录，不使用 Git submodule；AC79 SDK 中未使用的 `libmatter.a` 按约定忽略。Windows 工具链优先使用显式设置的 `JIELI_TOOL_DIR`，其次自动解压平台本地 portable 目录，再兼容系统默认安装目录；Linux 支持通过环境变量或标准路径配置 `pi32v2` 工具链。显式目录无效时会明确报错。

## 选择板卡与构建

在 TuyaOpen 仓库根目录下执行构建命令。

AC79 完整应用（例如 `apps/tuya_cloud/switch_demo`）：

```bash
tos.py config set CONFIG_BOARD_CHOICE_AC79_DEVKITBOARD=y CONFIG_JIELI_UART_LOG_PORT=1 CONFIG_JIELI_UART_LOG_BAUDRATE=115200
tos.py build
```

AC792 完整应用：

```bash
tos.py config set CONFIG_BOARD_CHOICE_AC792N_DEVELOP_BOARD=y CONFIG_JIELI_UART_LOG_PORT=0 CONFIG_JIELI_UART_LOG_BAUDRATE=115200
tos.py build
```

构建生成的镜像包含 Tuya TKL Wi-Fi/BLE、BLE 配网、Tuya IoT 云端连接和 DP 业务逻辑。

### Windows 本地 portable 工具链

源码工作区约定的工具链归档位于 `tools/toolchains/windows/pi32-2.5.2.zip`。Windows 构建找不到显式或已安装工具链时，`build_setup.py` 会校验 SHA-256，并将归档安全解压至 `.tools/portable-jieli-windows/pi32/`；解压结果包含完整 `bin`、头文件和库目录：

```text
tools/toolchains/windows/pi32-2.5.2.zip
.tools/portable-jieli-windows/pi32/bin/clang.exe  # 首次构建自动解压
```

TuyaOpen 的 Python、CMake、Ninja 和 GNU Make 等主机工具由 TuyaOpen 的 `tos.py prepare` 提供。工具链发行标签为 2.5.2，包内 clang 自报版本 4.0.1。用户亦可通过环境变量 `JIELI_TOOL_DIR` 指向其他已安装的 `pi32/bin` 路径。

## TKL 头文件兼容基线

`tuyaos/tuyaos_adapter/include/<域>/tkl_*.h` 是 JieLi 平台随仓维护的兼容快照，构建时优先于 TuyaOpen 公共 include 路径。

TuyaOpen 更新 TKL 接口时，以 TuyaOpen 仓库中的 `tools/porting/adapter/` 为同步来源，比较并更新本地快照，再检查 WL82、WL83 的构建。

## 板级串口与硬件规格参考

| 板卡 | UART 日志与 CLI 配置 | Flash / RAM 参考 |
| --- | --- | --- |
| `AC79_DevKitBoard` | 日志：UART1、TX=PB3、115200 baud；TAL CLI：UART0、TX=PA5、RX=PA6、115200 baud | 实测 Flash ID `5E4017`（8 MiB）；标准开发板资料为 8 MiB SDRAM、片上 SRAM 578 KB。Tuya 构建 staging 配置为 8 MiB Flash / 8 MiB SDRAM |
| `AC792N_Develop_Board` | UART0 日志与 Tuya CLI 共用，TX=PD1、RX=PE11、115200 baud | 实测 Flash ID `5E4017`（8 MiB）；构建配置为 8 MiB Flash / 16 MiB DDR1 |

## 烧录与串口监视

构建完成后执行烧录与串口监视：

```bash
tos.py flash
tos.py monitor -p <串口号>
```

`tos.py flash` 根据当前板卡选择对应 SDK 的烧录工具和配置文件。

### 进入下载模式

1. **首次烧录或空片**：
   - **AC792N**：按住板上 `UPDATE` 键并重新上电或复位，进入 USB Boot 模式（枚举为 `WL83 UBOOT1.00 USB Device`）。
   - **AC79**：按住下载按键重新上电进入 WL82 USB 下载模式。
2. **USB 运行时升级（支持免按键）**：
   AC79 与 AC792 的完整 Tuya 应用构建中已集成 USB Mass Storage 从机服务。当固件正常运行且通过板上 USB 数据接口连接 PC 时，再次执行 `tos.py flash` 会自动触发软跳转并完成固件烧录。

串口监视波特率默认根据 Kconfig 配置读取（115200 baud），亦可显式指定：

```bash
tos.py monitor -p COM3 -b 115200
```

## 已知限制

**AC792N 当前不支持 WPA3：**
在 STA 关联阶段 CPU1 的 MbedTLS 会触发异常，作为临时规避，AC792 SDK 中的 `CONFIG_WPA3_SUPPORT` 被置为 0：

```c
// chip/wl83/AC792_SDK/sdk/apps/common/net/wifi_conf.c
const u8 CONFIG_WPA3_SUPPORT = 0;  // 原值为 1
```

影响范围：AC792 无法连接仅支持 WPA3/SAE 的 AP；WPA2 及以下不受影响。
