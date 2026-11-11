#ifndef JIELI_PINMUX_INTERNAL_H
#define JIELI_PINMUX_INTERNAL_H

#include "tuya_cloud_types.h"

/* Reapply a selected route after dev_open() installs board defaults. A logical
 * port with no custom route is a successful no-op. */
OPERATE_RET jieli_pinmux_uart_apply(TUYA_UART_NUM_E port);
OPERATE_RET jieli_pinmux_iic_apply(TUYA_I2C_NUM_E port);

#if defined(JIELI_SELECTED_CHIP_WL83)
/* SPI pins the application asked for through tkl_io_pinmux_config(), per TKL
 * port. The vendor driver latches its pins at dev_open(), so tkl_spi_init()
 * reads them back and rewrites the platform data before opening the device. An
 * entry left at UINT32_MAX keeps the value the board staged. */
void jieli_pinmux_spi_pins_get(TUYA_SPI_NUM_E port, uint32_t *clk_pin, uint32_t *do_pin, uint32_t *di_pin);
#endif

#endif
