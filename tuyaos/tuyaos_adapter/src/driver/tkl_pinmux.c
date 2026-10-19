#include "tkl_pinmux.h"
#include "tuya_error_code.h"

#include "asm/cpu.h"
#include "gpio.h"

/* Peripheral pin muxing, as opposed to driving a pin as plain GPIO.
 *
 * Only wl83 has an API for this. Its gpio_function_t is a per-pin function
 * selector and gpio_enable_function()/gpio_disable_function() apply it:
 *
 *     gpio_enable_function(gpio, fn, io_init)   claim a pin for a peripheral
 *     gpio_disable_function(gpio, fn, io_uninit) hand it back
 *
 * wl82 has no equivalent. Its device/gpio.h exposes only UART-specific helpers
 * (gpio_set_uart0/1/2, gpio_uart_rx_input, gpio_output_channle), and each of
 * those takes a *channel* index rather than deriving one from the pin, so
 * choosing the argument needs a pin-to-channel table the SDK does not publish.
 * Guessing that table would put a wrong signal on a pin, so every request on
 * wl82 is refused and the rest of the peripheral drivers on that part keep
 * owning their own pin setup. That is the same shape as tkl_mipi_dsi, which is
 * wl83-only for the same reason.
 *
 * What is mapped below is only where the vendor enum member is the same thing
 * under the same name. Where the vendor has no member, or where the member is
 * ambiguous, the request is refused rather than approximated:
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

OPERATE_RET tkl_io_pinmux_config(TUYA_PIN_NAME_E pin, TUYA_PIN_FUNC_E pin_func)
{
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
    /* wl82: no generic pin-function API exists on this part. See the file
     * comment - the UART-only helpers need a channel index that cannot be
     * derived from the pin. */
    (void)pin_func;
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
