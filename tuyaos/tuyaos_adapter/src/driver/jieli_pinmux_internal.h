#ifndef JIELI_PINMUX_INTERNAL_H
#define JIELI_PINMUX_INTERNAL_H

#include "tuya_cloud_types.h"

/* Reapply a selected route after dev_open() installs board defaults. A logical
 * port with no custom route is a successful no-op. */
OPERATE_RET jieli_pinmux_uart_apply(TUYA_UART_NUM_E port);
OPERATE_RET jieli_pinmux_iic_apply(TUYA_I2C_NUM_E port);

#endif
