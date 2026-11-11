#include "tkl_pinmux.h"
#include "tuya_error_code.h"
#include "jieli_pinmux_internal.h"

#include "asm/cpu.h"
#include "asm/iic.h"
#include "asm/spi.h"
#include "gpio.h"
#include "system/spinlock.h"

/* Shared pin-existence check from tkl_gpio.c. */
extern int jieli_pin_exists(uint32_t pin);

/* Pinmux route state is shared by TKL configuration calls and the UART/IIC
 * reopen hooks. The SDK spinlock disables local IRQs and also serializes CPUs
 * on multicore builds, so the small state transitions cannot race. */
DEFINE_SPINLOCK(s_pinmux_lock);

static int tkl_pinmux_pin_exists(uint32_t pin, TUYA_PIN_FUNC_E pin_func)
{
    if (jieli_pin_exists(pin)) {
        return 1;
    }
#if defined(JIELI_SELECTED_CHIP_WL82)
    /* USB0 D+/D- are SDK-defined GPIO-capable pads only in the IIC1 route.
     * Keep them out of the general GPIO namespace and reject other USB pads. */
    if ((pin_func == TUYA_IIC1_SCL && pin == IO_PORT_USB_DPA) ||
        (pin_func == TUYA_IIC1_SDA && pin == IO_PORT_USB_DMA)) {
        return 1;
    }
#else
    (void)pin_func;
#endif
    return 0;
}

/* WL83 selects functions per pin. WL82 exposes fixed UART0/IIC1 pairs and SDK
 * SPI groups. Unsupported or ambiguous mappings return NOT_SUPPORTED. */
#if defined(JIELI_SELECTED_CHIP_WL83)
/* Use the vendor pin bound; Tuya's TUYA_IO_PIN_MAX is 0xFFFF. */
#define TKL_PINMUX_PIN_MAX IO_MAX_NUM

/* Save the function for TUYA_GPIO release. */
static gpio_function_t s_pin_fn[TKL_PINMUX_PIN_MAX];
static u8 s_pin_claimed[TKL_PINMUX_PIN_MAX];

/* SPI1 pins requested through tkl_io_pinmux_config(). Unlike the other
 * functions these are not enabled here: the vendor SPI driver muxes its own
 * pins inside dev_open(), so the request is held until tkl_spi_init() can put
 * it into the platform data first. */
static uint32_t s_spi1_clk_pin = UINT32_MAX;
static uint32_t s_spi1_do_pin = UINT32_MAX;
static uint32_t s_spi1_di_pin = UINT32_MAX;

void jieli_pinmux_spi1_pins_get(uint32_t *clk_pin, uint32_t *do_pin, uint32_t *di_pin)
{
    if (clk_pin != NULL) *clk_pin = s_spi1_clk_pin;
    if (do_pin != NULL) *do_pin = s_spi1_do_pin;
    if (di_pin != NULL) *di_pin = s_spi1_di_pin;
}

static int tkl_pinmux_lookup(TUYA_PIN_FUNC_E pin_func, gpio_function_t *fn)
{
    switch (pin_func) {
    /* IIC - wl83 has IIC0..2, Tuya names IIC0..5. */
    case TUYA_IIC0_SCL: *fn = GPIO_FUNC_IIC0_SCL; return 0;
    case TUYA_IIC0_SDA: *fn = GPIO_FUNC_IIC0_SDA; return 0;
    case TUYA_IIC1_SCL: *fn = GPIO_FUNC_IIC1_SCL; return 0;
    case TUYA_IIC1_SDA: *fn = GPIO_FUNC_IIC1_SDA; return 0;
    /* IIC2 uses the SDK software-IIC bit-bang device. Its pins come from
     * static board data, not gpio_enable_function(). */

    /* UART TX/RX routes exposed by this adapter. */
    case TUYA_UART0_TX: *fn = GPIO_FUNC_UART0_TX; return 0;
    case TUYA_UART0_RX: *fn = GPIO_FUNC_UART0_RX; return 0;
    /* WL83 exposes logical UART1 as SDK device uart2. The SDK UART examples
     * select a function from the numeric suffix of the device name. */
    case TUYA_UART1_TX: *fn = GPIO_FUNC_UART2_TX; return 0;
    case TUYA_UART1_RX: *fn = GPIO_FUNC_UART2_RX; return 0;
    /* This adapter registers only logical UART0 and UART1; unsupported TKL
     * ports must not claim a WL83 function without a matching device route. */

    /* SPI clock and data lines. The SDK names the data pins DA0/DA1 in the
     * same order as d2/d3 below them, so DA0 is DI and DA1 is DO. */
    case TUYA_SPI0_CLK: *fn = GPIO_FUNC_SPI0_CLK; return 0;
    case TUYA_SPI1_CLK: *fn = GPIO_FUNC_SPI1_CLK; return 0;
    case TUYA_SPI1_MISO: *fn = GPIO_FUNC_SPI1_DA0; return 0;
    case TUYA_SPI1_MOSI: *fn = GPIO_FUNC_SPI1_DA1; return 0;
    case TUYA_SPI2_CLK: *fn = GPIO_FUNC_SPI2_CLK; return 0;

    /* ADC - the vendor has a single GPADC function and the pin itself picks
     * the channel, so every Tuya ADC index maps to the same selector. */
    case TUYA_ADC0:
    case TUYA_ADC1:
    case TUYA_ADC2:
    case TUYA_ADC3:
    case TUYA_ADC4:
    case TUYA_ADC5:
        *fn = GPIO_FUNC_GPADC;
        return 0;

    default:
        return -1;
    }
}

static int tkl_uart_functions(TUYA_UART_NUM_E port, gpio_function_t *tx, gpio_function_t *rx)
{
    switch (port) {
    case TUYA_UART_NUM_0: *tx = GPIO_FUNC_UART0_TX; *rx = GPIO_FUNC_UART0_RX; return 0;
    case TUYA_UART_NUM_1: *tx = GPIO_FUNC_UART2_TX; *rx = GPIO_FUNC_UART2_RX; return 0;
    default: return -1;
    }
}

static int tkl_pinmux_wl83_reapply(gpio_function_t first, gpio_function_t second)
{
    uint32_t pin;
    for (pin = 0; pin < TKL_PINMUX_PIN_MAX; pin++) {
        if (s_pin_claimed[pin] && (s_pin_fn[pin] == first || s_pin_fn[pin] == second) &&
            gpio_enable_function(pin, s_pin_fn[pin], 1) != 0) {
            return -1;
        }
    }
    return 0;
}

static OPERATE_RET jieli_pinmux_uart_apply_unlocked(TUYA_UART_NUM_E port)
{
    gpio_function_t tx;
    gpio_function_t rx;
    if (port >= TUYA_UART_NUM_MAX) return OPRT_INVALID_PARM;
    if (tkl_uart_functions(port, &tx, &rx) != 0) return OPRT_NOT_SUPPORTED;
    return tkl_pinmux_wl83_reapply(tx, rx) == 0 ? OPRT_OK : OPRT_COM_ERROR;
}

static OPERATE_RET jieli_pinmux_iic_apply_unlocked(TUYA_I2C_NUM_E port)
{
    gpio_function_t scl;
    gpio_function_t sda;
    switch (port) {
    case TUYA_I2C_NUM_0: scl = GPIO_FUNC_IIC0_SCL; sda = GPIO_FUNC_IIC0_SDA; break;
    case TUYA_I2C_NUM_1: scl = GPIO_FUNC_IIC1_SCL; sda = GPIO_FUNC_IIC1_SDA; break;
    default:
        return port >= TUYA_I2C_NUM_MAX ? OPRT_INVALID_PARM : OPRT_NOT_SUPPORTED;
    }
    return tkl_pinmux_wl83_reapply(scl, sda) == 0 ? OPRT_OK : OPRT_COM_ERROR;
}
#endif /* JIELI_SELECTED_CHIP_WL83 */

#if defined(JIELI_SELECTED_CHIP_WL82)
/* WL82 UART0 switches among four SDK-defined TX/RX pairs. */
typedef struct {
    uint32_t tx_pin;
    uint32_t rx_pin;
} jieli_wl82_uart0_pin_group_t;

static const jieli_wl82_uart0_pin_group_t s_wl82_uart0_pin_groups[] = {
    {IO_PORTA_05, IO_PORTA_06},
    {IO_PORTB_01, IO_PORTB_02},
    {IO_PORTA_03, IO_PORTA_04},
    {IO_PORTH_00, IO_PORTH_01},
};

/* -1 means the board's staged default group remains in effect. */
static int s_wl82_uart0_pin_group = -1;
static uint32_t s_wl82_uart0_tx_pin = UINT32_MAX;
static uint32_t s_wl82_uart0_rx_pin = UINT32_MAX;

typedef struct {
    uint32_t scl_pin;
    uint32_t sda_pin;
    uint32_t io_map_value;
} jieli_wl82_iic1_pin_group_t;

static const jieli_wl82_iic1_pin_group_t s_wl82_iic1_pin_groups[] = {
    {IO_PORT_USB_DPA, IO_PORT_USB_DMA, 0u},
    {IO_PORTA_07, IO_PORTA_08, BIT(18)},
    {IO_PORTC_01, IO_PORTC_02, BIT(19)},
    {IO_PORTH_00, IO_PORTH_01, BIT(18) | BIT(19)},
};

static int s_wl82_iic1_pin_group = -1;
static uint32_t s_wl82_iic1_scl_pin = UINT32_MAX;
static uint32_t s_wl82_iic1_sda_pin = UINT32_MAX;
static int s_wl82_spi_route_group[3] = {-1, -1, -1};

typedef struct {
    uint8_t spi_port;
    uint8_t group;
    uint32_t miso_pin;
    uint32_t mosi_pin;
    uint32_t clk_pin;
    uint32_t cs_pin;
} jieli_wl82_spi_pin_group_t;

/* SDK spi_io[] routes. Reserved flash/SDRAM/USB groups are excluded. */
static const jieli_wl82_spi_pin_group_t s_wl82_spi_pin_groups[] = {
    {0u, 1u, IO_PORTC_00, IO_PORTC_04, IO_PORTC_03, IO_PORTC_05},
    {1u, 0u, IO_PORTB_05, IO_PORTB_07, IO_PORTB_06, UINT32_MAX},
    {1u, 1u, IO_PORTC_08, IO_PORTC_10, IO_PORTC_09, UINT32_MAX},
    {2u, 0u, IO_PORTH_02, IO_PORTH_01, IO_PORTH_00, UINT32_MAX},
    {2u, 2u, IO_PORTA_02, IO_PORTA_04, IO_PORTA_03, UINT32_MAX},
};

enum {
    JIELI_WL82_ROUTE_UART0,
    JIELI_WL82_ROUTE_IIC1,
    JIELI_WL82_ROUTE_SPI0,
    JIELI_WL82_ROUTE_SPI1,
    JIELI_WL82_ROUTE_SPI2,
};

static int jieli_wl82_route_group(uint32_t owner)
{
    if (owner == JIELI_WL82_ROUTE_UART0) return s_wl82_uart0_pin_group;
    if (owner == JIELI_WL82_ROUTE_IIC1) return s_wl82_iic1_pin_group;
    return s_wl82_spi_route_group[owner - JIELI_WL82_ROUTE_SPI0];
}

static int jieli_wl82_route_pins(uint32_t owner, int group, uint32_t *pins, uint32_t *count)
{
    unsigned int i;
    *count = 0u;
    if (owner == JIELI_WL82_ROUTE_UART0) {
        if (group < 0 || group >= (int)(sizeof(s_wl82_uart0_pin_groups) / sizeof(s_wl82_uart0_pin_groups[0]))) return -1;
        pins[(*count)++] = s_wl82_uart0_pin_groups[group].tx_pin;
        pins[(*count)++] = s_wl82_uart0_pin_groups[group].rx_pin;
        return 0;
    }
    if (owner == JIELI_WL82_ROUTE_IIC1) {
        if (group < 0 || group >= (int)(sizeof(s_wl82_iic1_pin_groups) / sizeof(s_wl82_iic1_pin_groups[0]))) return -1;
        pins[(*count)++] = s_wl82_iic1_pin_groups[group].scl_pin;
        pins[(*count)++] = s_wl82_iic1_pin_groups[group].sda_pin;
        return 0;
    }
    for (i = 0; i < sizeof(s_wl82_spi_pin_groups) / sizeof(s_wl82_spi_pin_groups[0]); i++) {
        const jieli_wl82_spi_pin_group_t *route = &s_wl82_spi_pin_groups[i];
        if (route->spi_port != owner - JIELI_WL82_ROUTE_SPI0 || route->group != group) continue;
        pins[(*count)++] = route->miso_pin;
        pins[(*count)++] = route->mosi_pin;
        pins[(*count)++] = route->clk_pin;
        if (route->cs_pin != UINT32_MAX) pins[(*count)++] = route->cs_pin;
        return 0;
    }
    return -1;
}

static int jieli_wl82_route_conflicts(uint32_t owner, int candidate_group)
{
    uint32_t candidate_pins[4], active_pins[4], candidate_count, active_count, i, j, other;
    if (jieli_wl82_route_pins(owner, candidate_group, candidate_pins, &candidate_count) != 0) return 1;
    for (other = JIELI_WL82_ROUTE_UART0; other <= JIELI_WL82_ROUTE_SPI2; other++) {
        int active_group;
        if (other == owner) continue;
        active_group = jieli_wl82_route_group(other);
        if (active_group < 0 ||
            jieli_wl82_route_pins(other, active_group, active_pins, &active_count) != 0) continue;
        for (i = 0; i < candidate_count; i++) {
            for (j = 0; j < active_count; j++) {
                if (candidate_pins[i] == active_pins[j]) return 1;
            }
        }
    }
    return 0;
}

static int jieli_wl82_iic1_group_for_pin(uint32_t pin, TUYA_PIN_FUNC_E pin_func)
{
    unsigned int i;

    for (i = 0; i < sizeof(s_wl82_iic1_pin_groups) / sizeof(s_wl82_iic1_pin_groups[0]); i++) {
        if ((pin_func == TUYA_IIC1_SCL && pin == s_wl82_iic1_pin_groups[i].scl_pin) ||
            (pin_func == TUYA_IIC1_SDA && pin == s_wl82_iic1_pin_groups[i].sda_pin)) {
            return (int)i;
        }
    }
    return -1;
}

static int jieli_wl82_spi_role(TUYA_PIN_FUNC_E pin_func, uint32_t *spi_port)
{
    switch (pin_func) {
    case TUYA_SPI0_MISO: *spi_port = 0u; return 0;
    case TUYA_SPI0_MOSI: *spi_port = 0u; return 1;
    case TUYA_SPI0_CLK:  *spi_port = 0u; return 2;
    case TUYA_SPI1_MISO: *spi_port = 1u; return 0;
    case TUYA_SPI1_MOSI: *spi_port = 1u; return 1;
    case TUYA_SPI1_CLK:  *spi_port = 1u; return 2;
    case TUYA_SPI2_MISO: *spi_port = 2u; return 0;
    case TUYA_SPI2_MOSI: *spi_port = 2u; return 1;
    case TUYA_SPI2_CLK:  *spi_port = 2u; return 2;
    default: return -1;
    }
}

static int jieli_wl82_uart0_group_for_pin(uint32_t pin, TUYA_PIN_FUNC_E pin_func)
{
    unsigned int i;

    if (pin_func != TUYA_UART0_TX && pin_func != TUYA_UART0_RX) {
        return -1;
    }
    for (i = 0; i < sizeof(s_wl82_uart0_pin_groups) / sizeof(s_wl82_uart0_pin_groups[0]); i++) {
        if ((pin_func == TUYA_UART0_TX && pin == s_wl82_uart0_pin_groups[i].tx_pin) ||
            (pin_func == TUYA_UART0_RX && pin == s_wl82_uart0_pin_groups[i].rx_pin)) {
            return (int)i;
        }
    }
    return -1;
}

/* Reapply the saved UART0 route after opening the device. */
OPERATE_RET jieli_wl82_uart0_pinmux_apply(void)
{
    if (s_wl82_uart0_pin_group < 0) {
        return OPRT_OK;
    }
    return gpio_set_uart0((unsigned int)s_wl82_uart0_pin_group) == 0 ? OPRT_OK : OPRT_COM_ERROR;
}

OPERATE_RET jieli_wl82_iic1_pinmux_apply(void)
{
    const jieli_wl82_iic1_pin_group_t *group;

    if (s_wl82_iic1_pin_group < 0) {
        return OPRT_OK;
    }
    group = &s_wl82_iic1_pin_groups[s_wl82_iic1_pin_group];
    JL_IOMAP->CON1 = (JL_IOMAP->CON1 & ~(BIT(18) | BIT(19))) | group->io_map_value;
    if (group->scl_pin == IO_PORT_USB_DPA && group->sda_pin == IO_PORT_USB_DMA) {
        usb_iomode(0, 1);
    }
    if (gpio_direction_output(group->scl_pin, 1) != 0 || gpio_direction_output(group->sda_pin, 1) != 0 ||
        gpio_set_pull_up(group->scl_pin, 1) != 0 || gpio_set_pull_up(group->sda_pin, 1) != 0 ||
        gpio_set_pull_down(group->scl_pin, 0) != 0 || gpio_set_pull_down(group->sda_pin, 0) != 0 ||
        gpio_set_hd(group->scl_pin, 0) != 0 || gpio_set_hd(group->sda_pin, 0) != 0 ||
        gpio_set_die(group->scl_pin, 1) != 0 || gpio_set_die(group->sda_pin, 1) != 0) {
        return OPRT_COM_ERROR;
    }
    return OPRT_OK;
}

static OPERATE_RET jieli_pinmux_uart_apply_unlocked(TUYA_UART_NUM_E port)
{
    if (port >= TUYA_UART_NUM_MAX) return OPRT_INVALID_PARM;
    if (port != TUYA_UART_NUM_0) return OPRT_OK;
    return jieli_wl82_uart0_pinmux_apply();
}

static OPERATE_RET jieli_pinmux_iic_apply_unlocked(TUYA_I2C_NUM_E port)
{
    if (port >= TUYA_I2C_NUM_MAX) return OPRT_INVALID_PARM;
    if (port != TUYA_I2C_NUM_1) return OPRT_OK;
    return jieli_wl82_iic1_pinmux_apply();
}
#endif /* JIELI_SELECTED_CHIP_WL82 */

OPERATE_RET jieli_pinmux_uart_apply(TUYA_UART_NUM_E port)
{
    OPERATE_RET rt;
    spin_lock(&s_pinmux_lock);
    rt = jieli_pinmux_uart_apply_unlocked(port);
    spin_unlock(&s_pinmux_lock);
    return rt;
}

OPERATE_RET jieli_pinmux_iic_apply(TUYA_I2C_NUM_E port)
{
    OPERATE_RET rt;
    spin_lock(&s_pinmux_lock);
    rt = jieli_pinmux_iic_apply_unlocked(port);
    spin_unlock(&s_pinmux_lock);
    return rt;
}

static OPERATE_RET tkl_io_pinmux_config_unlocked(TUYA_PIN_NAME_E pin, TUYA_PIN_FUNC_E pin_func)
{
    /* WL83's pin range contains holes; check the actual pin map. */
    if (!tkl_pinmux_pin_exists((uint32_t)pin, pin_func)) {
        return OPRT_INVALID_PARM;
    }
#if defined(JIELI_SELECTED_CHIP_WL83)
    if (pin >= TKL_PINMUX_PIN_MAX) {
        return OPRT_INVALID_PARM;
    }
#else
    if (pin >= TUYA_IO_PIN_MAX) {
        return OPRT_INVALID_PARM;
    }
#endif

#if defined(JIELI_SELECTED_CHIP_WL83)
    {
        gpio_function_t fn;

        /* TUYA_GPIO releases a previously claimed peripheral function. */
        if (pin_func == TUYA_GPIO) {
            if (!s_pin_claimed[pin]) {
                return OPRT_OK; /* nothing to release */
            }
            if (gpio_disable_function((unsigned int)pin, s_pin_fn[pin], 1) != 0) {
                return OPRT_COM_ERROR;
            }
            s_pin_claimed[pin] = 0;
            return OPRT_OK;
        }

        if (pin_func == TUYA_SPI1_CLK || pin_func == TUYA_SPI1_MISO || pin_func == TUYA_SPI1_MOSI) {
            if (pin_func == TUYA_SPI1_CLK) {
                s_spi1_clk_pin = (uint32_t)pin;
            } else if (pin_func == TUYA_SPI1_MISO) {
                s_spi1_di_pin = (uint32_t)pin;
            } else {
                s_spi1_do_pin = (uint32_t)pin;
            }
            return OPRT_OK;
        }

        if (tkl_pinmux_lookup(pin_func, &fn) != 0) {
            return OPRT_NOT_SUPPORTED;
        }
        if (s_pin_claimed[pin] && s_pin_fn[pin] != fn) {
            return OPRT_INVALID_PARM;
        }
        if (gpio_enable_function((unsigned int)pin, fn, 1) != 0) {
            /* The SDK rejected this pin/function combination. */
            return OPRT_NOT_SUPPORTED;
        }
        s_pin_fn[pin] = fn;
        s_pin_claimed[pin] = 1;
        return OPRT_OK;
    }
#else
    /* Select a documented UART0 pair and reject mismatched TX/RX pins. */
    if (pin_func == TUYA_UART0_TX || pin_func == TUYA_UART0_RX) {
        int channel = jieli_wl82_uart0_group_for_pin((uint32_t)pin, pin_func);
        if (channel < 0) {
            return OPRT_NOT_SUPPORTED;
        }
        if ((pin_func == TUYA_UART0_TX && s_wl82_uart0_rx_pin != UINT32_MAX &&
             s_wl82_uart0_pin_groups[channel].rx_pin != s_wl82_uart0_rx_pin) ||
            (pin_func == TUYA_UART0_RX && s_wl82_uart0_tx_pin != UINT32_MAX &&
             s_wl82_uart0_pin_groups[channel].tx_pin != s_wl82_uart0_tx_pin)) {
            return OPRT_INVALID_PARM;
        }
        if (jieli_wl82_route_conflicts(JIELI_WL82_ROUTE_UART0, channel)) {
            return OPRT_INVALID_PARM;
        }
        if (gpio_set_uart0((unsigned int)channel) != 0) {
            return OPRT_COM_ERROR;
        }
        s_wl82_uart0_pin_group = channel;
        if (pin_func == TUYA_UART0_TX) {
            s_wl82_uart0_tx_pin = (uint32_t)pin;
        } else {
            s_wl82_uart0_rx_pin = (uint32_t)pin;
        }
        return OPRT_OK;
    }

    if (pin_func == TUYA_IIC1_SCL || pin_func == TUYA_IIC1_SDA) {
        int group = jieli_wl82_iic1_group_for_pin((uint32_t)pin, pin_func);
        if (group < 0) {
            return OPRT_NOT_SUPPORTED;
        }
        if ((pin_func == TUYA_IIC1_SCL && s_wl82_iic1_sda_pin != UINT32_MAX &&
             s_wl82_iic1_pin_groups[group].sda_pin != s_wl82_iic1_sda_pin) ||
            (pin_func == TUYA_IIC1_SDA && s_wl82_iic1_scl_pin != UINT32_MAX &&
             s_wl82_iic1_pin_groups[group].scl_pin != s_wl82_iic1_scl_pin)) {
            return OPRT_INVALID_PARM;
        }
        if (jieli_wl82_route_conflicts(JIELI_WL82_ROUTE_IIC1, group)) {
            return OPRT_INVALID_PARM;
        }
        /* Commit the desired route before touching IOMAP/USB/GPIO state. The
         * SDK calls below can fail after partially changing hardware; retain
         * the pin reservation so another peripheral cannot claim those pads,
         * and let the post-open apply hook retry the full setup. */
        s_wl82_iic1_pin_group = group;
        if (pin_func == TUYA_IIC1_SCL) {
            s_wl82_iic1_scl_pin = (uint32_t)pin;
        } else {
            s_wl82_iic1_sda_pin = (uint32_t)pin;
        }
        return jieli_wl82_iic1_pinmux_apply();
    }

    {
        uint32_t spi_port;
        if (jieli_wl82_spi_role(pin_func, &spi_port) >= 0) {
            /* WL82 SPI is a fixed group table, not a per-pin mux: the pins come
             * from the group the platform data names, so a single pin request
             * cannot be honoured. Use jieli_wl82_spi_pinmux_select() instead. */
            return OPRT_NOT_SUPPORTED;
        }
    }

    if (pin_func == TUYA_GPIO) {
        if (s_wl82_uart0_pin_group >= 0 &&
            (pin == s_wl82_uart0_pin_groups[s_wl82_uart0_pin_group].tx_pin ||
             pin == s_wl82_uart0_pin_groups[s_wl82_uart0_pin_group].rx_pin)) {
            if (gpio_set_uart0((unsigned int)-1) != 0) {
                return OPRT_COM_ERROR;
            }
            s_wl82_uart0_pin_group = -1;
            s_wl82_uart0_tx_pin = UINT32_MAX;
            s_wl82_uart0_rx_pin = UINT32_MAX;
            return OPRT_OK;
        }
        if ((s_wl82_iic1_pin_group >= 0 &&
             (pin == s_wl82_iic1_pin_groups[s_wl82_iic1_pin_group].scl_pin ||
              pin == s_wl82_iic1_pin_groups[s_wl82_iic1_pin_group].sda_pin))) {
            /* WL82 has no generic peripheral-disable operation for HW IIC. */
            return OPRT_NOT_SUPPORTED;
        }
        {
            unsigned int i;
            for (i = 0; i < sizeof(s_wl82_spi_pin_groups) / sizeof(s_wl82_spi_pin_groups[0]); i++) {
                const jieli_wl82_spi_pin_group_t *group = &s_wl82_spi_pin_groups[i];
                if (group->spi_port < 3u && s_wl82_spi_route_group[group->spi_port] == group->group &&
                    (group->miso_pin == pin || group->mosi_pin == pin || group->clk_pin == pin ||
                     group->cs_pin == pin)) {
                    /* WL82 has no generic SPI peripheral-disable operation. */
                    return OPRT_NOT_SUPPORTED;
                }
            }
        }
        return OPRT_OK;
    }

    return OPRT_NOT_SUPPORTED;
#endif
}

OPERATE_RET tkl_io_pinmux_config(TUYA_PIN_NAME_E pin, TUYA_PIN_FUNC_E pin_func)
{
    OPERATE_RET rt;
    spin_lock(&s_pinmux_lock);
    rt = tkl_io_pinmux_config_unlocked(pin, pin_func);
    spin_unlock(&s_pinmux_lock);
    return rt;
}

OPERATE_RET tkl_multi_io_pinmux_config(TUYA_MUL_PIN_CFG_T *cfg, uint16_t num)
{
    uint16_t i;

    if (cfg == NULL || num == 0) {
        return OPRT_INVALID_PARM;
    }

    for (i = 0; i < num; i++) {
        OPERATE_RET rt = tkl_io_pinmux_config(cfg[i].pin, cfg[i].pin_func);
        if (rt != OPRT_OK) {
            /* Report which entry failed by its return code; the caller gets
             * the same code a single call would have produced. Entries before
             * it have already been applied, which matches how the other
             * platforms treat this call. */
            return rt;
        }
    }
    return OPRT_OK;
}

int32_t tkl_io_pin_to_func(uint32_t pin, TUYA_PIN_TYPE_E pin_type)
{
    if (pin_type == TUYA_IO_TYPE_ADC) {
#if defined(JIELI_SELECTED_CHIP_WL82)
        /* Order matches AD_CH_PA07..AD_CH_DP in the WL82 adc_api.h. The two
         * USB pins are ADC inputs in that SDK but are outside TKL GPIO IDs. */
        static const uint16_t adc_pins[] = {
            7u, 8u, 10u, 17u, 22u, 23u, 32u, 33u, 41u, 42u, 112u, 115u, 149u, 148u,
        };
#else
        /* Order matches adc_io2ch()'s WL83 adc_ch_io_table in adc_api.c. */
        static const uint16_t adc_pins[] = {
            48u, 49u, 54u, 55u, 56u, 57u, 58u, 68u, 69u, 70u, 71u, 72u, 73u, 45u, 96u, 112u,
        };
#endif
        unsigned int channel;
        for (channel = 0; channel < sizeof(adc_pins) / sizeof(adc_pins[0]); channel++) {
            if (pin == adc_pins[channel]) {
                /* The adapter exposes one ADC port; channel IDs are the
                 * sequence indices accepted by tkl_adc_read_single_channel. */
                return (int32_t)channel;
            }
        }
    }

    /* No verified generic pin-to-channel table exists for PWM on either chip. */
    return -1;
}
