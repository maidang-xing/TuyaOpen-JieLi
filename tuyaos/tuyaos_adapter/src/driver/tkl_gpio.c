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
 *         the io map, the rest are tied to particular pins. Since each of those
 *         two owns its own remap field, only TWO arbitrary-pin interrupts can be
 *         live at once; the remap also accepts only pins the driver lists in
 *         its own port_table (ports D and F are absent from it entirely).
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
 * TUYA_GPIO_NUM_MAX is 128, which reaches ports A..H on wl82 (0..127) and, on
 * wl83, ports A..F plus the two sparse USB pairs. PE11 - this board's console
 * RX - is pin 75 and is now addressable through the contract.
 *
 * Which of those numbers is a real pin is a per-chip question and is answered
 * by jieli_pin_exists(): wl83's PORTC is 14 bits wide and PORTF 6, and wl82 has
 * no port I register block at all. The bound here is only the contract's.
 */
#define TKL_GPIO_PIN_MAX 128

/* How many external interrupts this backend can hold at once.
 *
 * The vendor table has PORT_WAKED_ID_NUM == 3 slots, but only EVENT_IO_0 and
 * EVENT_IO_1 can be remapped to an arbitrary pin - and each of those owns a
 * single io-map field, so two pins cannot share one. That makes two the real
 * ceiling for general-purpose pins; the third vendor slot is only useful for
 * its fixed-pin events, which this contract does not hand out. A third request
 * returns OPRT_EXCEED_UPPER_LIMIT rather than silently colliding. */
#define TKL_GPIO_WL82_IRQ_MAX 2

typedef struct {
    TUYA_GPIO_IRQ_CB cb;
    void *arg;
    TUYA_GPIO_IRQ_E mode;
    void *hdl;   /* wl82: the port_wakeup handle. wl83: unused. */
    int exti;    /* wl83: the exti index, which may legitimately be 0. */
    u8 armed;    /* registration is live; NOT deriveable from the values above */
} tkl_gpio_irq_ctx_t;

static tkl_gpio_irq_ctx_t s_irq[TKL_GPIO_PIN_MAX];

/* The vendor pin space is 16 pins per port letter; both chips use that width. */
#define JIELI_PORT_WIDTH 16u

/* Which of the Tuya pin numbers actually exist on this chip.
 *
 * The vendor names a pin IO_PORT<port>_<bit> and defines it arithmetically -
 * IO_PORTA_07 is (16 * 0 + 7) - which is the same flat layout TUYA_GPIO_NUM_E
 * uses, so the number needs no translation. What does need a table is the
 * population, and it is not uniform (all from the chip's asm/gpio.h):
 *
 *   wl82  WL82.h defines JL_PORTA..JL_PORTH, 16 bits each. asm/gpio.h also
 *         names IO_PORTI_00..15 and derives IO_MAX_NUM (144) from them, but no
 *         JL_PORTI base exists, so port I is not offered.
 *
 *   wl83  PORTA/B/D/E are 0xffff, PORTC is 0x3fff and PORTF is 0x003f, so
 *         PC14/PC15 and PF6..PF15 are not pins. The USB pairs are sparse at
 *         96/97 and 112/113; R and V sit above IO_MAX_NUM.
 *
 * A port mask describes the GPIO controller's width, not whether a package
 * bonds the pin out - electrical usability is still a schematic question.
 *
 * tkl_pinmux.c uses this too, through the extern declaration there, so the
 * table has exactly one definition.
 */
int jieli_pin_exists(uint32_t pin)
{
#if defined(JIELI_SELECTED_CHIP_WL83)
    uint32_t port = pin / JIELI_PORT_WIDTH;
    uint32_t bit = pin % JIELI_PORT_WIDTH;

    switch (port) {
    case 0: /* A */
    case 1: /* B */
    case 3: /* D */
    case 4: /* E */
        return 1;
    case 2: /* C: PC0..PC13 */
        return bit < 14u;
    case 5: /* F: PF0..PF5 */
        return bit < 6u;
    case 6: /* FUSB: only DPA/DMA are defined */
        return bit < 2u;
    case 7: /* HUSB: only DPB/DMB are defined */
        return bit < 2u;
    default:
        return 0;
    }
#else /* JIELI_SELECTED_CHIP_WL82 */
    return pin < 128u;
#endif
}

static int tkl_gpio_valid(TUYA_GPIO_NUM_E pin_id)
{
    /* Range alone is not enough: the chips do not populate every bit of every
     * port. wl83's PORTC is 14 bits wide, so PC14/PC15 (46, 47) are not pins -
     * without this check tkl_gpio_init() would report OPRT_OK for a pin that
     * cannot work. */
    return (pin_id < TUYA_GPIO_NUM_MAX) && (pin_id < TKL_GPIO_PIN_MAX) &&
           jieli_pin_exists((uint32_t)pin_id);
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

static void (*const s_wl82_isr[TKL_GPIO_WL82_IRQ_MAX])(void) = {
    tkl_gpio_wl82_isr_0,
    tkl_gpio_wl82_isr_1,
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

    /* Publish the slot before registering: port_wakeup_reg enables the event
     * as part of the call, so an edge arriving on the way out would otherwise
     * find the slot still marked free and drop the callback. The dispatch
     * checks the used flag, so an interrupt in that window either finds a
     * fully published slot or (before this store) returns harmlessly. */
    s_wl82_slot_pin[slot] = pin_id;
    s_wl82_slot_used[slot] = 1;

    /* One event per slot. EVENT_IO_0 and EVENT_IO_1 each own a separate
     * io-map remap field and a separate enable/edge/pending bit, so two pins
     * must use different events: registering both on EVENT_IO_0 would make the
     * second overwrite the first's remap (so the first pin stops firing) and
     * would make the vendor ISR dispatch BOTH handlers on either pin's edge,
     * because it walks every table entry whose stored event is pended. */
    hdl = port_wakeup_reg(slot == 0 ? EVENT_IO_0 : EVENT_IO_1, (unsigned int)pin_id,
                          (PORT_EDGE_E)edge, s_wl82_isr[slot]);
    if (hdl == NULL) {
        /* The pin is not in the driver's remap table - ports D and F are not
         * listed at all, and A/B/C/E/G/H only partially. Give the slot back. */
        s_wl82_slot_used[slot] = 0;
        return OPRT_NOT_SUPPORTED;
    }

    s_irq[pin_id].hdl = hdl;
    s_irq[pin_id].armed = 1u;
    return OPRT_OK;
}

static void tkl_gpio_wl82_unregister(TUYA_GPIO_NUM_E pin_id)
{
    int slot;

    if (!s_irq[pin_id].armed) {
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
    s_irq[pin_id].armed = 0u;
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
    /* The index is documented as non-negative, so 0 is a valid registration and
     * cannot be used as the "not armed" sentinel - that would make the first
     * pin's registration invisible to disable/deinit and leave its callback
     * firing after the caller disabled it. exti_armed is the source of truth. */
    s_irq[pin_id].exti = index;
    s_irq[pin_id].armed = 1u;
    return OPRT_OK;
}

static void tkl_gpio_wl83_unregister(TUYA_GPIO_NUM_E pin_id)
{
    if (!s_irq[pin_id].armed) {
        return;
    }
    (void)exti_uninit(s_irq[pin_id].exti);
    s_irq[pin_id].armed = 0u;
    s_irq[pin_id].exti = -1;
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
    if (s_irq[pin_id].armed) {
        /* Re-initialising an armed pin: hand the old registration back first,
         * or the vendor slot would leak and the new configuration would be
         * silently ignored until a disable. */
        (void)tkl_gpio_irq_disable(pin_id);
    }
    s_irq[pin_id].cb = cfg->cb;
    s_irq[pin_id].arg = cfg->arg;
    s_irq[pin_id].mode = cfg->mode;
    s_irq[pin_id].hdl = NULL;
    s_irq[pin_id].exti = -1;
    s_irq[pin_id].armed = 0u;
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
    if (s_irq[pin_id].armed) {
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
