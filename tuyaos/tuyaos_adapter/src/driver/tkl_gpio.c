#include "tkl_gpio.h"
#include "tuya_error_code.h"

/* The vendor gpio.h is written in terms of u32/u8 but includes nothing itself,
 * so the base types have to be in scope first. tkl_audio.c gets them through
 * os_api.h and tkl_mipi_dsi.c through device.h; this file reaches the vendor
 * header directly, so it needs the header they all come from. Without it the
 * wl82 build fails on six "use of undeclared identifier 'u32'" errors, while
 * wl83 happens to compile because the type leaked in from another header. */
#include "asm/cpu.h"
#include "gpio.h"

/* The two chips reach the external interrupt through different drivers, and
 * neither is a thin wrapper over the other:
 *
 *   wl82  asm/port_waked_up.h
 *           port_wakeup_reg(event, gpio, edge, handler) / port_wakeup_unreg(hdl)
 *         One shared ISR on IRQ_PORT_IDX walks a fixed table of handlers. The
 *         handler takes no argument, and only a fixed set of "events" exists:
 *         EVENT_IO_0 and EVENT_IO_1 can be remapped to an arbitrary pin through
 *         the io map, the rest are tied to particular pins. PORT_WAKED_ID_NUM
 *         is 3, so at most three registrations are live at once, and the remap
 *         accepts only pins the driver lists in its own port_table (ports D and
 *         F are absent from it entirely).
 *
 *   wl83  asm/exti.h
 *           exti_init(gpio, edge, cb(priv, gpio), priv) / exti_uninit(index)
 *         A real per-pin external interrupt. The callback carries both the
 *         private pointer and the pin, so one dispatcher serves every pin.
 *
 * Both drivers expose exactly two trigger edges. Neither part has a
 * both-edges or a level-triggered mode, so TUYA_GPIO_IRQ_RISE_FALL, _LOW and
 * _HIGH are refused rather than approximated - see tkl_irq_edge().
 */
#if defined(JIELI_SELECTED_CHIP_WL82)
#include "asm/port_waked_up.h"
#elif defined(JIELI_SELECTED_CHIP_WL83)
#include "asm/exti.h"
#else
#error "JIELI tkl_gpio.c needs JIELI_SELECTED_CHIP_WL82 or _WL83 defined"
#endif

/* The JieLi pin namespace is already flat and matches Tuya's: the vendor header
 * defines IO_PORTA_00 as IO_GROUP_NUM * 0 + 0 with IO_GROUP_NUM == 16, so port A
 * occupies 0..15, B 16..31 and so on, and a Tuya pin index is the vendor pin
 * number directly. No lookup table is needed.
 *
 * The mapping only reaches as far as Tuya's enum does: TUYA_GPIO_NUM_MAX is 64,
 * so PORTE (64..79) and PORTF (80..95) cannot be addressed through this
 * contract. That includes PE11, which this board uses as the console RX. Those
 * ports stay reachable only from the vendor side; widening the contract is a
 * separate change and is not needed by the display path.
 */
#define TKL_GPIO_PIN_MAX 64

/* How many external interrupts the wl82 port driver can hold at once.
 * PORT_WAKED_ID_NUM is 3 in cpu/wl82/port_waked_up.c; a fourth registration
 * returns NULL there, which this file reports as OPRT_EXCEED_UPPER_LIMIT. */
#define TKL_GPIO_WL82_IRQ_MAX 3

typedef struct {
    TUYA_GPIO_IRQ_CB cb;
    void *arg;
    TUYA_GPIO_IRQ_E mode;
    void *hdl; /* wl82: the port_wakeup handle. wl83: the exti index as an int. */
} tkl_gpio_irq_ctx_t;

static tkl_gpio_irq_ctx_t s_irq[TKL_GPIO_PIN_MAX];

static int tkl_gpio_valid(TUYA_GPIO_NUM_E pin_id)
{
    return (pin_id < TUYA_GPIO_NUM_MAX) && (pin_id < TKL_GPIO_PIN_MAX);
}

/* Map a Tuya trigger mode onto the vendor's two-edge enum. Both chips spell the
 * two edges the same way (PORT_EDGE_E and trigger_edge_t both define
 * EDGE_POSITIVE and EDGE_NEGATIVE), so one mapping serves either. */
static int tkl_irq_edge(TUYA_GPIO_IRQ_E mode, int *edge)
{
    switch (mode) {
    case TUYA_GPIO_IRQ_RISE:
        *edge = EDGE_POSITIVE;
        return 0;
    case TUYA_GPIO_IRQ_FALL:
        *edge = EDGE_NEGATIVE;
        return 0;
    default:
        /* RISE_FALL, LOW and HIGH: no vendor mode expresses these. Faking a
         * level trigger with an edge would change when the callback fires, so
         * refuse instead of degrading silently. */
        return -1;
    }
}

static void tkl_gpio_apply_pull(u32 pin, TUYA_GPIO_MODE_E mode)
{
#if defined(JIELI_SELECTED_CHIP_WL82)
    /* wl82 takes a plain 1/0 per direction and fixes the resistor at 10K, so
     * the two calls are always made as a pair to keep the opposite leg off. */
    if (mode == TUYA_GPIO_PULLUP) {
        (void)gpio_set_pull_down(pin, 0);
        (void)gpio_set_pull_up(pin, 1);
    } else if (mode == TUYA_GPIO_PULLDOWN) {
        (void)gpio_set_pull_up(pin, 0);
        (void)gpio_set_pull_down(pin, 1);
    } else {
        (void)gpio_set_pull_up(pin, 0);
        (void)gpio_set_pull_down(pin, 0);
    }
#else
    /* wl83 selects a strength. Only the 10K taps exist on this part - the
     * header marks 100K and 1M as unsupported - so 10K is the one to use, with
     * the opposite leg explicitly disabled. */
    if (mode == TUYA_GPIO_PULLUP) {
        (void)gpio_set_pull_down(pin, GPIO_PULL_DOWN_DISABLE);
        (void)gpio_set_pull_up(pin, GPIO_PULL_UP_10K);
    } else if (mode == TUYA_GPIO_PULLDOWN) {
        (void)gpio_set_pull_up(pin, GPIO_PULL_UP_DISABLE);
        (void)gpio_set_pull_down(pin, GPIO_PULL_DOWN_10K);
    } else {
        (void)gpio_set_pull_up(pin, GPIO_PULL_UP_DISABLE);
        (void)gpio_set_pull_down(pin, GPIO_PULL_DOWN_DISABLE);
    }
#endif
}

#if defined(JIELI_SELECTED_CHIP_WL82)
/* The wl82 handler takes no argument, so which pin fired cannot be recovered
 * from the call. Each live registration therefore needs its own trampoline and
 * there can be at most TKL_GPIO_WL82_IRQ_MAX of them; the slot-to-pin mapping
 * is filled in at registration time. */
static TUYA_GPIO_NUM_E s_wl82_slot_pin[TKL_GPIO_WL82_IRQ_MAX];
/* A separate in-use flag rather than a sentinel pin number: 0 is IO_PORTA_00,
 * a perfectly valid pin, so a zero-initialised array cannot mean "free" and a
 * -1 sentinel would sail past the `pin_id < TKL_GPIO_PIN_MAX` guard below and
 * index s_irq[-1]. */
static u8 s_wl82_slot_used[TKL_GPIO_WL82_IRQ_MAX];

static void tkl_gpio_wl82_dispatch(int slot)
{
    TUYA_GPIO_NUM_E pin_id;

    if (!s_wl82_slot_used[slot]) {
        return;
    }
    pin_id = s_wl82_slot_pin[slot];
    if (pin_id < TKL_GPIO_PIN_MAX && s_irq[pin_id].cb != NULL) {
        s_irq[pin_id].cb(s_irq[pin_id].arg);
    }
}

static void tkl_gpio_wl82_isr_0(void) { tkl_gpio_wl82_dispatch(0); }
static void tkl_gpio_wl82_isr_1(void) { tkl_gpio_wl82_dispatch(1); }
static void tkl_gpio_wl82_isr_2(void) { tkl_gpio_wl82_dispatch(2); }

static void (*const s_wl82_isr[TKL_GPIO_WL82_IRQ_MAX])(void) = {
    tkl_gpio_wl82_isr_0,
    tkl_gpio_wl82_isr_1,
    tkl_gpio_wl82_isr_2,
};

static OPERATE_RET tkl_gpio_wl82_register(TUYA_GPIO_NUM_E pin_id, int edge)
{
    int slot;
    void *hdl;

    for (slot = 0; slot < TKL_GPIO_WL82_IRQ_MAX; slot++) {
        if (!s_wl82_slot_used[slot]) {
            break;
        }
    }
    if (slot == TKL_GPIO_WL82_IRQ_MAX) {
        /* Every vendor slot is taken. The driver would print its own
         * "PORT_WAKED_ID_NUM to less" and return NULL; catching it here keeps
         * the failure a code the caller can act on. */
        return OPRT_EXCEED_UPPER_LIMIT;
    }

    /* Note: port_wakeup_reg drives the pin to input and, for a plain io event,
     * sets the pull resistor to match the trigger edge (down for a rising edge,
     * up for a falling one). That is inside the vendor driver and cannot be
     * avoided from here, so a conflicting pull requested through tkl_gpio_init
     * is overridden by the vendor once the interrupt is enabled. */
    hdl = port_wakeup_reg(EVENT_IO_0, (unsigned int)pin_id, (PORT_EDGE_E)edge,
                          s_wl82_isr[slot]);
    if (hdl == NULL) {
        /* The pin is not in the driver's remap table - ports D and F are not
         * listed at all, and A/B/C/E/G/H only partially. */
        return OPRT_NOT_SUPPORTED;
    }

    s_wl82_slot_used[slot] = 1;
    s_wl82_slot_pin[slot] = pin_id;
    s_irq[pin_id].hdl = hdl;
    return OPRT_OK;
}

static void tkl_gpio_wl82_unregister(TUYA_GPIO_NUM_E pin_id)
{
    int slot;

    if (s_irq[pin_id].hdl == NULL) {
        return;
    }
    for (slot = 0; slot < TKL_GPIO_WL82_IRQ_MAX; slot++) {
        if (s_wl82_slot_used[slot] && s_wl82_slot_pin[slot] == pin_id) {
            s_wl82_slot_used[slot] = 0;
            break;
        }
    }
    port_wakeup_unreg(s_irq[pin_id].hdl);
    s_irq[pin_id].hdl = NULL;
}
#else /* JIELI_SELECTED_CHIP_WL83 */
/* wl83 hands the private pointer and the pin back to the callback, so a single
 * dispatcher covers every pin and the pin travels as the private pointer. */
static void tkl_gpio_wl83_dispatch(void *priv, unsigned int gpio)
{
    TUYA_GPIO_NUM_E pin_id = (TUYA_GPIO_NUM_E)(uintptr_t)priv;
    (void)gpio;
    if (pin_id < TKL_GPIO_PIN_MAX && s_irq[pin_id].cb != NULL) {
        s_irq[pin_id].cb(s_irq[pin_id].arg);
    }
}

static OPERATE_RET tkl_gpio_wl83_register(TUYA_GPIO_NUM_E pin_id, int edge)
{
    int index = exti_init((unsigned int)pin_id, (trigger_edge_t)edge,
                          tkl_gpio_wl83_dispatch, (void *)(uintptr_t)pin_id);
    if (index < 0) {
        /* exti_init refuses a pin with no external-interrupt capability. */
        return OPRT_NOT_SUPPORTED;
    }
    s_irq[pin_id].hdl = (void *)(intptr_t)index;
    return OPRT_OK;
}

static void tkl_gpio_wl83_unregister(TUYA_GPIO_NUM_E pin_id)
{
    if (s_irq[pin_id].hdl == NULL) {
        return;
    }
    (void)exti_uninit((int)(intptr_t)s_irq[pin_id].hdl);
    s_irq[pin_id].hdl = NULL;
}
#endif

OPERATE_RET tkl_gpio_init(TUYA_GPIO_NUM_E pin_id, const TUYA_GPIO_BASE_CFG_T *cfg)
{
    if (!tkl_gpio_valid(pin_id) || cfg == NULL) {
        return OPRT_INVALID_PARM;
    }

    /* Check the mode before touching the pin. The vendor API has no open-drain
     * setting - gpio_set_hd() selects a drive strength, not an output type - so
     * an open-drain request cannot be honoured. Driving it push-pull instead
     * would leave the line actively high where the caller expected it released,
     * which on a shared line is a conflict rather than a degradation. Refuse it
     * and leave the pin as it was. */
    if (cfg->direct == TUYA_GPIO_OUTPUT &&
        (cfg->mode == TUYA_GPIO_OPENDRAIN || cfg->mode == TUYA_GPIO_OPENDRAIN_PULLUP)) {
        return OPRT_NOT_SUPPORTED;
    }

    /* Digital function: the analogue alternative (die = 0) is only meaningful
     * for pins routed to the ADC, which this contract does not cover. */
    (void)gpio_set_die((u32)pin_id, 1);

    if (cfg->direct == TUYA_GPIO_OUTPUT) {
        (void)gpio_direction_output((u32)pin_id,
                                    cfg->level == TUYA_GPIO_LEVEL_HIGH ? 1 : 0);
        return OPRT_OK;
    }

    (void)gpio_direction_input((u32)pin_id);

    /* Pull selection is mapped for both chips now: wl82 has a fixed 10K up/down
     * and wl83 picks the 10K tap. A mode with no pull (HIGH_IMPEDANCE,
     * FLOATING) clears both legs. */
    tkl_gpio_apply_pull((u32)pin_id, cfg->mode);
    return OPRT_OK;
}

OPERATE_RET tkl_gpio_deinit(TUYA_GPIO_NUM_E pin_id)
{
    if (!tkl_gpio_valid(pin_id)) {
        return OPRT_INVALID_PARM;
    }
    /* Drop any interrupt first: leaving the vendor handler pointing at a pin
     * whose callback state is about to be cleared would call into stale state.
     * tkl_gpio_irq_disable is a no-op when nothing is registered. */
    (void)tkl_gpio_irq_disable(pin_id);
    s_irq[pin_id].cb = NULL;
    s_irq[pin_id].arg = NULL;

    /* Leave the pin as a floating input rather than holding whatever level it
     * was last driven to. */
    (void)gpio_direction_input((u32)pin_id);
    tkl_gpio_apply_pull((u32)pin_id, TUYA_GPIO_FLOATING);
    return OPRT_OK;
}

OPERATE_RET tkl_gpio_write(TUYA_GPIO_NUM_E pin_id, TUYA_GPIO_LEVEL_E level)
{
    if (!tkl_gpio_valid(pin_id)) {
        return OPRT_INVALID_PARM;
    }
    if (level != TUYA_GPIO_LEVEL_LOW && level != TUYA_GPIO_LEVEL_HIGH) {
        return OPRT_INVALID_PARM;
    }
    (void)gpio_direction_output((u32)pin_id, level == TUYA_GPIO_LEVEL_HIGH ? 1 : 0);
    return OPRT_OK;
}

OPERATE_RET tkl_gpio_read(TUYA_GPIO_NUM_E pin_id, TUYA_GPIO_LEVEL_E *level)
{
    if (!tkl_gpio_valid(pin_id) || level == NULL) {
        return OPRT_INVALID_PARM;
    }
    *level = gpio_read((u32)pin_id) ? TUYA_GPIO_LEVEL_HIGH : TUYA_GPIO_LEVEL_LOW;
    return OPRT_OK;
}

OPERATE_RET tkl_gpio_irq_init(TUYA_GPIO_NUM_E pin_id, const TUYA_GPIO_IRQ_T *cfg)
{
    int edge;

    if (!tkl_gpio_valid(pin_id) || cfg == NULL || cfg->cb == NULL) {
        return OPRT_INVALID_PARM;
    }
    if (tkl_irq_edge(cfg->mode, &edge) != 0) {
        return OPRT_NOT_SUPPORTED;
    }

    /* Per the contract this arms nothing: it records what to register when the
     * caller enables the interrupt. Nothing is written to the vendor driver
     * here, which is also what keeps the "init does not enable" promise on
     * hardware where registering and enabling are the same operation. */
    s_irq[pin_id].cb = cfg->cb;
    s_irq[pin_id].arg = cfg->arg;
    s_irq[pin_id].mode = cfg->mode;
    s_irq[pin_id].hdl = NULL;
    return OPRT_OK;
}

OPERATE_RET tkl_gpio_irq_enable(TUYA_GPIO_NUM_E pin_id)
{
    int edge;

    if (!tkl_gpio_valid(pin_id)) {
        return OPRT_INVALID_PARM;
    }
    if (s_irq[pin_id].cb == NULL) {
        return OPRT_INVALID_PARM;
    }
    if (s_irq[pin_id].hdl != NULL) {
        return OPRT_OK; /* already armed */
    }
    if (tkl_irq_edge(s_irq[pin_id].mode, &edge) != 0) {
        return OPRT_NOT_SUPPORTED;
    }

#if defined(JIELI_SELECTED_CHIP_WL82)
    return tkl_gpio_wl82_register(pin_id, edge);
#else
    return tkl_gpio_wl83_register(pin_id, edge);
#endif
}

OPERATE_RET tkl_gpio_irq_disable(TUYA_GPIO_NUM_E pin_id)
{
    if (!tkl_gpio_valid(pin_id)) {
        return OPRT_INVALID_PARM;
    }
    /* Both vendor drivers make "register" and "enable" the same act, so the
     * only way to stop the interrupt is to hand the registration back. A later
     * tkl_gpio_irq_enable re-registers from the saved configuration. */
#if defined(JIELI_SELECTED_CHIP_WL82)
    tkl_gpio_wl82_unregister(pin_id);
#else
    tkl_gpio_wl83_unregister(pin_id);
#endif
    return OPRT_OK;
}
