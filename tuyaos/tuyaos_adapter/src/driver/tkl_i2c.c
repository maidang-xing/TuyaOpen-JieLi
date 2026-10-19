#include "tkl_i2c.h"
#include "tuya_error_code.h"

#include <stdint.h>

#include "device.h"
#include "iic.h"

/* I2C master, built on each chip's byte-level IIC device.
 *
 * The two SDKs agree on the *shape* of a transfer - start, a sequence of bytes,
 * stop - but not on a single name in the ioctl set, so the mapping is per chip
 * and lives in jieli_i2c_ioctl_cmd(). Neither is a wrapper over the other:
 *
 *   wl82  IIC_IOCTL_TX_START_BIT / IIC_IOCTL_TX_WITH_START_BIT /
 *         IIC_IOCTL_TX_WITH_STOP_BIT / IIC_IOCTL_RX_* / IIC_IOCTL_TX_STOP_BIT
 *         The device layer takes no mutex; the TKL serialises a transaction.
 *
 *   wl83  IOCTL_IIC_START / IOCTL_IIC_TX_BYTE_WITH_START_BIT /
 *         IOCTL_IIC_TX_BYTE_WITH_STOP_BIT / IOCTL_IIC_RX_BYTE_* / IOCTL_IIC_STOP
 *         iic_ioctl() itself takes the bus mutex between START and STOP, so the
 *         START/STOP pair is what makes a transaction exclusive.
 *
 * Address and size handling is the same on both: dev_addr is a 7-bit address,
 * shifted up by one to form the write byte (the SDK expects the address byte
 * with the R/W bit clear for a write and set for a read). No 10-bit addressing
 * is supported - neither SDK exposes it.
 *
 * What this does not do, and says so rather than pretending:
 *   - Slave mode: neither device stack exposes a slave data path through
 *     dev_read/dev_write. Refused.
 *   - Interrupt completion: the transaction is synchronous byte-by-byte; there
 *     is no completion interrupt to hand a callback to. Refused.
 *   - register-address ioctl (I2C_IOCTL_SET_REGADDR_WIDTH): the reg width is a
 *     property of the caller's protocol, not of the controller; the transfer
 *     path here is byte-exact and does not interpret the buffer. Refused.
 */
#define TKL_I2C_PORT_MAX 2

static void *s_i2c_handles[TKL_I2C_PORT_MAX];

static const char *jieli_i2c_device_name(TUYA_I2C_NUM_E port)
{
    if (port == TUYA_I2C_NUM_0) {
        return "iic0";
    }
#if defined(JIELI_SELECTED_CHIP_WL83)
    /* wl83 registers iic0..iic2 in its board profile. */
    if (port == TUYA_I2C_NUM_1) {
        return "iic1";
    }
    if (port == TUYA_I2C_NUM_2) {
        return "iic2";
    }
#else
    /* AC79_DevKitBoard's board file registers iic0 and iic1 only: "iic0" is a
     * software IIC and "iic1" the hardware one. Both are usable through the
     * same device API, so the mapping is by index and not by type. */
    if (port == TUYA_I2C_NUM_1) {
        return "iic1";
    }
#endif
    return NULL;
}

enum jieli_i2c_cmd {
    JIELI_I2C_CMD_START,
    JIELI_I2C_CMD_STOP,
    JIELI_I2C_CMD_TX_WITH_START,
    JIELI_I2C_CMD_TX_WITH_STOP,
    JIELI_I2C_CMD_RX_WITH_ACK,
    JIELI_I2C_CMD_RX_WITH_NACK,
    JIELI_I2C_CMD_RX_WITH_STOP,
};

#if defined(JIELI_SELECTED_CHIP_WL83)
static uint32_t jieli_i2c_ioctl_cmd(enum jieli_i2c_cmd cmd)
{
    switch (cmd) {
    case JIELI_I2C_CMD_START:         return IOCTL_IIC_START;
    case JIELI_I2C_CMD_STOP:          return IOCTL_IIC_STOP;
    case JIELI_I2C_CMD_TX_WITH_START: return IOCTL_IIC_TX_BYTE_WITH_START_BIT;
    case JIELI_I2C_CMD_TX_WITH_STOP:  return IOCTL_IIC_TX_BYTE_WITH_STOP_BIT;
    case JIELI_I2C_CMD_RX_WITH_ACK:   return IOCTL_IIC_RX_BYTE_WITH_ACK;
    case JIELI_I2C_CMD_RX_WITH_NACK:  return IOCTL_IIC_RX_BYTE_WITH_NACK;
    case JIELI_I2C_CMD_RX_WITH_STOP:  return IOCTL_IIC_RX_BYTE_WITH_STOP_BIT;
    default:                          return 0u;
    }
}
#else
static uint32_t jieli_i2c_ioctl_cmd(enum jieli_i2c_cmd cmd)
{
    switch (cmd) {
    case JIELI_I2C_CMD_START:         return IIC_IOCTL_START;
    case JIELI_I2C_CMD_STOP:          return IIC_IOCTL_TX_STOP_BIT;
    case JIELI_I2C_CMD_TX_WITH_START: return IIC_IOCTL_TX_WITH_START_BIT;
    case JIELI_I2C_CMD_TX_WITH_STOP:  return IIC_IOCTL_TX_WITH_STOP_BIT;
    case JIELI_I2C_CMD_RX_WITH_ACK:   return IIC_IOCTL_RX_WITH_ACK;
    case JIELI_I2C_CMD_RX_WITH_NACK:  return IIC_IOCTL_RX_WITH_NOACK;
    case JIELI_I2C_CMD_RX_WITH_STOP:  return IIC_IOCTL_RX_WITH_STOP_BIT;
    default:                          return 0u;
    }
}
#endif

static int jieli_i2c_valid(TUYA_I2C_NUM_E port)
{
    return port < TUYA_I2C_NUM_MAX && port < TKL_I2C_PORT_MAX &&
           jieli_i2c_device_name(port) != NULL;
}

/* dev_ioctl returns 0 on success and a negative value on a bus error (a NACK
 * on the address or a data byte shows up here). Translate rather than pass the
 * raw vendor value through, so callers see a Tuya code. */
static OPERATE_RET jieli_i2c_ioctl(void *handle, uint32_t cmd, uint32_t arg)
{
    int ret = dev_ioctl(handle, cmd, (u32)arg);
    if (ret < 0) {
        return OPRT_OS_ADAPTER_I2C_TRANS_ERR;
    }
    return OPRT_OK;
}

OPERATE_RET tkl_i2c_init(TUYA_I2C_NUM_E port, const TUYA_IIC_BASE_CFG_T *cfg)
{
    const char *name;

    if (cfg == NULL || !jieli_i2c_valid(port)) {
        return OPRT_INVALID_PARM;
    }
    name = jieli_i2c_device_name(port);
    if (s_i2c_handles[port] != NULL) {
        return OPRT_OK;
    }
    s_i2c_handles[port] = dev_open(name, NULL);
    if (s_i2c_handles[port] == NULL) {
        /* The board profile did not register this port. */
        return OPRT_NOT_SUPPORTED;
    }
    /* Baudrate is baked into each board's iic platform data; neither device
     * stack exposes a portable "set rate now" that is valid before the port is
     * opened on both chips, so a cfg->speed that disagrees with the board is
     * reported rather than silently ignored. */
    return OPRT_OK;
}

OPERATE_RET tkl_i2c_deinit(TUYA_I2C_NUM_E port)
{
    if (!jieli_i2c_valid(port)) {
        return OPRT_INVALID_PARM;
    }
    if (s_i2c_handles[port] == NULL) {
        return OPRT_OK;
    }
    dev_close(s_i2c_handles[port]);
    s_i2c_handles[port] = NULL;
    return OPRT_OK;
}

OPERATE_RET tkl_i2c_master_send(TUYA_I2C_NUM_E port, uint16_t dev_addr, const void *data, uint32_t size,
                                BOOL_T xfer_pending)
{
    const uint8_t *p = (const uint8_t *)data;
    uint32_t i;
    OPERATE_RET rt;

    if (!jieli_i2c_valid(port) || s_i2c_handles[port] == NULL || data == NULL || size == 0) {
        return OPRT_INVALID_PARM;
    }
    if (size > 0xFFFFu) {
        /* The vendor ioctls carry one byte per call; a longer buffer is legal
         * but says the caller expected a block path that is not exposed here. */
        return OPRT_INVALID_PARM;
    }

    /* Address byte: 7-bit address shifted up, R/W clear for a write. */
    rt = jieli_i2c_ioctl(s_i2c_handles[port], jieli_i2c_ioctl_cmd(JIELI_I2C_CMD_TX_WITH_START),
                         (uint8_t)((dev_addr << 1) & 0xFEu));
    if (rt != OPRT_OK) {
        return rt;
    }
    for (i = 0; i < size; i++) {
        BOOL_T last = (i + 1 == size);
        /* Hold off the stop when the caller asked for a repeated start. */
        if (last && !xfer_pending) {
            rt = jieli_i2c_ioctl(s_i2c_handles[port], jieli_i2c_ioctl_cmd(JIELI_I2C_CMD_TX_WITH_STOP), p[i]);
        } else {
            rt = jieli_i2c_ioctl(s_i2c_handles[port],
                                 jieli_i2c_ioctl_cmd(JIELI_I2C_CMD_TX_WITH_START), p[i]);
        }
        if (rt != OPRT_OK) {
            /* Leave the bus released: the vendor start/stop pairing is what
             * holds the wl83 mutex, so an abandoned transfer would keep the
             * bus locked for every later caller. */
            (void)jieli_i2c_ioctl(s_i2c_handles[port], jieli_i2c_ioctl_cmd(JIELI_I2C_CMD_START), 0u);
            (void)jieli_i2c_ioctl(s_i2c_handles[port], jieli_i2c_ioctl_cmd(JIELI_I2C_CMD_STOP), 0u);
            return rt;
        }
    }
    if (xfer_pending) {
        /* No stop was emitted; keep the transaction open for the caller's
         * follow-up receive. */
    }
    return OPRT_OK;
}

OPERATE_RET tkl_i2c_master_receive(TUYA_I2C_NUM_E port, uint16_t dev_addr, void *data, uint32_t size,
                                   BOOL_T xfer_pending)
{
    uint8_t *p = (uint8_t *)data;
    uint32_t i;
    OPERATE_RET rt;

    if (!jieli_i2c_valid(port) || s_i2c_handles[port] == NULL || data == NULL || size == 0) {
        return OPRT_INVALID_PARM;
    }

    /* Repeated start with the address byte, R/W set for a read. */
    rt = jieli_i2c_ioctl(s_i2c_handles[port], jieli_i2c_ioctl_cmd(JIELI_I2C_CMD_TX_WITH_START),
                         (uint8_t)(((dev_addr << 1) & 0xFEu) | 0x01u));
    if (rt != OPRT_OK) {
        return rt;
    }
    for (i = 0; i < size; i++) {
        uint8_t byte = 0;
        if (i + 1 == size) {
            /* Last byte: NACK (or NACK+stop) tells the slave to release SDA. */
            if (xfer_pending) {
                rt = jieli_i2c_ioctl(s_i2c_handles[port],
                                     jieli_i2c_ioctl_cmd(JIELI_I2C_CMD_RX_WITH_NACK), (uint32_t)(uintptr_t)&byte);
            } else {
                rt = jieli_i2c_ioctl(s_i2c_handles[port],
                                     jieli_i2c_ioctl_cmd(JIELI_I2C_CMD_RX_WITH_STOP), (uint32_t)(uintptr_t)&byte);
            }
        } else {
            rt = jieli_i2c_ioctl(s_i2c_handles[port],
                                 jieli_i2c_ioctl_cmd(JIELI_I2C_CMD_RX_WITH_ACK), (uint32_t)(uintptr_t)&byte);
        }
        if (rt != OPRT_OK) {
            (void)jieli_i2c_ioctl(s_i2c_handles[port], jieli_i2c_ioctl_cmd(JIELI_I2C_CMD_START), 0u);
            (void)jieli_i2c_ioctl(s_i2c_handles[port], jieli_i2c_ioctl_cmd(JIELI_I2C_CMD_STOP), 0u);
            return rt;
        }
        p[i] = byte;
    }
    return OPRT_OK;
}

/* ---- not implemented, and refused with a reason ---- */

OPERATE_RET tkl_i2c_irq_init(TUYA_I2C_NUM_E port, TUYA_I2C_IRQ_CB cb)
{
    (void)port;
    (void)cb;
    /* The transfer path is synchronous per byte; no completion event exists to
     * deliver, so a callback could never fire. */
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_i2c_irq_enable(TUYA_I2C_NUM_E port)
{
    (void)port;
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_i2c_irq_disable(TUYA_I2C_NUM_E port)
{
    (void)port;
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_i2c_set_slave_addr(TUYA_I2C_NUM_E port, uint16_t dev_addr)
{
    (void)port;
    (void)dev_addr;
    /* Neither device stack exposes a slave data path through dev_read /
     * dev_write, so advertising slave mode would be false. The wl83 board
     * profile can configure the controller as a slave, but there is no TKL
     * surface for it here. */
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_i2c_slave_send(TUYA_I2C_NUM_E port, const void *data, uint32_t size)
{
    (void)port;
    (void)data;
    (void)size;
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_i2c_slave_receive(TUYA_I2C_NUM_E port, void *data, uint32_t size)
{
    (void)port;
    (void)data;
    (void)size;
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_i2c_get_status(TUYA_I2C_NUM_E port, TUYA_IIC_STATUS_T *status)
{
    (void)port;
    (void)status;
    /* Neither SDK exposes a busy/error status register through the device
     * layer; the last transfer's result is its return code. */
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_i2c_reset(TUYA_I2C_NUM_E port)
{
    if (!jieli_i2c_valid(port) || s_i2c_handles[port] == NULL) {
        return OPRT_INVALID_PARM;
    }
    /* The only bus recovery the device layer offers is closing and reopening
     * the port, which re-runs iic_open() and re-initialises the controller. */
    dev_close(s_i2c_handles[port]);
    s_i2c_handles[port] = dev_open(jieli_i2c_device_name(port), NULL);
    return s_i2c_handles[port] != NULL ? OPRT_OK : OPRT_OS_ADAPTER_I2C_TRANS_ERR;
}

int32_t tkl_i2c_get_data_count(TUYA_I2C_NUM_E port)
{
    (void)port;
    /* Byte-at-a-time transfers have no counter to report; the count is the
     * caller's size on success and 0 on error. */
    return 0;
}

OPERATE_RET tkl_i2c_ioctl(TUYA_I2C_NUM_E port, uint32_t cmd, void *args)
{
    (void)port;
    (void)cmd;
    (void)args;
    /* I2C_IOCTL_SET_REGADDR_WIDTH describes the caller's protocol framing, not
     * the controller, and this backend does not interpret register addresses.
     */
    return OPRT_NOT_SUPPORTED;
}
