#include "tkl_pinmux.h"
#include "tuya_error_code.h"

#include "asm/cpu.h"
#include "asm/iic.h"
#include "asm/spi.h"
#include "gpio.h"

/* Defined in tkl_gpio.c. It is the single per-chip table of which Tuya pin
 * numbers exist, and this file needs the same answer before handing a pin to
 * gpio_enable_function(), so it is declared rather than duplicated. */
extern int jieli_pin_exists(uint32_t pin);

/* Peripheral pin muxing, as opposed to driving a pin as plain GPIO.
 *
 * WL83 has a per-pin gpio_function_t selector. WL82 has different, narrower
 * mechanisms: UART0 selects one of four documented pairs, hardware IIC1 selects
 * one of four documented pairs, and SPI selects one of the board's spi_io[]
 * groups. These are group selectors, not a crossbar that accepts any GPIO.
 * Unsupported pins/functions are refused rather than approximated.
 *
 * WL83 mappings that cannot be derived from the vendor ABI remain refused:
 *
 *   SPI0/1/2 MISO, MOSI, CS
 *       The vendor names its four data lines DA0..DA3 and CLK. Nothing in the
 *       SDK says which of DA0..DA3 is MISO and which is MOSI, so the mapping
 *       cannot be derived - only CLK is unambiguous.
 *
 *   PWM0..5
 *       The vendor offers MCPWM0..3 as _H and _L (a polarity pair, not six
 *       channels) and TIMER0..5_PWM, which is reached through the output
 *       crossbar rather than gpio_enable_function. No 1:1 correspondence.
 *
 *   UART RTS/CTS and IIC3..5
 *       No vendor enum member exists at all.
 */
#if defined(JIELI_SELECTED_CHIP_WL83)
/* The vendor's own pin count, not Tuya's TUYA_IO_PIN_MAX: that is 0xFFFF, and
 * sizing the bookkeeping below by it would cost ~300KB of RAM. IO_MAX_NUM ends
 * just past IO_PORT_USB_DMB on this part. */
#define TKL_PINMUX_PIN_MAX IO_MAX_NUM

/* Set once a pin is claimed, so that TUYA_GPIO can hand the same function back
 * to gpio_disable_function - it needs the fn that was enabled, not just the
 * pin. */
static gpio_function_t s_pin_fn[TKL_PINMUX_PIN_MAX];
static u8 s_pin_claimed[TKL_PINMUX_PIN_MAX];

static int tkl_pinmux_lookup(TUYA_PIN_FUNC_E pin_func, gpio_function_t *fn)
{
    switch (pin_func) {
    /* IIC - wl83 has IIC0..2, Tuya names IIC0..5. */
    case TUYA_IIC0_SCL: *fn = GPIO_FUNC_IIC0_SCL; return 0;
    case TUYA_IIC0_SDA: *fn = GPIO_FUNC_IIC0_SDA; return 0;
    case TUYA_IIC1_SCL: *fn = GPIO_FUNC_IIC1_SCL; return 0;
    case TUYA_IIC1_SDA: *fn = GPIO_FUNC_IIC1_SDA; return 0;
    case TUYA_IIC2_SCL: *fn = GPIO_FUNC_IIC2_SCL; return 0;
    case TUYA_IIC2_SDA: *fn = GPIO_FUNC_IIC2_SDA; return 0;

    /* UART TX/RX - wl83 has UART0..4, Tuya names UART0..3. */
    case TUYA_UART0_TX: *fn = GPIO_FUNC_UART0_TX; return 0;
    case TUYA_UART0_RX: *fn = GPIO_FUNC_UART0_RX; return 0;
    case TUYA_UART1_TX: *fn = GPIO_FUNC_UART1_TX; return 0;
    case TUYA_UART1_RX: *fn = GPIO_FUNC_UART1_RX; return 0;
    case TUYA_UART2_TX: *fn = GPIO_FUNC_UART2_TX; return 0;
    case TUYA_UART2_RX: *fn = GPIO_FUNC_UART2_RX; return 0;
    case TUYA_UART3_TX: *fn = GPIO_FUNC_UART3_TX; return 0;
    case TUYA_UART3_RX: *fn = GPIO_FUNC_UART3_RX; return 0;

    /* SPI clock only - see the DA0..DA3 note above. */
    case TUYA_SPI0_CLK: *fn = GPIO_FUNC_SPI0_CLK; return 0;
    case TUYA_SPI1_CLK: *fn = GPIO_FUNC_SPI1_CLK; return 0;
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
#endif /* JIELI_SELECTED_CHIP_WL83 */

#if defined(JIELI_SELECTED_CHIP_WL82)
extern OPERATE_RET jieli_wl82_spi_pinmux_select(uint32_t spi_port, uint32_t group);

/* WL82 does not have WL83's per-pin function selector, but UART0 has four
 * documented TX/RX groups in the vendor SDK. Keep this mapping explicit: the
 * pinmux API takes one pin at a time, while gpio_set_uart0() switches the pair
 * as a unit. This intentionally rejects pins outside the SDK's groups. */
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

/* These are the vendor SDK's spi_io[] entries. spi0 group A is the flash bus,
 * and spi2 group B consumes the flash/SDRAM or USB pins, so neither is offered
 * as an application pinmux target. */
static const jieli_wl82_spi_pin_group_t s_wl82_spi_pin_groups[] = {
    {0u, 1u, IO_PORTC_00, IO_PORTC_04, IO_PORTC_03, IO_PORTC_05},
    {1u, 0u, IO_PORTB_05, IO_PORTB_07, IO_PORTB_06, UINT32_MAX},
    {1u, 1u, IO_PORTC_08, IO_PORTC_10, IO_PORTC_09, UINT32_MAX},
    {2u, 0u, IO_PORTH_02, IO_PORTH_01, IO_PORTH_00, UINT32_MAX},
    {2u, 2u, IO_PORTA_02, IO_PORTA_04, IO_PORTA_03, UINT32_MAX},
};

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

static int jieli_wl82_spi_group_for_pin(uint32_t pin, TUYA_PIN_FUNC_E pin_func, uint32_t *spi_port)
{
    int role = jieli_wl82_spi_role(pin_func, spi_port);
    unsigned int i;

    if (role < 0) {
        return -1;
    }
    for (i = 0; i < sizeof(s_wl82_spi_pin_groups) / sizeof(s_wl82_spi_pin_groups[0]); i++) {
        const jieli_wl82_spi_pin_group_t *group = &s_wl82_spi_pin_groups[i];
        uint32_t expected_pin;
        if (group->spi_port != *spi_port) {
            continue;
        }
        switch (role) {
        case 0: expected_pin = group->miso_pin; break;
        case 1: expected_pin = group->mosi_pin; break;
        default: expected_pin = group->clk_pin; break;
        }
        if (pin == expected_pin) {
            *spi_port = group->spi_port;
            return group->group;
        }
    }
    return -1;
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

/* tkl_uart_init() calls this after dev_open(), since the vendor driver applies
 * the board's default group while opening the device. */
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
#endif /* JIELI_SELECTED_CHIP_WL82 */

OPERATE_RET tkl_io_pinmux_config(TUYA_PIN_NAME_E pin, TUYA_PIN_FUNC_E pin_func)
{
    /* The upper bound alone is not enough: IO_MAX_NUM is derived from the last
     * defined macro, so the range contains holes. wl83 has no PC14/PC15, no
     * PF6..PF15 and no 98..111, and handing one of those to
     * gpio_enable_function() would mean writing an io-map field for a pin that
     * does not exist. */
    if (!jieli_pin_exists((uint32_t)pin)) {
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

        /* TUYA_GPIO is not a peripheral function - it means "stop driving this
         * pin as the peripheral and give it back as a plain GPIO". */
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

        if (tkl_pinmux_lookup(pin_func, &fn) != 0) {
            return OPRT_NOT_SUPPORTED;
        }
        if (gpio_enable_function((unsigned int)pin, fn, 1) != 0) {
            /* The pin cannot carry that function - the vendor refuses a pair
             * that is not in its io map. */
            return OPRT_NOT_SUPPORTED;
        }
        s_pin_fn[pin] = fn;
        s_pin_claimed[pin] = 1;
        return OPRT_OK;
    }
#else
    /* WL82 UART0 can switch only among the four paired routes documented by
     * asm/gpio.h. Use the same pair whether TX or RX is the first configured
     * pin, and reject all unsupported pin/function combinations. */
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
        s_wl82_iic1_pin_group = group;
        if (pin_func == TUYA_IIC1_SCL) {
            s_wl82_iic1_scl_pin = (uint32_t)pin;
        } else {
            s_wl82_iic1_sda_pin = (uint32_t)pin;
        }
        return jieli_wl82_iic1_pinmux_apply();
    }

    {
        uint32_t spi_port = 0u;
        if (jieli_wl82_spi_role(pin_func, &spi_port) >= 0) {
            int group = jieli_wl82_spi_group_for_pin((uint32_t)pin, pin_func, &spi_port);
            if (group < 0) {
                return OPRT_NOT_SUPPORTED;
            }
            {
                OPERATE_RET rt = jieli_wl82_spi_pinmux_select(spi_port, (uint32_t)group);
                if (rt == OPRT_OK) {
                    s_wl82_spi_route_group[spi_port] = group;
                }
                return rt;
            }
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
    /* This asks which peripheral channel a pin carries - a PWM id, an ADC
     * channel. Answering it needs a pin-to-channel table for the part, and
     * neither JieLi SDK publishes one: the channel is chosen by the peripheral
     * driver when it claims the pin, not by the pin's position. There is no
     * consumer of this call in any Jieli board or driver, so rather than
     * invent a numbering that would be wrong on the next board, report that it
     * is unavailable. */
    (void)pin;
    (void)pin_type;
    return -1;
}
