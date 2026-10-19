#include "tkl_uart.h"
#include "tkl_semaphore.h"
#include "tkl_system.h"
#include "tkl_thread.h"
#include "tuya_error_code.h"

#include <stdint.h>

#include "device.h"
#include "uart.h"

#define TKL_UART_PORT_MAX 2

/* The vendor UART driver has no compile-time field for a receive buffer:
 * struct uart_platform_data carries pins, baud and flags but no buffer
 * address. Reception only works once a circular buffer is handed to the
 * driver with IOCTL_UART_SET_CIRCULAR_BUFF_*, and its RX interrupt writes
 * into that buffer. Opening a port without registering one leaves the ISR
 * with no destination, so the first received byte faults (observed as
 * "hmem access inv exception" inside uartx_isr). 32-byte alignment and the
 * 1 KB size follow the vendor's own uart_test.c example.
 */
#define TKL_UART_RX_BUFFER_SIZE 1024

static uint8_t s_uart_rx_buffer[TKL_UART_PORT_MAX][TKL_UART_RX_BUFFER_SIZE] __attribute__((aligned(32)));

enum jieli_uart_ioctl_cmd {
    JIELI_UART_IOCTL_SET_CIRCULAR_BUFF_ADDR,
    JIELI_UART_IOCTL_SET_CIRCULAR_BUFF_LENTH,
    JIELI_UART_IOCTL_SET_RECV_BLOCK,
    JIELI_UART_IOCTL_SET_BAUDRATE,
    JIELI_UART_IOCTL_SET_IRQ_EVENT_CB,
    JIELI_UART_IOCTL_START,
    JIELI_UART_IOCTL_GET_RECV_CNT,
};

static void *s_uart_handles[TKL_UART_PORT_MAX];

#if defined(JIELI_SELECTED_CHIP_WL83)
/* The vendor ISR must not call dev_read(): that path takes a mutex and the SDK
 * asserts with "os_mutex_pend: in_irq". So the ISR only posts a semaphore and a
 * task does the reading, which is also the vendor's own model - its
 * uart_irq_cb example only prints, while a task calls dev_read. One trampoline
 * per port is needed because the vendor callback carries no port identifier.
 * Handle both RX and OT: RX fires only when a burst reaches RXCNT, while typed
 * input arrives one character at a time and is reported only by the over-time
 * event.
 */
#define TKL_UART_RX_TASK_STACK 2048

static TUYA_UART_IRQ_CB s_uart_rx_cb[TKL_UART_PORT_MAX];
static TKL_SEM_HANDLE s_uart_rx_sem[TKL_UART_PORT_MAX];
static TKL_THREAD_HANDLE s_uart_rx_thread[TKL_UART_PORT_MAX];

static void jieli_uart_irq_dispatch(uint8_t port_id, uart_irq_event_t event)
{
    if (event != UART_IRQ_EVENT_RX && event != UART_IRQ_EVENT_OT) {
        return;
    }
    if (s_uart_rx_sem[port_id] != NULL) {
        (void)tkl_semaphore_post(s_uart_rx_sem[port_id]);
    }
}

static void jieli_uart_irq_event_cb_0(uart_irq_event_t event) { jieli_uart_irq_dispatch(0u, event); }
static void jieli_uart_irq_event_cb_1(uart_irq_event_t event) { jieli_uart_irq_dispatch(1u, event); }

static void (*const s_uart_irq_event_cb[TKL_UART_PORT_MAX])(uart_irq_event_t) = {
    jieli_uart_irq_event_cb_0,
    jieli_uart_irq_event_cb_1,
};

/* Runs the TAL receive callback in task context. TAL's callback drains the port
 * with tkl_uart_read(), which is why the port is opened non-blocking. */
static void jieli_uart_rx_task(void *arg)
{
    uint8_t port_id = (uint8_t)(uintptr_t)arg;

    for (;;) {
        if (tkl_semaphore_wait(s_uart_rx_sem[port_id], TKL_SEM_WAIT_FOREVER) != OPRT_OK) {
            continue;
        }
        if (s_uart_rx_cb[port_id] != NULL) {
            s_uart_rx_cb[port_id]((TUYA_UART_NUM_E)port_id);
        }
    }
}
#else /* JIELI_SELECTED_CHIP_WL82 */
/* wl82 has no receive interrupt-event callback, but TAL cannot receive without
 * a notification.
 *
 * Its uart.h defines only the UART_SET_* ioctl set - UART_IRQ_EVENT_* belongs
 * to the wl83 layout, and there is no "set irq event cb" on this part. The
 * SDK's own receive consumer (apps/common/key/uart_key.c) therefore polls
 * getbyte() from a 10 ms key scan rather than waiting on an interrupt.
 *
 * TAL cannot do that: its receive ring is filled only from the callback
 * registered here, and tal_uart_read() blocks on a semaphore that only that
 * callback posts. So this port needs a notifier of its own.
 *
 * The vendor driver still fills the circular buffer - it is handed over in
 * tkl_uart_init and UART_START enables reception - so the missing piece is only
 * "there is something to drain". UART_GET_RECV_CNT reports the count of bytes
 * received and not yet taken, which is exactly that. Its argument convention is
 * not obvious from the declaration (wl82 says _IOR(...,u32), wl83 says u32 *,
 * and their ioctl numbers differ), but the SDK's own caller settles it - the
 * awtk HAL does
 *
 *     dev_ioctl(fd, UART_GET_RECV_CNT, (u32)&len);
 *
 * so the count comes back through a pointer on wl82. This task polls it from
 * TAL context rather than an ISR, so the TAL callback's mutex use is not a
 * hazard here.
 *
 * Deliberately not used: UART_SET_RECV_TIMEOUT_CB. It looks like an idle-line
 * interrupt, but no SDK consumer exercises it, so its call context is unknown,
 * and calling a mutex-taking callback from an ISR is the exact failure the wl83
 * path was written to avoid.
 *
 * Two honest consequences: latency is one poll period rather than the
 * interrupt latency, and a burst that is emitted and fully consumed between
 * two polls is missed. In this design TAL only drains from inside the
 * callback, so data stays in the ring until the next poll.
 */
#define TKL_UART_RX_POLL_PERIOD_MS 10
#define TKL_UART_RX_TASK_STACK     2048

static TUYA_UART_IRQ_CB s_uart_rx_cb[TKL_UART_PORT_MAX];
static TKL_THREAD_HANDLE s_uart_rx_thread[TKL_UART_PORT_MAX];
static volatile uint8_t s_uart_rx_poll_active[TKL_UART_PORT_MAX];

/* jieli_uart_wl82_recv_cnt() and jieli_uart_poll_task() are defined after
 * jieli_uart_ioctl(), which they call. */
#endif

static const char *jieli_uart_device_name(TUYA_UART_NUM_E port_id)
{
    if (port_id == 0u) {
        return "uart0";
    }
#if defined(JIELI_SELECTED_CHIP_WL83)
    if (port_id == 1u) {
        return "uart2";
    }
#endif
    return NULL;
}

static uint32_t jieli_uart_ioctl(enum jieli_uart_ioctl_cmd cmd)
{
#if defined(JIELI_SELECTED_CHIP_WL83)
    switch (cmd) {
    case JIELI_UART_IOCTL_SET_CIRCULAR_BUFF_ADDR:
        return IOCTL_UART_SET_CIRCULAR_BUFF_ADDR;
    case JIELI_UART_IOCTL_SET_CIRCULAR_BUFF_LENTH:
        return IOCTL_UART_SET_CIRCULAR_BUFF_LENTH;
    case JIELI_UART_IOCTL_SET_RECV_BLOCK:
        return IOCTL_UART_SET_RECV_BLOCK;
    case JIELI_UART_IOCTL_SET_BAUDRATE:
        return IOCTL_UART_SET_BAUDRATE;
    case JIELI_UART_IOCTL_SET_IRQ_EVENT_CB:
        return IOCTL_UART_SET_IRQ_EVENT_CB;
    case JIELI_UART_IOCTL_START:
        return IOCTL_UART_START;
    case JIELI_UART_IOCTL_GET_RECV_CNT:
        return IOCTL_UART_GET_RECV_CNT;
    default:
        return 0u;
    }
#elif defined(JIELI_SELECTED_CHIP_WL82)
    switch (cmd) {
    case JIELI_UART_IOCTL_SET_CIRCULAR_BUFF_ADDR:
        return UART_SET_CIRCULAR_BUFF_ADDR;
    case JIELI_UART_IOCTL_SET_CIRCULAR_BUFF_LENTH:
        return UART_SET_CIRCULAR_BUFF_LENTH;
    case JIELI_UART_IOCTL_SET_RECV_BLOCK:
        return UART_SET_RECV_BLOCK;
    case JIELI_UART_IOCTL_SET_BAUDRATE:
        return UART_SET_BAUDRATE;
    case JIELI_UART_IOCTL_START:
        return UART_START;
    case JIELI_UART_IOCTL_GET_RECV_CNT:
        return UART_GET_RECV_CNT;
    default:
        return 0u;
    }
#else
#error "Jieli UART mapping requires a selected chip"
#endif
}

#if defined(JIELI_SELECTED_CHIP_WL82)
static int jieli_uart_wl82_recv_cnt(TUYA_UART_NUM_E port_id, uint32_t *cnt)
{
    if (s_uart_handles[port_id] == NULL) {
        return -1;
    }
    return dev_ioctl(s_uart_handles[port_id], jieli_uart_ioctl(JIELI_UART_IOCTL_GET_RECV_CNT),
                     (u32)(uintptr_t)cnt);
}

static void jieli_uart_poll_task(void *arg)
{
    uint8_t port_id = (uint8_t)(uintptr_t)arg;
    uint32_t last = 0;

    while (s_uart_rx_poll_active[port_id]) {
        uint32_t cnt = 0;

        if (jieli_uart_wl82_recv_cnt((TUYA_UART_NUM_E)port_id, &cnt) != 0) {
            /* The query is not answering; leave the notifier idle rather than
             * spin on it. */
            (void)tkl_system_sleep(TKL_UART_RX_POLL_PERIOD_MS);
            continue;
        }
        if (cnt != last && s_uart_rx_cb[port_id] != NULL) {
            s_uart_rx_cb[port_id]((TUYA_UART_NUM_E)port_id);
            /* TAL drained the ring in the callback; re-baseline instead of
             * assuming it is empty. */
            last = 0;
            if (jieli_uart_wl82_recv_cnt((TUYA_UART_NUM_E)port_id, &last) != 0) {
                last = 0;
            }
        } else {
            last = cnt;
        }
        (void)tkl_system_sleep(TKL_UART_RX_POLL_PERIOD_MS);
    }
    s_uart_rx_thread[port_id] = NULL;
}
#endif

OPERATE_RET tkl_uart_init(TUYA_UART_NUM_E port_id, TUYA_UART_BASE_CFG_T *cfg)
{
    const char *device_name;

    if (port_id >= TUYA_UART_NUM_MAX || port_id >= TKL_UART_PORT_MAX || cfg == NULL) {
        return OPRT_INVALID_PARM;
    }
    device_name = jieli_uart_device_name(port_id);
    if (device_name == NULL) {
        return OPRT_NOT_SUPPORTED;
    }
    if (s_uart_handles[port_id] != NULL) {
        return OPRT_OK;
    }

    s_uart_handles[port_id] = dev_open(device_name, NULL);
    if (s_uart_handles[port_id] != NULL &&
        (dev_ioctl(s_uart_handles[port_id], jieli_uart_ioctl(JIELI_UART_IOCTL_SET_CIRCULAR_BUFF_ADDR),
                   (u32)(uintptr_t)s_uart_rx_buffer[port_id]) != 0 ||
         dev_ioctl(s_uart_handles[port_id], jieli_uart_ioctl(JIELI_UART_IOCTL_SET_CIRCULAR_BUFF_LENTH),
                   (u32)sizeof(s_uart_rx_buffer[port_id])) != 0 ||
         /* Non-blocking: TAL's receive callback drains the port with
          * tkl_uart_read() until it returns no more bytes, so a blocking
          * dev_read would hang on the second iteration. TAL's own
          * tal_uart_read() blocks on its semaphore instead. */
         dev_ioctl(s_uart_handles[port_id], jieli_uart_ioctl(JIELI_UART_IOCTL_SET_RECV_BLOCK), 0u) != 0 ||
         dev_ioctl(s_uart_handles[port_id], jieli_uart_ioctl(JIELI_UART_IOCTL_SET_BAUDRATE), cfg->baudrate) != 0 ||
         dev_ioctl(s_uart_handles[port_id], jieli_uart_ioctl(JIELI_UART_IOCTL_START), 0u) != 0)) {
        dev_close(s_uart_handles[port_id]);
        s_uart_handles[port_id] = NULL;
    }
    if (s_uart_handles[port_id] == NULL) {
        return OPRT_OS_ADAPTER_UART_INIT_FAILED;
    }
    return OPRT_OK;
}

OPERATE_RET tkl_uart_deinit(TUYA_UART_NUM_E port_id)
{
    if (port_id >= TUYA_UART_NUM_MAX || port_id >= TKL_UART_PORT_MAX || s_uart_handles[port_id] == NULL) {
        return OPRT_INVALID_PARM;
    }
    dev_close(s_uart_handles[port_id]);
    s_uart_handles[port_id] = NULL;
    return OPRT_OK;
}

int tkl_uart_write(TUYA_UART_NUM_E port_id, void *buff, uint16_t len)
{
    if (port_id >= TUYA_UART_NUM_MAX || port_id >= TKL_UART_PORT_MAX || s_uart_handles[port_id] == NULL ||
        buff == NULL) {
        return OPRT_INVALID_PARM;
    }
    int ret = dev_write(s_uart_handles[port_id], buff, len);
    return ret < 0 ? OPRT_OS_ADAPTER_UART_SEND_FAILED : ret;
}

void tkl_uart_rx_irq_cb_reg(TUYA_UART_NUM_E port_id, TUYA_UART_IRQ_CB rx_cb)
{
    if (port_id >= TUYA_UART_NUM_MAX || port_id >= TKL_UART_PORT_MAX) {
        return;
    }
#if defined(JIELI_SELECTED_CHIP_WL83)
    /* TAL calls this after tkl_uart_init(), so the handle is already open and
     * START has already run; the vendor ioctl only stores the callback pointer.
     * The wl82 SDK exposes no interrupt-event callback at all, so receive
     * notification stays unwired on that chip. */
    s_uart_rx_cb[port_id] = rx_cb;
    if (rx_cb == NULL || s_uart_handles[port_id] == NULL) {
        return;
    }
    if (s_uart_rx_sem[port_id] == NULL &&
        tkl_semaphore_create_init(&s_uart_rx_sem[port_id], 0u, 1u) != OPRT_OK) {
        s_uart_rx_sem[port_id] = NULL;
        return;
    }
    if (s_uart_rx_thread[port_id] == NULL) {
        (void)tkl_thread_create(&s_uart_rx_thread[port_id], "tuya_uart_rx", TKL_UART_RX_TASK_STACK, 3u,
                                jieli_uart_rx_task, (void *)(uintptr_t)port_id);
    }
    (void)dev_ioctl(s_uart_handles[port_id], jieli_uart_ioctl(JIELI_UART_IOCTL_SET_IRQ_EVENT_CB),
                    (u32)(uintptr_t)s_uart_irq_event_cb[port_id]);
#else
    s_uart_rx_cb[port_id] = rx_cb;
    if (rx_cb == NULL || s_uart_handles[port_id] == NULL) {
        return;
    }
    if (s_uart_rx_thread[port_id] == NULL) {
        s_uart_rx_poll_active[port_id] = 1;
        if (tkl_thread_create(&s_uart_rx_thread[port_id], "tuya_uart_rx", TKL_UART_RX_TASK_STACK, 4u,
                              jieli_uart_poll_task, (void *)(uintptr_t)port_id) != OPRT_OK) {
            s_uart_rx_poll_active[port_id] = 0;
            s_uart_rx_thread[port_id] = NULL;
        }
    }
#endif
}

void tkl_uart_tx_irq_cb_reg(TUYA_UART_NUM_E port_id, TUYA_UART_IRQ_CB tx_cb)
{
    (void)port_id;
    (void)tx_cb;
}

int tkl_uart_read(TUYA_UART_NUM_E port_id, void *buff, uint16_t len)
{
    if (port_id >= TUYA_UART_NUM_MAX || port_id >= TKL_UART_PORT_MAX || s_uart_handles[port_id] == NULL ||
        buff == NULL) {
        return OPRT_INVALID_PARM;
    }
    int ret = dev_read(s_uart_handles[port_id], buff, len);
    return ret < 0 ? OPRT_OS_ADAPTER_UART_READ_FAILED : ret;
}

OPERATE_RET tkl_uart_set_tx_int(TUYA_UART_NUM_E port_id, BOOL_T enable)
{
    (void)port_id;
    (void)enable;
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_uart_set_rx_flowctrl(TUYA_UART_NUM_E port_id, BOOL_T enable)
{
    (void)port_id;
    (void)enable;
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_uart_wait_for_data(TUYA_UART_NUM_E port_id, int timeout_ms)
{
    (void)port_id;
    (void)timeout_ms;
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_uart_ioctl(TUYA_UART_NUM_E port_id, uint32_t cmd, void *arg)
{
    (void)port_id;
    (void)cmd;
    (void)arg;
    return OPRT_NOT_SUPPORTED;
}
