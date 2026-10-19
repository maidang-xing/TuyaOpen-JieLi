#include "tkl_timer.h"
#include "tuya_error_code.h"

#include <stdint.h>

#include "system/timer.h"

/* Exported by both chips' cpu.a but absent from every shipped header, so it is
 * declared here. Signature taken from the symbol's own use: no arguments, a
 * free-running microsecond count. Using an undeclared symbol would fail the
 * -Wimplicit-function-declaration check, and inventing a different counter
 * would change the unit. */
extern u32 local_timer_us_time(void);

/* Hardware timer.
 *
 * Both chips expose the same microsecond timer API, which is what makes one
 * backend possible here:
 *
 *   sys_usec_timer_add(priv, fn, usec, call_in_irq, once) -> id
 *   sys_usec_timer_set(id, usec)
 *   sys_usec_timer_del(id)
 *   local_timer_us_time()      - the hardware counter, in microseconds
 *
 * Two things about it matter for the TKL contract and are stated rather than
 * glossed:
 *
 *   Context. sys_usec_timer_add takes call_in_irq explicitly. TAL's timer
 *   callback (e.g. the IR driver's) is written for an interrupt context, so it
 *   is registered with call_in_irq = 1. A caller whose callback blocks must not
 *   use this TKL on these parts; the flag is the vendor's own contract and is
 *   chosen here for the consumers that exist.
 *
 *   Expiry vs capture. local_timer_us_time() reads a free-running counter, so
 *   tkl_timer_get() reports elapsed time since the timer was started, which is
 *   how the TKL contract's "current value" is used by the IR path. There is no
 *   separate capture register in this API.
 *
 * The vendor internal timer1_init/timer1_run/timer1_stop symbols exist in the
 * prebuilt archives but have no header declaration and no caller anywhere in
 * the SDK, so they are deliberately not used: their units and interrupt wiring
 * are undocumented.
 *
 * sys_timer_get_ms() is millisecond-resolution and is not used for the timer
 * itself; it only backs nothing here. The microsecond counter is the one that
 * matches the contract's usec units.
 */

#define TKL_TIMER_MAX TUYA_TIMER_NUM_MAX

typedef struct {
    int id;              /* vendor timer id, negative when unused */
    TUYA_TIMER_MODE_E mode;
    TUYA_TIMER_ISR_CB cb;
    void *args;
    uint32_t started_us;
} tkl_timer_ctx_t;

static tkl_timer_ctx_t s_timer[TKL_TIMER_MAX];
static int s_timer_ready;

static void jieli_timer_dispatch(void *priv)
{
    tkl_timer_ctx_t *ctx;

    if (priv == NULL) {
        return;
    }
    ctx = (tkl_timer_ctx_t *)priv;
    if (ctx->mode == TUYA_TIMER_MODE_ONCE) {
        /* The vendor's `once` flag already stops it; remember that so a later
         * tkl_timer_stop does not try to delete an id the vendor has retired. */
        ctx->id = -1;
    } else {
        ctx->started_us = local_timer_us_time();
    }
    if (ctx->cb != NULL) {
        ctx->cb(ctx->args);
    }
}

static void jieli_timer_reset_table(void)
{
    uint32_t i;

    if (s_timer_ready) {
        return;
    }
    for (i = 0; i < TKL_TIMER_MAX; i++) {
        s_timer[i].id = -1;
        s_timer[i].mode = TUYA_TIMER_MODE_ONCE;
        s_timer[i].cb = NULL;
        s_timer[i].args = NULL;
        s_timer[i].started_us = local_timer_us_time();
    }
    s_timer_ready = 1;
}

static int jieli_timer_valid(TUYA_TIMER_NUM_E timer_id)
{
    jieli_timer_reset_table();
    return timer_id < TUYA_TIMER_NUM_MAX;
}

OPERATE_RET tkl_timer_init(TUYA_TIMER_NUM_E timer_id, TUYA_TIMER_BASE_CFG_T *cfg)
{
    tkl_timer_ctx_t *ctx;

    if (!jieli_timer_valid(timer_id) || cfg == NULL) {
        return OPRT_INVALID_PARM;
    }
    if (cfg->mode != TUYA_TIMER_MODE_ONCE && cfg->mode != TUYA_TIMER_MODE_PERIOD) {
        return OPRT_INVALID_PARM;
    }

    ctx = &s_timer[timer_id];
    /* Re-initialising a running timer: hand the vendor id back first, or the
     * timer stays armed with a stale context. */
    if (ctx->id >= 0) {
        sys_usec_timer_del(ctx->id);
        ctx->id = -1;
    }
    ctx->mode = cfg->mode;
    ctx->cb = cfg->cb;
    ctx->args = cfg->args;
    return OPRT_OK;
}

OPERATE_RET tkl_timer_start(TUYA_TIMER_NUM_E timer_id, uint32_t us)
{
    tkl_timer_ctx_t *ctx;
    int id;

    if (!jieli_timer_valid(timer_id) || us == 0u) {
        return OPRT_INVALID_PARM;
    }
    ctx = &s_timer[timer_id];
    if (ctx->cb == NULL) {
        return OPRT_INVALID_PARM;
    }

    if (ctx->id >= 0) {
        /* Already armed: change the period in place, which is what the vendor
         * exposes, rather than creating a second timer on the same slot. */
        sys_usec_timer_set(ctx->id, us);
        ctx->started_us = local_timer_us_time();
        return OPRT_OK;
    }

    id = sys_usec_timer_add(ctx, jieli_timer_dispatch, us, 1u,
                            ctx->mode == TUYA_TIMER_MODE_ONCE ? 1u : 0u);
    if (id < 0) {
        return OPRT_RESOURCE_NOT_READY;
    }
    ctx->id = id;
    ctx->started_us = local_timer_us_time();
    return OPRT_OK;
}

OPERATE_RET tkl_timer_stop(TUYA_TIMER_NUM_E timer_id)
{
    tkl_timer_ctx_t *ctx;

    if (!jieli_timer_valid(timer_id)) {
        return OPRT_INVALID_PARM;
    }
    ctx = &s_timer[timer_id];
    if (ctx->id >= 0) {
        sys_usec_timer_del(ctx->id);
        ctx->id = -1;
    }
    return OPRT_OK;
}

OPERATE_RET tkl_timer_deinit(TUYA_TIMER_NUM_E timer_id)
{
    OPERATE_RET rt = tkl_timer_stop(timer_id);

    if (rt != OPRT_OK) {
        return rt;
    }
    s_timer[timer_id].cb = NULL;
    s_timer[timer_id].args = NULL;
    return OPRT_OK;
}

OPERATE_RET tkl_timer_get_current_value(TUYA_TIMER_NUM_E timer_id, uint32_t *us)
{
    if (!jieli_timer_valid(timer_id) || us == NULL) {
        return OPRT_INVALID_PARM;
    }
    /* Free-running counter since the timer was last started. */
    *us = local_timer_us_time() - s_timer[timer_id].started_us;
    return OPRT_OK;
}

OPERATE_RET tkl_timer_get(TUYA_TIMER_NUM_E timer_id, uint32_t *us)
{
    return tkl_timer_get_current_value(timer_id, us);
}
