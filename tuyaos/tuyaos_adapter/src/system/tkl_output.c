#include "tkl_output.h"
#include "tuya_error_code.h"

#include <stdarg.h>
#include <stdio.h>

#if defined(__has_include)
#if __has_include("board_com_api.h")
#include "board_com_api.h"
#endif
#endif

#if (defined(ENABLE_JIELI_USB_CDC_LOG) && ENABLE_JIELI_USB_CDC_LOG) || \
    (defined(CONFIG_ENABLE_JIELI_USB_CDC_LOG) && CONFIG_ENABLE_JIELI_USB_CDC_LOG)
#define USE_JIELI_USB_CDC_LOG 1
extern unsigned int cdc_write_data(int usb_id, const unsigned char *buf, unsigned int len);
#else
#define USE_JIELI_USB_CDC_LOG 0
#endif

void tkl_log_output(const char *format, ...)
{
    va_list args;

#if USE_JIELI_USB_CDC_LOG
    char log_buf[512];
    va_start(args, format);
    int len = vsnprintf(log_buf, sizeof(log_buf), format, args);
    va_end(args);

    if (len > 0) {
        unsigned int write_len = (len < (int)sizeof(log_buf)) ? (unsigned int)len : (sizeof(log_buf) - 1);
        /* Which USB id the CDC class bound to depends on the board's port: the
         * high-speed port is id 1, the full-speed port id 0. The inactive id
         * has no CDC handle and returns 0, so writing to both is what the
         * vendor's own debug_user.c does and what keeps the log alive whichever
         * port a board wires up. */
        if (cdc_write_data(0, (const unsigned char *)log_buf, write_len) == 0u) {
            (void)cdc_write_data(1, (const unsigned char *)log_buf, write_len);
        }
    }
#else
    va_start(args, format);
    vprintf(format, args);
    va_end(args);
#endif
}

OPERATE_RET tkl_log_close(void)
{
    return OPRT_OK;
}

OPERATE_RET tkl_log_open(void)
{
    return OPRT_OK;
}
