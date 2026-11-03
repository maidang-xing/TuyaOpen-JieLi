set(JIELI_ADAPTER_NAME jieli_tuya_adapter)

if(CONFIG_CHIP_CHOICE STREQUAL "wl82")
    set(JIELI_SELECTED_CHIP_DEFINE JIELI_SELECTED_CHIP_WL82)
elseif(CONFIG_CHIP_CHOICE STREQUAL "wl83")
    set(JIELI_SELECTED_CHIP_DEFINE JIELI_SELECTED_CHIP_WL83)
    # The wl83 audio provider pulls vendor media headers (audio_dac.h ->
    # asm/cpu_dac.h, adc_file.h, ...) whose homes sit outside include_lib's
    # driver/system trees. Mirror the include set the staged audio profile
    # gives the same translation unit (tools/jieli_build/audio_profile.py).
    set(JIELI_ADAPTER_CHIP_INCLUDES
        ${JIELI_SDK_ROOT}/include_lib/media
        ${JIELI_SDK_ROOT}/include_lib/media/cpu/wl83
        ${JIELI_SDK_ROOT}/include_lib/media/cpu/wl83/asm
        ${JIELI_SDK_ROOT}/include_lib/media/framework/include
        ${JIELI_SDK_ROOT}/include_lib/media/cvp
        ${JIELI_SDK_ROOT}/audio
        ${JIELI_SDK_ROOT}/audio/cpu/wl83
        ${JIELI_SDK_ROOT}/audio/CVP
        ${JIELI_SDK_ROOT}/audio/common
        ${JIELI_SDK_ROOT}/audio/effect
        ${JIELI_SDK_ROOT}/audio/effect/spatial_effect
        ${JIELI_SDK_ROOT}/audio/framework/include
        ${JIELI_SDK_ROOT}/audio/interface
        ${JIELI_SDK_ROOT}/audio/interface/includes
        # The vendor headers reach app_config.h / board_config.h with quoted
        # includes. The staged Makefile resolves them to the demo app's copies
        # because the vendor INCLUDES block lists the demo dirs before the
        # profile's wifi_camera dirs; keep the same precedence here or the
        # wifi_camera board chain (lcd_board_cfg_template.h) leaks in.
        ${JIELI_SDK_ROOT}/apps
        ${JIELI_SDK_ROOT}/apps/demo/demo_hello/include
        ${JIELI_SDK_ROOT}/apps/demo/demo_hello/board/wl83
        ${JIELI_SDK_ROOT}/include_lib/system/os
        ${JIELI_SDK_ROOT}/apps/common/system
        ${JIELI_SDK_ROOT}/apps/wifi_camera/include
        ${JIELI_SDK_ROOT}/apps/wifi_camera/board/wl83
        # tkl_display.c drives the vendor LCD/DSI stack, whose public headers
        # live outside include_lib.
        ${JIELI_SDK_ROOT}/apps/common/lcd/include
        ${JIELI_SDK_ROOT}/apps/common/lcd/lcd_driver
    )
else()
    message(FATAL_ERROR "JIELI requires CONFIG_CHIP_CHOICE to be wl82 or wl83")
endif()

if(NOT DEFINED DOT_CONFIG_DIR OR NOT DEFINED DOT_CONFIG)
    message(FATAL_ERROR "JIELI adapter source gates require DOT_CONFIG_DIR and DOT_CONFIG from the evaluated Kconfig build")
endif()
set(JIELI_EVALUATED_KCONFIG_FILE "${DOT_CONFIG_DIR}/${DOT_CONFIG}")
if(NOT EXISTS "${JIELI_EVALUATED_KCONFIG_FILE}")
    message(FATAL_ERROR "JIELI evaluated Kconfig file is missing: ${JIELI_EVALUATED_KCONFIG_FILE}")
endif()
file(STRINGS "${JIELI_EVALUATED_KCONFIG_FILE}" JIELI_EVALUATED_KCONFIG_LINES)

# Kconfiglib omits this bool when ENABLE_DISPLAY is off. When display is on,
# write_config emits an explicit n for the visible default, so only an
# explicitly disabled display permits this gate to be absent.
set(JIELI_OPTIONAL_ADAPTER_SOURCE_GATES CONFIG_ENABLE_MIPI_DSI)

function(jieli_lookup_evaluated_config symbol output_variable)
    set(config_value "")
    foreach(config_line IN LISTS JIELI_EVALUATED_KCONFIG_LINES)
        if(config_line MATCHES "^${symbol}=(.*)$")
            set(config_value "${CMAKE_MATCH_1}")
            break()
        elseif(config_line STREQUAL "# ${symbol} is not set")
            set(config_value "n")
            break()
        endif()
    endforeach()
    set(${output_variable} "${config_value}" PARENT_SCOPE)
endfunction()

file(STRINGS "${CMAKE_CURRENT_LIST_DIR}/adapter_sources.txt" JIELI_ADAPTER_RELATIVE_SRC)
set(JIELI_ADAPTER_SRC)
set(JIELI_ADAPTER_SEEN_SOURCES)
foreach(source_line IN LISTS JIELI_ADAPTER_RELATIVE_SRC)
    string(FIND "${source_line}" "#" comment_position)
    if(NOT comment_position EQUAL -1)
        string(SUBSTRING "${source_line}" 0 ${comment_position} normalized_source_line)
    else()
        set(normalized_source_line "${source_line}")
    endif()
    string(STRIP "${normalized_source_line}" normalized_source_line)
    string(ASCII 9 JIELI_TAB)
    string(REPLACE "${JIELI_TAB}" " " normalized_source_line "${normalized_source_line}")
    string(STRIP "${normalized_source_line}" normalized_source_line)
    if(normalized_source_line STREQUAL "")
        continue()
    endif()
    string(FIND "${normalized_source_line}" " " source_separator)
    if(source_separator EQUAL -1)
        set(source "${normalized_source_line}")
        set(gate "")
    else()
        string(SUBSTRING "${normalized_source_line}" 0 ${source_separator} source)
        math(EXPR gate_start "${source_separator} + 1")
        string(SUBSTRING "${normalized_source_line}" ${gate_start} -1 gate)
    endif()
    string(STRIP "${gate}" gate)
    string(FIND "${source}" "\\" backslash_position)
    if(NOT source MATCHES "^src/" OR source MATCHES "(^|/)\\.\\.(/|$)" OR
       source MATCHES "^/" OR NOT backslash_position EQUAL -1 OR NOT source MATCHES "\\.c$")
        message(FATAL_ERROR "Invalid adapter source path '${source}' in adapter_sources.txt")
    endif()
    list(FIND JIELI_ADAPTER_SEEN_SOURCES "${source}" duplicate_index)
    if(NOT duplicate_index EQUAL -1)
        message(FATAL_ERROR "Duplicate adapter source '${source}' in adapter_sources.txt")
    endif()
    list(APPEND JIELI_ADAPTER_SEEN_SOURCES "${source}")
    if(NOT EXISTS "${CMAKE_CURRENT_LIST_DIR}/${source}")
        message(FATAL_ERROR "Adapter source does not exist: ${source}")
    endif()
    if(gate)
        if(NOT gate MATCHES "^CONFIG_[A-Z0-9_]+$")
            message(FATAL_ERROR "Invalid adapter source gate '${gate}' in adapter_sources.txt: ${source_line}")
        endif()
        jieli_lookup_evaluated_config("${gate}" gate_value)
        if(gate_value STREQUAL "")
            list(FIND JIELI_OPTIONAL_ADAPTER_SOURCE_GATES "${gate}" optional_gate_index)
            if(optional_gate_index EQUAL -1)
                message(FATAL_ERROR "Undefined adapter source gate '${gate}' in adapter_sources.txt")
            endif()
            jieli_lookup_evaluated_config("CONFIG_ENABLE_DISPLAY" display_value)
            if(NOT display_value STREQUAL "n")
                if(display_value STREQUAL "")
                    set(display_value "undefined")
                endif()
                message(FATAL_ERROR
                    "Adapter source gate '${gate}' is missing while CONFIG_ENABLE_DISPLAY=${display_value}; only an explicitly disabled display may omit this hidden gate"
                )
            endif()
            set(gate_value "n")
        endif()
        if(NOT gate_value STREQUAL "y" AND NOT gate_value STREQUAL "n")
            message(FATAL_ERROR "Adapter source gate '${gate}' is not a Kconfig bool: ${gate_value}")
        endif()
        if(gate STREQUAL "CONFIG_ENABLE_MCU8080" AND gate_value STREQUAL "y" AND
           CONFIG_CHIP_CHOICE STREQUAL "wl83")
            message(FATAL_ERROR
                "CONFIG_ENABLE_MCU8080 is unsupported on WL83; the Jieli MCU8080 adapter "
                "is implemented only for AC79_DevKitBoard"
            )
        endif()
        if(gate_value STREQUAL "n")
            continue()
        endif()
    endif()
    if(source MATCHES "^src/chip/(wl82|wl83)/")
        set(source_chip "${CMAKE_MATCH_1}")
        if(NOT source_chip STREQUAL "${CONFIG_CHIP_CHOICE}")
            continue()
        endif()
    endif()
    list(APPEND JIELI_ADAPTER_SRC "${CMAKE_CURRENT_LIST_DIR}/${source}")
endforeach()

file(GLOB JIELI_ADAPTER_INCLUDE_ENTRIES LIST_DIRECTORIES true
    "${CMAKE_CURRENT_LIST_DIR}/include/*")
set(JIELI_ADAPTER_INCLUDE_DIRS)
foreach(include_entry IN LISTS JIELI_ADAPTER_INCLUDE_ENTRIES)
    if(IS_DIRECTORY "${include_entry}")
        list(APPEND JIELI_ADAPTER_INCLUDE_DIRS "${include_entry}")
    endif()
endforeach()
list(APPEND JIELI_ADAPTER_INCLUDE_DIRS "${CMAKE_CURRENT_LIST_DIR}/include/init/include")

add_library(${JIELI_ADAPTER_NAME} STATIC ${JIELI_ADAPTER_SRC})
target_compile_definitions(${JIELI_ADAPTER_NAME} PRIVATE
    "${JIELI_SELECTED_CHIP_DEFINE}=1"
)
jieli_lookup_evaluated_config("CONFIG_ENABLE_SPI" spi_value)
if(spi_value STREQUAL "y")
    target_compile_definitions(${JIELI_ADAPTER_NAME} PRIVATE CONFIG_ENABLE_SPI=1)
elseif(NOT spi_value STREQUAL "n")
    message(FATAL_ERROR "CONFIG_ENABLE_SPI is not a Kconfig bool: '${spi_value}'")
endif()
jieli_lookup_evaluated_config("CONFIG_ENABLE_MEDIA" media_value)
if(media_value STREQUAL "y")
    target_compile_definitions(${JIELI_ADAPTER_NAME} PRIVATE CONFIG_MEDIA_ENABLE=1)
elseif(NOT media_value STREQUAL "n")
    message(FATAL_ERROR "CONFIG_ENABLE_MEDIA is not a Kconfig bool: '${media_value}'")
endif()
if(CONFIG_CHIP_CHOICE STREQUAL "wl82" AND CONFIG_BOARD_CHOICE STREQUAL "AC79_DevKitBoard")
    target_compile_definitions(${JIELI_ADAPTER_NAME} PRIVATE JIELI_AC79_DEVKIT_BOARD=1)
endif()
target_include_directories(${JIELI_ADAPTER_NAME}
    PUBLIC
        ${CMAKE_CURRENT_LIST_DIR}/include
        ${JIELI_ADAPTER_INCLUDE_DIRS}
        ${CMAKE_CURRENT_LIST_DIR}/include/utilities/include
        ${TOP_SOURCE_DIR}/src/common/include
        ${JIELI_SDK_ROOT}/include_lib
        ${JIELI_SDK_ROOT}/include_lib/btstack
        ${JIELI_SDK_ROOT}/include_lib/btstack/le
        ${JIELI_SDK_ROOT}/include_lib/btctrler
        ${JIELI_SDK_ROOT}/include_lib/btctrler/port/${JIELI_CPU}
        ${JIELI_SDK_ROOT}/include_lib/net
        ${JIELI_SDK_ROOT}/include_lib/driver
        ${JIELI_SDK_ROOT}/include_lib/driver/device
        ${JIELI_SDK_ROOT}/include_lib/driver/cpu/${JIELI_CPU}
        ${JIELI_SDK_ROOT}/include_lib/system
        ${JIELI_SDK_ROOT}/include_lib/utils
        ${JIELI_SDK_ROOT}/include_lib/utils/syscfg
        ${JIELI_SDK_ROOT}/include_lib/utils/event
        ${JIELI_SDK_ROOT}/include_lib/net/lwip_2_2_0
        ${JIELI_SDK_ROOT}/include_lib/net/lwip_2_2_0/lwip/src/include
        ${JIELI_SDK_ROOT}/include_lib/net/lwip_2_2_0/lwip/src/include/compat
        ${JIELI_SDK_ROOT}/include_lib/net/lwip_2_2_0/lwip/port
)

# Implementation-private vendor trees (the chip's media/audio headers plus the
# demo app / wifi_camera dirs whose app_config.h chain those headers quote).
# They stay out of the PUBLIC set above: anything linking the adapter should
# see its public headers, not the vendor SDK's internals.
target_include_directories(${JIELI_ADAPTER_NAME} PRIVATE
    ${JIELI_ADAPTER_CHIP_INCLUDES}
)

# The board's audio_config.h carries the JIELI_AUDIO_* mic routing macros the
# wl83 capture path reads. The staged vendor Makefile force-includes it for
# this object (audio_profile.py per_file_flags); mirror that here. The board
# CMakeLists exports the header path as JIELI_BOARD_AUDIO_CONFIG.
# Missing it is not survivable: the capture path would compile against the
# fallback JIELI_AUDIO_MIC_CHANNEL_MAP -> AUDIO_ADC_MIC_CH (all channels),
# which trips the vendor's audio_adc_mic_open ch err check and reboots the
# board — so fail the configure step instead of silently degrading.
if(CONFIG_CHIP_CHOICE STREQUAL "wl83" AND NOT EXISTS "${JIELI_BOARD_AUDIO_CONFIG}")
    message(FATAL_ERROR
        "JIELI wl83 board did not export JIELI_BOARD_AUDIO_CONFIG: the wl83 "
        "capture path needs the board's audio_config.h mic routing macros")
endif()
if(JIELI_BOARD_AUDIO_CONFIG AND EXISTS "${JIELI_BOARD_AUDIO_CONFIG}")
    foreach(source IN LISTS JIELI_ADAPTER_SRC)
        if(source MATCHES "tkl_audio\\.c$")
            set_source_files_properties("${source}" PROPERTIES
                COMPILE_OPTIONS "-include;${JIELI_BOARD_AUDIO_CONFIG}"
                OBJECT_DEPENDS "${JIELI_BOARD_AUDIO_CONFIG}"
            )
        endif()
    endforeach()
endif()
