/**
 * @file tkl_sleep.c
 * @brief CPU sleep adapter for JieLi WL82 (AC791) and WL83 (AC792).
 *
 * TuyaOpen's generic power path (tuya_pm -> lpmgr -> tal_sleep) bottoms out in
 * tkl_cpu_sleep_mode_set(): TRUE lets the core idle into its low-power state,
 * FALSE holds it awake. The two SDKs express that with different primitives,
 * so the mapping is chip-specific:
 *
 *   WL82  low_power_sys_unsleep_lock() / low_power_sys_unsleep_unlock()
 *         declared in asm/power_interface.h, defined in cpu/wl82/liba/cpu.a
 *         (llvm-nm: T low_power_sys_unsleep_lock / _unlock).
 *   WL83  low_power_disable() / low_power_enable()
 *         declared in asm/power/power_api.h, defined in the cpu/wl83/liba
 *         archives (llvm-nm: T low_power_disable / T low_power_enable). WL83
 *         has no *_unsleep_lock equivalent - the whole liba set was searched.
 *
 * The WL82 pair is counted inside the SDK and the WL83 pair is a plain global
 * switch, so neither is safe to call once per request: a second lock with no
 * second unlock would stay locked forever, and two callers asking for "awake"
 * followed by one release would wrongly re-enable sleep. This adapter keeps
 * one boolean and only touches the vendor API on a state CHANGE, which makes
 * every entry point idempotent. Reference counting belongs to
 * tal_cpu_lp_disable()/tal_cpu_lp_enable() and the TAL wakelock bitmap above
 * this layer; duplicating it here would let the two counters drift apart.
 *
 * Deep sleep is deliberately NOT wired. TUYA_CPU_DEEP_SLEEP would mean AC792's
 * DEEP_SLEEP_EN=2 / power-off-and-reboot path, which is out of scope, so the
 * mode is refused with OPRT_NOT_SUPPORTED rather than silently degraded into an
 * ordinary sleep - a caller that asked for deep sleep must not be told it got
 * something it did not.
 *
 * @copyright Copyright (c) 2021-2026 Tuya Inc. All Rights Reserved.
 */

#include "tkl_sleep.h"

#include "tkl_mutex.h"
#include "tuya_error_code.h"

#include "system/os/os_api.h"

#if defined(JIELI_SELECTED_CHIP_WL82)
#include "asm/power_interface.h"
#elif defined(JIELI_SELECTED_CHIP_WL83)
#include "asm/power/power_api.h"
#else
#error "Jieli CPU sleep requires a selected chip"
#endif

static TKL_MUTEX_HANDLE s_sleep_mutex = NULL;
/* Matches the hardware state left by board_power_init(): both boards call
 * power_init() with low power enabled, so the core may sleep until someone
 * says otherwise. Starting at TRUE also makes the first request for the state
 * the hardware is already in a no-op instead of a vendor call issued before
 * power_init() has run. */
static BOOL_T s_sleep_allowed = TRUE;

static OPERATE_RET jieli_sleep_mutex_ready(void)
{
    if (s_sleep_mutex != NULL) {
        return OPRT_OK;
    }
    return tkl_mutex_create_init(&s_sleep_mutex);
}

/* Hold the core awake, or let it idle. Idempotent: the vendor call happens
 * only when the state actually changes. */
static OPERATE_RET jieli_sleep_allowed_set(BOOL_T allow)
{
    OPERATE_RET rt = jieli_sleep_mutex_ready();

    if (OPRT_OK != rt) {
        return rt;
    }

    tkl_mutex_lock(s_sleep_mutex);

    if (s_sleep_allowed == allow) {
        tkl_mutex_unlock(s_sleep_mutex);
        return OPRT_OK;
    }

#if defined(JIELI_SELECTED_CHIP_WL82)
    if (allow) {
        low_power_sys_unsleep_unlock();
    } else {
        low_power_sys_unsleep_lock();
    }
#else
    if (allow) {
        low_power_enable();
    } else {
        low_power_disable();
    }
#endif

    s_sleep_allowed = allow;
    tkl_mutex_unlock(s_sleep_mutex);

    return OPRT_OK;
}

void tkl_system_sleep(uint32_t num_ms)
{
    os_time_dly((int)((num_ms + 9u) / 10u));
}

void tkl_system_delay(uint32_t num_ms)
{
    os_time_dly((int)((num_ms + 9u) / 10u));
}

OPERATE_RET tkl_cpu_sleep_mode_set(BOOL_T enable, TUYA_CPU_SLEEP_MODE_E mode)
{
    if (TUYA_CPU_SLEEP != mode) {
        return OPRT_NOT_SUPPORTED;
    }

    return jieli_sleep_allowed_set(enable ? TRUE : FALSE);
}

/* Neither SDK publishes a usable pre/post sleep hook to an adapter: the WL82
 * lp_target registry and the WL83 low_power_operation callbacks are internal
 * to the sleep flow, and no supported way to attach one from here is
 * documented. Reporting OPRT_NOT_SUPPORTED keeps callers from believing a
 * callback is installed. */
OPERATE_RET tkl_cpu_sleep_callback_register(TUYA_SLEEP_CB_T *sleep_cb)
{
    (void)sleep_cb;
    return OPRT_NOT_SUPPORTED;
}

void tkl_cpu_allow_sleep(void)
{
    (void)jieli_sleep_allowed_set(TRUE);
}

void tkl_cpu_force_wakeup(void)
{
    /* On both chips the core only sleeps inside the idle task, so a running
     * thread has no suspended context to resume. The useful action is to stop
     * the next idle from sleeping, which is what holding the core awake does. */
    (void)jieli_sleep_allowed_set(FALSE);
}
