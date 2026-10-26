#!/bin/sh

set -eu
if [ -z "${JIELI_TOOL_DIR:-}" ]; then
    DIR="$(cd "$(dirname "$0")" && pwd -P)"
    if [ -x "${DIR}/chip/wl82/ipc_ac7916a/toolchain/jieli-linux-toolchains/pi32v2/bin/lto-ar" ]; then
        JIELI_TOOL_DIR="${DIR}/chip/wl82/ipc_ac7916a/toolchain/jieli-linux-toolchains/pi32v2/bin"
    elif [ -x "${DIR}/../tools/jieli-toolchain/jieli-linux-toolchains-20260730.1/pi32v2/bin/lto-ar" ]; then
        JIELI_TOOL_DIR="${DIR}/../tools/jieli-toolchain/jieli-linux-toolchains-20260730.1/pi32v2/bin"
    elif [ -x "${DIR}/../../tools/jieli-toolchain/jieli-linux-toolchains-20260730.1/pi32v2/bin/lto-ar" ]; then
        JIELI_TOOL_DIR="${DIR}/../../tools/jieli-toolchain/jieli-linux-toolchains-20260730.1/pi32v2/bin"
    elif [ -x "${DIR}/../jieli-toolchain/pi32v2/bin/lto-ar" ]; then
        JIELI_TOOL_DIR="${DIR}/../jieli-toolchain/pi32v2/bin"
    elif [ -x "${DIR}/../../jieli-toolchain/pi32v2/bin/lto-ar" ]; then
        JIELI_TOOL_DIR="${DIR}/../../jieli-toolchain/pi32v2/bin"
    elif [ -x "/opt/jieli/pi32v2/bin/lto-ar" ]; then
        JIELI_TOOL_DIR="/opt/jieli/pi32v2/bin"
    fi
fi
exec "${JIELI_TOOL_DIR}/lto-ar" s "$@"
