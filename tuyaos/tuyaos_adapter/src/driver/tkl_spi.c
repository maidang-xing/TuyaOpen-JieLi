#include "tkl_spi.h"
#include "tuya_error_code.h"

#include <stdint.h>

#include "device.h"
#include "asm/spi.h"

#if defined(JIELI_SELECTED_CHIP_WL83)
#include "asm/gpio.h"
#include "jieli_pinmux_internal.h"

/* The board states its SPI1 pins once, in its Kconfig; the platform stages
 * them into tuya_board_periph_config.h, which is force-included into every
 * object of the build. The defaults below only matter for a board that
 * declares nothing (or a build that bypasses the platform staging). */
#ifndef TUYA_BOARD_SPI1_CLK_PIN
#define TUYA_BOARD_SPI1_CLK_PIN IO_PORTC_00
#endif
#ifndef TUYA_BOARD_SPI1_DO_PIN
#define TUYA_BOARD_SPI1_DO_PIN IO_PORTC_01
#endif
#ifndef TUYA_BOARD_SPI1_DI_PIN
#define TUYA_BOARD_SPI1_DI_PIN IO_PORTC_02
#endif

/* The adapter owns SPI1: the platform strips the vendor board's spi1 node, so
 * this is the only registration of that name.
 *
 * The pins come from the board header and can be moved at run time with
 * tkl_io_pinmux_config(pin, TUYA_SPI1_CLK/MOSI/MISO) before the first
 * tkl_spi_init(): the vendor driver latches whatever is in this struct at
 * dev_open(), so the request has to be in place by then. Port A holds the IIC
 * adapter's pins, PB4/PB5 are this adapter's UART1 pads and PB8..PB13 carry
 * the MCPWM channels, which is why the board default is port C.
 *
 * CS is left unconnected: the vendor driver has no automatic chip-select on a
 * dev_write/dev_read, and the vendor master example drives CS as a plain GPIO
 * around each transfer. tkl_spi_send/tkl_spi_recv keep the same contract. */
static struct spi_platform_data s_spi1_pd = {
    .io = {
        .cs_pin  = -1,
        .di_pin  = TUYA_BOARD_SPI1_DI_PIN,
        .do_pin  = TUYA_BOARD_SPI1_DO_PIN,
        .clk_pin = TUYA_BOARD_SPI1_CLK_PIN,
        .d2_pin  = -1,
        .d3_pin  = -1,
    },
    .clk                = 1000000,
    .mode               = SPI_STD_MODE,
    .priority           = 6,
    .cpu_id             = 1,
    .hd_level           = 0,
    .attr               = SPI_SCLK_L_UPL_SMPH,
    .role               = SPI_MODE_MASTER,
    .sync_wait_timeout  = 1000,
    .irq                = IRQ_SPI1_IDX,
    .reg                = JL_SPI1,
};

/* Fold in whatever tkl_io_pinmux_config() recorded. Must run before dev_open()
 * because that is where the vendor driver muxes the pins. */
static void jieli_spi1_apply_requested_pins(void)
{
    uint32_t clk_pin;
    uint32_t do_pin;
    uint32_t di_pin;

    jieli_pinmux_spi1_pins_get(&clk_pin, &do_pin, &di_pin);
    if (clk_pin != UINT32_MAX) s_spi1_pd.io.clk_pin = (int)clk_pin;
    if (do_pin != UINT32_MAX) s_spi1_pd.io.do_pin = (int)do_pin;
    if (di_pin != UINT32_MAX) s_spi1_pd.io.di_pin = (int)di_pin;
}
#else

/* WL82 picks one pin group out of the SDK's fixed table, and the baud rate is
 * baked into the platform data at compile time (there is no SET_BAUDRATE
 * ioctl). Group 0 of SPI1 is PB6 CLK / PB7 DO / PB5 DI, the same route the
 * vendor DevKitBoard uses.
 *
 * asm/spi.h needs the pin macros and the register map from the chip headers,
 * and its own SPI_STD_MODE enum is commented out, so the mode comes from
 * device/spiflash.h's identical enum value. */
#include "asm/gpio.h"
#include "asm/WL82.h"
#include "asm/hwi.h"
#define SPI_STD_MODE 1

SPI1_PLATFORM_DATA_BEGIN(s_spi1_pd)
    .clk    = 1000000,
    .mode   = SPI_STD_MODE,
    .port   = 'B',
    .attr   = SPI_SCLK_L_UPH_SMPH | SPI_BIDIR_MODE,
SPI1_PLATFORM_DATA_END()
#define JIELI_WL82_SPI1_BAUDRATE 1000000u
#endif

REGISTER_DEVICE(spi1_node) = { "spi1", &spi_dev_ops, (void *)&s_spi1_pd };

/* Only SPI1 has a node. SPI0/SPI2 stay in the port table so a caller asking
 * for them gets OPRT_NOT_SUPPORTED from dev_open rather than a wrong bus. */
#define TKL_SPI_PORT_MAX 3

static void *s_spi_handles[TKL_SPI_PORT_MAX];
static uint8_t s_spi_lsb_first[TKL_SPI_PORT_MAX];

#if defined(JIELI_SELECTED_CHIP_WL82)
static int8_t s_wl82_spi_pin_group[TKL_SPI_PORT_MAX] = {-1, -1, -1};

OPERATE_RET jieli_wl82_spi_pinmux_select(uint32_t spi_port, uint32_t group)
{
    if (spi_port >= TKL_SPI_PORT_MAX ||
        (spi_port == 0u && group != 1u) ||
        (spi_port == 1u && group > 1u) ||
        (spi_port == 2u && group != 0u && group != 2u)) {
        return OPRT_NOT_SUPPORTED;
    }
    if (s_wl82_spi_pin_group[spi_port] >= 0 && s_wl82_spi_pin_group[spi_port] != (int8_t)group) {
        /* Pinmux is configured one signal at a time. Do not let a later signal
         * silently move the whole peripheral away from the earlier one. */
        return OPRT_INVALID_PARM;
    }
    if (s_spi_handles[spi_port] != NULL &&
        dev_ioctl(s_spi_handles[spi_port], IOCTL_SPI_SET_PORT, group) != 0) {
        return OPRT_COM_ERROR;
    }
    s_wl82_spi_pin_group[spi_port] = (int8_t)group;
    return OPRT_OK;
}
#endif

static const char *jieli_spi_device_name(TUYA_SPI_NUM_E port)
{
    switch (port) {
    case TUYA_SPI_NUM_0: return "spi0";
    case TUYA_SPI_NUM_1: return "spi1";
    case TUYA_SPI_NUM_2: return "spi2";
    default:             return NULL;
    }
}

static int jieli_spi_valid(TUYA_SPI_NUM_E port)
{
    return port < TUYA_SPI_NUM_MAX && port < TKL_SPI_PORT_MAX &&
           jieli_spi_device_name(port) != NULL;
}

/* The vendor path is byte oriented and clocks MSB first; the SDK offers no
 * bit-order register, and its own LSB demo reverses the bits in software. Do
 * the same here so TUYA_SPI_ORDER_LSB2MSB means what it says.
 *
 * Chunked so nothing has to be allocated: the caller's buffer is walked in
 * fixed-size pieces and reversed in place (receive) or into a scratch buffer
 * (send). */
#define JIELI_SPI_REVERSE_CHUNK 64u

static uint8_t jieli_spi_reverse_byte(uint8_t value)
{
    value = (uint8_t)(((value & 0xF0u) >> 4) | ((value & 0x0Fu) << 4));
    value = (uint8_t)(((value & 0xCCu) >> 2) | ((value & 0x33u) << 2));
    value = (uint8_t)(((value & 0xAAu) >> 1) | ((value & 0x55u) << 1));
    return value;
}

static void jieli_spi_reverse_bits(uint8_t *from, uint8_t *to, uint32_t size)
{
    uint32_t i;

    for (i = 0u; i < size; i++) {
        to[i] = jieli_spi_reverse_byte(from[i]);
    }
}

static OPERATE_RET jieli_spi_write(void *handle, uint8_t *data, uint32_t size, int lsb_first)
{
    uint8_t scratch[JIELI_SPI_REVERSE_CHUNK];
    uint32_t offset = 0u;

    while (offset < size) {
        uint32_t chunk = size - offset;
        uint8_t *out = data + offset;
        int ret;

        if (chunk > JIELI_SPI_REVERSE_CHUNK) {
            chunk = JIELI_SPI_REVERSE_CHUNK;
        }
        if (lsb_first) {
            jieli_spi_reverse_bits(out, scratch, chunk);
            out = scratch;
        }
        ret = dev_write(handle, out, chunk);
#if defined(JIELI_SELECTED_CHIP_WL83)
        /* dev_write returns the accepted byte count on WL83. */
        if (ret != (int)chunk) {
            return OPRT_OS_ADAPTER_SPI_BUS_ERR;
        }
#else
        /* The WL82 device layer returns 0 on success. */
        if (ret != 0 && ret != (int)chunk) {
            return OPRT_OS_ADAPTER_SPI_BUS_ERR;
        }
#endif
        offset += chunk;
    }
    return OPRT_OK;
}

static OPERATE_RET jieli_spi_read(void *handle, uint8_t *data, uint32_t size, int lsb_first)
{
    uint32_t offset = 0u;

    while (offset < size) {
        uint32_t chunk = size - offset;
        uint8_t *in = data + offset;
        int ret;

        if (chunk > JIELI_SPI_REVERSE_CHUNK) {
            chunk = JIELI_SPI_REVERSE_CHUNK;
        }
        ret = dev_read(handle, in, chunk);
#if defined(JIELI_SELECTED_CHIP_WL83)
        if (ret != (int)chunk) {
            return OPRT_OS_ADAPTER_SPI_BUS_ERR;
        }
#else
        if (ret != 0 && ret != (int)chunk) {
            return OPRT_OS_ADAPTER_SPI_BUS_ERR;
        }
#endif
        if (lsb_first) {
            uint32_t i;

            for (i = 0u; i < chunk; i++) {
                in[i] = jieli_spi_reverse_byte(in[i]);
            }
        }
        offset += chunk;
    }
    return OPRT_OK;
}

OPERATE_RET tkl_spi_init(TUYA_SPI_NUM_E port, const TUYA_SPI_BASE_CFG_T *cfg){
    if (cfg == NULL || !jieli_spi_valid(port)) {
        return OPRT_INVALID_PARM;
    }
    if (cfg->freq_hz == 0u) {
        return OPRT_INVALID_PARM;
    }
    if (cfg->role != TUYA_SPI_ROLE_MASTER) {
        /* Slave has no data path through the device layer. */
        return OPRT_NOT_SUPPORTED;
    }
    if (cfg->databits != TUYA_SPI_DATA_BIT8) {
        /* The vendor transfers are byte counts; there is no 16-bit frame. */
        return OPRT_NOT_SUPPORTED;
    }
    if (cfg->mode != TUYA_SPI_MODE0) {
        /* CPOL/CPHA live in the const platform data and no ioctl changes them
         * at runtime. SPI_SCLK_L_UPL_SMPH is clock-idle-low, sample on the
         * first edge, which is mode 0. */
        return OPRT_NOT_SUPPORTED;
    }
#if defined(JIELI_SELECTED_CHIP_WL82)
    if (cfg->freq_hz != JIELI_WL82_SPI1_BAUDRATE) {
        /* WL82 has no baud-rate ioctl: the rate is fixed in the staged
         * platform data, so a different request cannot be honoured. */
        return OPRT_NOT_SUPPORTED;
    }
#endif

    if (s_spi_handles[port] == NULL) {
#if defined(JIELI_SELECTED_CHIP_WL83)
        if (port == TUYA_SPI_NUM_1) {
            jieli_spi1_apply_requested_pins();
        }
#endif
        s_spi_handles[port] = dev_open((char *)jieli_spi_device_name(port), NULL);
        if (s_spi_handles[port] == NULL) {
            return OPRT_NOT_SUPPORTED;
        }
    }
#if defined(JIELI_SELECTED_CHIP_WL83)
    if (dev_ioctl(s_spi_handles[port], IOCTL_SPI_SET_BAUDRATE, cfg->freq_hz) < 0) {
        dev_close(s_spi_handles[port]);
        s_spi_handles[port] = NULL;
        return OPRT_OS_ADAPTER_SPI_INIT_FAILED;
    }
#endif
    s_spi_lsb_first[port] = (cfg->bitorder == TUYA_SPI_ORDER_LSB2MSB) ? 1u : 0u;
    return OPRT_OK;
}

OPERATE_RET tkl_spi_deinit(TUYA_SPI_NUM_E port)
{
    if (!jieli_spi_valid(port)) {
        return OPRT_INVALID_PARM;
    }
    if (s_spi_handles[port] == NULL) {
        return OPRT_OK;
    }
    dev_close(s_spi_handles[port]);
    s_spi_handles[port] = NULL;
    s_spi_lsb_first[port] = 0u;
    return OPRT_OK;
}

OPERATE_RET tkl_spi_send(TUYA_SPI_NUM_E port, void *data, uint32_t size)
{
    if (!jieli_spi_valid(port) || s_spi_handles[port] == NULL || data == NULL || size == 0u) {
        return OPRT_INVALID_PARM;
    }
    if (size > SPI_MAX_SIZE) {
        return OPRT_INVALID_PARM;
    }
    return jieli_spi_write(s_spi_handles[port], (uint8_t *)data, size, s_spi_lsb_first[port]);
}

OPERATE_RET tkl_spi_recv(TUYA_SPI_NUM_E port, void *data, uint32_t size)
{
    if (!jieli_spi_valid(port) || s_spi_handles[port] == NULL || data == NULL || size == 0u) {
        return OPRT_INVALID_PARM;
    }
    if (size > SPI_MAX_SIZE) {
        return OPRT_INVALID_PARM;
    }
    return jieli_spi_read(s_spi_handles[port], (uint8_t *)data, size, s_spi_lsb_first[port]);
}

OPERATE_RET tkl_spi_transfer(TUYA_SPI_NUM_E port, void *send_buf, void *receive_buf, uint32_t length)
{
    if (length == 0u || length > SPI_MAX_SIZE) {
        return OPRT_INVALID_PARM;
    }
    /* Full duplex is a separate vendor path (IOCTL_SPI_FDX_TR_DATA, defined on
     * WL83 only). It is used when both buffers are given; otherwise this falls
     * back to half duplex. The vendor path is MSB-first, so LSB-order callers
     * take the half-duplex path. */
#if defined(JIELI_SELECTED_CHIP_WL83)
    if (send_buf != NULL && receive_buf != NULL) {
        spi_fdx_data_t fdx;
        int ret;

        if (!jieli_spi_valid(port) || s_spi_handles[port] == NULL) {
            return OPRT_INVALID_PARM;
        }
        if (s_spi_lsb_first[port]) {
            OPERATE_RET rt = tkl_spi_send(port, send_buf, length);
            if (rt != OPRT_OK) {
                return rt;
            }
            return tkl_spi_recv(port, receive_buf, length);
        }
        fdx.send_buf = (u8 *)send_buf;
        fdx.recv_buf = (u8 *)receive_buf;
        fdx.len = length;
        fdx.callback = NULL;
        ret = dev_ioctl(s_spi_handles[port], IOCTL_SPI_FDX_TR_DATA, (u32)(uintptr_t)&fdx);
        return ret < 0 ? OPRT_OS_ADAPTER_SPI_BUS_ERR : OPRT_OK;
    }
#endif
    if (send_buf != NULL) {
        OPERATE_RET rt = tkl_spi_send(port, send_buf, length);
        if (rt != OPRT_OK || receive_buf == NULL) {
            return rt;
        }
    }
    if (receive_buf != NULL) {
        return tkl_spi_recv(port, receive_buf, length);
    }
    return OPRT_INVALID_PARM;
}

OPERATE_RET tkl_spi_transfer_with_length(TUYA_SPI_NUM_E port, void *send_buf, uint32_t send_len, void *receive_buf,
                                         uint32_t receive_len)
{
    if (send_buf != NULL && receive_buf != NULL &&
        (send_len != receive_len || send_len == 0u)) {
        /* Full duplex requires equal lengths; unequal halves must be issued
         * as separate operations so the chip never clocks more bits than it
         * has data for. */
        OPERATE_RET rt = tkl_spi_send(port, send_buf, send_len);
        if (rt != OPRT_OK) {
            return rt;
        }
        return tkl_spi_recv(port, receive_buf, receive_len);
    }
    if (send_buf == NULL && receive_buf == NULL) {
        return OPRT_INVALID_PARM;
    }
    if (send_buf != NULL && receive_buf != NULL) {
        return tkl_spi_transfer(port, send_buf, receive_buf, send_len);
    }
    if (send_buf != NULL) {
        return tkl_spi_send(port, send_buf, send_len);
    }
    return tkl_spi_recv(port, receive_buf, receive_len);
}

OPERATE_RET tkl_spi_abort_transfer(TUYA_SPI_NUM_E port)
{
    (void)port;
    /* Transfers are synchronous and are not interruptible from another
     * context; there is nothing left to abort by the time this returns. */
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_spi_get_status(TUYA_SPI_NUM_E port, TUYA_SPI_STATUS_T *status)
{
    (void)port;
    (void)status;
    /* No busy/error status register is surfaced through the device layer; the
     * result of the last call is its return code. */
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_spi_irq_init(TUYA_SPI_NUM_E port, TUYA_SPI_IRQ_CB cb)
{
    (void)port;
    (void)cb;
    /* Synchronous transfers have no completion event to deliver. */
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_spi_irq_enable(TUYA_SPI_NUM_E port)
{
    (void)port;
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_spi_irq_disable(TUYA_SPI_NUM_E port)
{
    (void)port;
    return OPRT_NOT_SUPPORTED;
}

int32_t tkl_spi_get_data_count(TUYA_SPI_NUM_E port)
{
    (void)port;
    /* Byte-exact synchronous calls: the count is the caller's size on success
     * and there is no in-flight transfer to report between calls. */
    return 0;
}

OPERATE_RET tkl_spi_ioctl(TUYA_SPI_NUM_E port, uint32_t cmd, void *args)
{
    (void)port;
    (void)cmd;
    (void)args;
    /* No portable ioctl is defined by the TKL contract that this backend can
     * honour on both chips; CS is owned by the board or by the caller as GPIO. */
    return OPRT_NOT_SUPPORTED;
}

uint32_t tkl_spi_get_max_dma_data_length(void)
{
    /* Synchronous path: the whole buffer goes through dev_write/dev_read, whose
     * limit is the vendor's SPI_MAX_SIZE. Reported so callers can chunk. */
    return SPI_MAX_SIZE;
}
