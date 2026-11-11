#include "tkl_pwm.h"
#include "tuya_error_code.h"

#include <stdint.h>

#include "device.h"
#if defined(JIELI_SELECTED_CHIP_WL83)
/* wl83's asm/pwm.h is self-contained: generic/typedef.h, generic/ioctl.h and
 * device/device.h only. */
#include "asm/pwm.h"
#include "asm/gpio.h"

/* WL83 board remap: the four MCPWM channels get a complementary H/L pin pair
 * each. The pins come from the board's Kconfig through the generated
 * tuya_board_periph_config.h; the defaults below only apply to a board that
 * declares nothing. CH1 keeps PA8/PA10, which is where this adapter has always
 * carried it. */
#ifndef TUYA_BOARD_PWM0_H_PIN
#define TUYA_BOARD_PWM0_H_PIN IO_PORTB_08
#endif
#ifndef TUYA_BOARD_PWM0_L_PIN
#define TUYA_BOARD_PWM0_L_PIN IO_PORTB_09
#endif
#ifndef TUYA_BOARD_PWM1_H_PIN
#define TUYA_BOARD_PWM1_H_PIN IO_PORTA_08
#endif
#ifndef TUYA_BOARD_PWM1_L_PIN
#define TUYA_BOARD_PWM1_L_PIN IO_PORTA_10
#endif
#ifndef TUYA_BOARD_PWM2_H_PIN
#define TUYA_BOARD_PWM2_H_PIN IO_PORTB_10
#endif
#ifndef TUYA_BOARD_PWM2_L_PIN
#define TUYA_BOARD_PWM2_L_PIN IO_PORTB_11
#endif
#ifndef TUYA_BOARD_PWM3_H_PIN
#define TUYA_BOARD_PWM3_H_PIN IO_PORTB_12
#endif
#ifndef TUYA_BOARD_PWM3_L_PIN
#define TUYA_BOARD_PWM3_L_PIN IO_PORTB_13
#endif

static struct pwm_platform_data s_pwm1_pd = {
    .timer_pwm_port = { -1, -1, -1, -1 },
    .mcpwm_port = {
        /* MCPWM0H .. MCPWM3H */
        TUYA_BOARD_PWM0_H_PIN, TUYA_BOARD_PWM1_H_PIN,
        TUYA_BOARD_PWM2_H_PIN, TUYA_BOARD_PWM3_H_PIN,
        /* MCPWM0L .. MCPWM3L */
        TUYA_BOARD_PWM0_L_PIN, TUYA_BOARD_PWM1_L_PIN,
        TUYA_BOARD_PWM2_L_PIN, TUYA_BOARD_PWM3_L_PIN,
    },
    .pwm_config = {
        .pwm_ch = PWMCH1_H | PWMCH1_L,
        /* Match the WL83 vendor example's safe, nonzero board defaults. */
        .freq = 4800,
        .duty = 80.0f,
        .point_bit = 2,
        .deathtime = 0,
    },
    .hd_level = GPIO_DRIVE_STRENGTH_2p5mA,
};
REGISTER_DEVICE(pwm1_node) = { "pwm1", &pwm_dev_ops, &s_pwm1_pd };
#else
/* WL82 asm/pwm.h pulls vendor filesystem declarations that conflict with
 * libc. Define its public _IOW('P', n, u32) command encoding locally instead:
 * dir<<30 | size<<16 | type<<8 | nr, with a 4-byte payload. Values match the
 * SDK macro expansion. */
#define TKL_PWM_IOC_WRITE_U32(nr)     ((uint32_t)((1U << 30) | (4U << 16) | ((uint32_t)('P') << 8) | (uint32_t)(nr)))

#define PWM_STOP          TKL_PWM_IOC_WRITE_U32(0)
#define PWM_RUN           TKL_PWM_IOC_WRITE_U32(1)
#define PWM_FORDIRC       TKL_PWM_IOC_WRITE_U32(2)
#define PWM_REVDIRC       TKL_PWM_IOC_WRITE_U32(3)
#define PWM_SET_FREQ      TKL_PWM_IOC_WRITE_U32(6)
#define PWM_SET_DUTY      TKL_PWM_IOC_WRITE_U32(7)

/* Channel masks: the same bit positions as asm/pwm.h (PWMCHx_H is bit x,
 * PWMCHx_L is bit 10+x). */
#define PWMCH0_H          (1U << 0)
#define PWMCH1_H          (1U << 1)
#define PWMCH2_H          (1U << 2)
#define PWMCH3_H          (1U << 3)
#define PWM_CHL_OFFSET    10U
#define PWMCH0_L          (1U << (PWM_CHL_OFFSET + 0))
#define PWMCH1_L          (1U << (PWM_CHL_OFFSET + 1))
#define PWMCH2_L          (1U << (PWM_CHL_OFFSET + 2))
#define PWMCH3_L          (1U << (PWM_CHL_OFFSET + 3))

/* WL82 PWM runs on TIMER2/TIMER3 output channels, not MCPWM. A timer output
 * takes one pin and one channel per device, so the adapter registers pwm1 with
 * TIMER2 on the pin the board declares (PH2 by default, which is what the
 * vendor DevKitBoard uses for its own PWM sample). TKL PWM0 is that single
 * channel; it is not a complementary pair, so only the forward direction
 * exists. */
#include "asm/gpio.h"

/* asm/pwm.h is not includable here (it pulls vendor filesystem declarations
 * that conflict with libc), so its channel bit is spelled out: TIME_PWM_OFFSET
 * is 20 and TIMER2 is the first timer output. */
#define PWM_TIMER2_OPCH2  (1U << 20)

#ifndef TUYA_BOARD_PWM0_PIN
#define TUYA_BOARD_PWM0_PIN IO_PORTH_02
#endif

typedef struct {
    uint32_t con;
    uint32_t cnt;
    uint32_t prd;
} PWM_TIMER;

typedef struct {
    uint32_t con0;
    uint32_t con1;
    uint32_t cmph;
    uint32_t cmpl;
} PWM_CHCON;

struct pwm_platform_data {
    PWM_TIMER *treg;
    PWM_CHCON *creg;
    float duty;
    uint8_t deathtime;
    uint8_t point_bit;
    uint32_t port;
    uint32_t pwm_ch;
    uint32_t freq;
};

extern const struct device_operations pwm_dev_ops;

static const struct pwm_platform_data s_pwm1_pd = {
    .treg = NULL,
    .creg = NULL,
    .duty = 50.0f,
    .deathtime = 0u,
    .point_bit = 0u,
    .port = TUYA_BOARD_PWM0_PIN,
    .pwm_ch = PWM_TIMER2_OPCH2,
    .freq = 1000u,
};
REGISTER_DEVICE(pwm1_node) = { "pwm1", &pwm_dev_ops, (void *)&s_pwm1_pd };
#define JIELI_WL82_PWM_PORT TUYA_BOARD_PWM0_PIN
#endif

/* PWM, built on each chip's `pwm1` device.
 *
 * Both SDKs drive the same hardware blocks from different descriptions, and the
 * differences are large enough that this file cannot pretend to one contract:
 *
 *   wl82  pwm_platform_data {treg, creg, duty(float), deathtime, point_bit,
 *                            port, pwm_ch, freq}
 *         The vendor's own example states the constraint plainly:
 *         "初始化之后，只支持单通道选择" - after open, only ONE channel group.
 *         The complementary pair PWMCHx_H|PWMCHx_L is how a channel is driven.
 *
 *   wl83  pwm_platform_data {timer_pwm_port[], mcpwm_port[], pwm_config{...},
 *                            hd_level}
 *         Multi-channel ioctl is supported, but each channel's IO is fixed at
 *         board level in the remap tables, so a channel with no declared pin
 *         cannot be driven.
 *
 * WL83 registers MCPWM CH0-CH3 below. WL82 registers MCPWM's alternative:
 * TIMER2 output on a single pin, because that is the only PWM route in the
 * AC791 SDK that is not shared with another peripheral's output channel.
 */
static void *s_pwm_handle;
static TUYA_PWM_BASE_CFG_T s_pwm_cfg[TUYA_PWM_NUM_MAX];
static uint8_t s_pwm_inited[TUYA_PWM_NUM_MAX];
static uint8_t s_pwm_running[TUYA_PWM_NUM_MAX];

/* Only the channel groups each chip exposes: four complementary pairs on WL83,
 * one timer output on WL82. The TKL enum goes to eleven, so the tail is refused
 * rather than mapped to a channel the vendor has no register for. */
#define TKL_PWM_MAX_CHANNEL 4u

/* TUYA_PWM_BASE_CFG_T defines duty as one part of cycle. Callers that leave
 * cycle at zero are using the 0-10000 scale several of the shared examples are
 * written in, so zero means 10000 rather than an invalid period. */
#define JIELI_PWM_DEFAULT_CYCLE 10000u

static uint32_t jieli_pwm_cycle(const TUYA_PWM_BASE_CFG_T *cfg)
{
    return cfg->cycle != 0u ? cfg->cycle : JIELI_PWM_DEFAULT_CYCLE;
}

static uint32_t jieli_pwm_channel_bit(TUYA_PWM_NUM_E ch_id)
{
#if defined(JIELI_SELECTED_CHIP_WL83)
    switch (ch_id) {
    case TUYA_PWM_NUM_0: return PWMCH0_H | PWMCH0_L;
    case TUYA_PWM_NUM_1: return PWMCH1_H | PWMCH1_L;
    case TUYA_PWM_NUM_2: return PWMCH2_H | PWMCH2_L;
    case TUYA_PWM_NUM_3: return PWMCH3_H | PWMCH3_L;
    default:             return 0u;
    }
#else
    /* WL82 exposes a single timer output; there is no H/L pair to select. */
    return ch_id == TUYA_PWM_NUM_0 ? PWM_TIMER2_OPCH2 : 0u;
#endif
}

static int jieli_pwm_valid(TUYA_PWM_NUM_E ch_id)
{
    return ch_id < TUYA_PWM_NUM_MAX && ch_id < TKL_PWM_MAX_CHANNEL &&
           jieli_pwm_channel_bit(ch_id) != 0u;
}

/* Only channels with a complete board-level H/L pin pair are exposed by the
 * selected chip's registered pwm1 device. */
static int jieli_pwm_channel_has_pins(TUYA_PWM_NUM_E ch_id)
{
#if defined(JIELI_SELECTED_CHIP_WL83)
    return ch_id < TKL_PWM_MAX_CHANNEL;
#else
    return ch_id == TUYA_PWM_NUM_0;
#endif
}

static OPERATE_RET jieli_pwm_cfg_validate(const TUYA_PWM_BASE_CFG_T *cfg)
{
    if (cfg == NULL || cfg->frequency == 0u || cfg->duty > jieli_pwm_cycle(cfg)) {
        return OPRT_INVALID_PARM;
    }
    if (cfg->count_mode != TUYA_PWM_CNT_UP) {
        return OPRT_NOT_SUPPORTED;
    }
    if (cfg->polarity != TUYA_PWM_POSITIVE && cfg->polarity != TUYA_PWM_NEGATIVE) {
        return OPRT_INVALID_PARM;
    }
    return OPRT_OK;
}

/* The vendor takes a percentage plus a decimal-point count. TUYA_PWM_BASE_CFG
 * gives duty/cycle, so this converts to a float percentage and picks point_bit
 * from the frequency, mirroring the board headers' own guidance (2 below
 * 4 kHz, 1 below 40 kHz, else 0). The vendor rounds to that many decimals
 * itself; with point_bit 0 a 33.33% request lands on 33%. */
static void jieli_pwm_duty_percent(const TUYA_PWM_BASE_CFG_T *cfg, float *duty, uint8_t *point_bit)
{
    uint32_t freq = cfg->frequency;
    uint32_t cycle = jieli_pwm_cycle(cfg);

    if (freq != 0u && freq <= 4000u) {
        *point_bit = 2u;
    } else if (freq != 0u && freq <= 40000u) {
        *point_bit = 1u;
    } else {
        *point_bit = 0u;
    }
    *duty = (float)((double)cfg->duty * 100.0 / (double)cycle);
}

/* A failed configure/run sequence may have already changed some registers.
 * Try to leave the channel in a known stopped state before returning the
 * original error; preserve the prior running flag if even the stop ioctl fails. */
static void jieli_pwm_stop_after_error(TUYA_PWM_NUM_E ch_id, const void *vendor_cfg)
{
#if defined(JIELI_SELECTED_CHIP_WL83)
    int ret = dev_ioctl(s_pwm_handle, IOCTL_PWM_SET_STOP,
                        (u32)(uintptr_t)vendor_cfg);
#else
    int ret = dev_ioctl(s_pwm_handle, PWM_STOP, (u32)(uintptr_t)vendor_cfg);
#endif
    if (ret >= 0) {
        s_pwm_running[ch_id] = 0u;
    }
}

OPERATE_RET tkl_pwm_init(TUYA_PWM_NUM_E ch_id, const TUYA_PWM_BASE_CFG_T *cfg)
{
    if (!jieli_pwm_valid(ch_id)) {
        return OPRT_INVALID_PARM;
    }
    if (!jieli_pwm_channel_has_pins(ch_id)) {
        return OPRT_NOT_SUPPORTED;
    }
    {
        OPERATE_RET rt = jieli_pwm_cfg_validate(cfg);
        if (rt != OPRT_OK) return rt;
    }

    if (s_pwm_handle == NULL) {
        s_pwm_handle = dev_open("pwm1", NULL);
        if (s_pwm_handle == NULL) {
            return OPRT_NOT_SUPPORTED;
        }
    }

    /* Reinitializing an active channel must stop the old hardware configuration
     * before resetting its software state or replacing the configuration. */
    if (s_pwm_inited[ch_id] && s_pwm_running[ch_id]) {
        OPERATE_RET rt = tkl_pwm_stop(ch_id);
        if (rt != OPRT_OK) {
            return rt;
        }
    }

    s_pwm_cfg[ch_id] = *cfg;
    s_pwm_inited[ch_id] = 1u;
    s_pwm_running[ch_id] = 0u;
    return OPRT_OK;
}

static OPERATE_RET jieli_pwm_apply(TUYA_PWM_NUM_E ch_id, BOOL_T run)
{
    const TUYA_PWM_BASE_CFG_T *cfg = &s_pwm_cfg[ch_id];
    float duty;
    uint8_t point_bit;
    int ret;

    if (s_pwm_handle == NULL) {
        return OPRT_INVALID_PARM;
    }
    jieli_pwm_duty_percent(cfg, &duty, &point_bit);

#if defined(JIELI_SELECTED_CHIP_WL83)
    {
        pwm_config_t pc;

        pc.pwm_ch = jieli_pwm_channel_bit(ch_id);
        pc.duty = duty;
        pc.point_bit = point_bit;
        pc.deathtime = 0u;
        pc.freq = cfg->frequency;
        /* Frequency first: the vendor derives the period from it, so a duty
         * written before the period is meaningful would be rescaled. */
        if (pc.freq != 0u) {
            ret = dev_ioctl(s_pwm_handle, IOCTL_PWM_SET_FREQ, (u32)(uintptr_t)&pc);
            if (ret < 0) {
                jieli_pwm_stop_after_error(ch_id, &pc);
                return OPRT_OS_ADAPTER_PWM_INIT_FAILED;
            }
        }
        ret = dev_ioctl(s_pwm_handle,
                        cfg->polarity == TUYA_PWM_NEGATIVE ? IOCTL_PWM_SET_REVDIRC : IOCTL_PWM_SET_FORDIRC,
                        (u32)(uintptr_t)&pc);
        if (ret < 0) {
            jieli_pwm_stop_after_error(ch_id, &pc);
            return OPRT_OS_ADAPTER_PWM_MODE_ERR;
        }
        if (dev_ioctl(s_pwm_handle, IOCTL_PWM_SET_DUTY, (u32)(uintptr_t)&pc) < 0) {
            jieli_pwm_stop_after_error(ch_id, &pc);
            return OPRT_OS_ADAPTER_PWM_MODE_ERR;
        }
        if (dev_ioctl(s_pwm_handle, run ? IOCTL_PWM_SET_RUN : IOCTL_PWM_SET_STOP,
                      (u32)(uintptr_t)&pc) < 0) {
            jieli_pwm_stop_after_error(ch_id, &pc);
            return OPRT_OS_ADAPTER_PWM_MODE_ERR;
        }
    }
#else
    {
        struct pwm_platform_data pd;

        pd.treg = NULL;
        pd.creg = NULL;
        pd.duty = duty;
        pd.deathtime = 0u;
        pd.point_bit = point_bit;
        pd.port = JIELI_WL82_PWM_PORT;
        pd.pwm_ch = jieli_pwm_channel_bit(ch_id);
        pd.freq = cfg->frequency;
        if (pd.freq != 0u) {
            ret = dev_ioctl(s_pwm_handle, PWM_SET_FREQ, (u32)(uintptr_t)&pd);
            if (ret < 0) {
                jieli_pwm_stop_after_error(ch_id, &pd);
                return OPRT_OS_ADAPTER_PWM_INIT_FAILED;
            }
        }
        ret = dev_ioctl(s_pwm_handle,
                        cfg->polarity == TUYA_PWM_NEGATIVE ? PWM_REVDIRC : PWM_FORDIRC,
                        (u32)(uintptr_t)&pd);
        if (ret < 0) {
            jieli_pwm_stop_after_error(ch_id, &pd);
            return OPRT_OS_ADAPTER_PWM_MODE_ERR;
        }
        if (dev_ioctl(s_pwm_handle, PWM_SET_DUTY, (u32)(uintptr_t)&pd) < 0) {
            jieli_pwm_stop_after_error(ch_id, &pd);
            return OPRT_OS_ADAPTER_PWM_MODE_ERR;
        }
        if (dev_ioctl(s_pwm_handle, run ? PWM_RUN : PWM_STOP, (u32)(uintptr_t)&pd) < 0) {
            jieli_pwm_stop_after_error(ch_id, &pd);
            return OPRT_OS_ADAPTER_PWM_MODE_ERR;
        }
    }
#endif
    return OPRT_OK;
}

OPERATE_RET tkl_pwm_deinit(TUYA_PWM_NUM_E ch_id)
{
    uint32_t i;

    if (!jieli_pwm_valid(ch_id)) {
        return OPRT_INVALID_PARM;
    }
    if (s_pwm_inited[ch_id]) {
        OPERATE_RET rt = tkl_pwm_stop(ch_id);
        if (rt != OPRT_OK) return rt;
        s_pwm_inited[ch_id] = 0u;
        s_pwm_running[ch_id] = 0u;
    }
    /* The handle is shared by every channel; close it only when the last one
     * goes away, or a later channel's start would find a dangling device. */
    for (i = 0; i < TUYA_PWM_NUM_MAX; i++) {
        if (s_pwm_inited[i]) {
            return OPRT_OK;
        }
    }
    if (s_pwm_handle != NULL) {
        dev_close(s_pwm_handle);
        s_pwm_handle = NULL;
    }
    return OPRT_OK;
}

OPERATE_RET tkl_pwm_start(TUYA_PWM_NUM_E ch_id)
{
    if (!jieli_pwm_valid(ch_id) || !s_pwm_inited[ch_id]) {
        return OPRT_INVALID_PARM;
    }
    {
        OPERATE_RET rt = jieli_pwm_apply(ch_id, TRUE);
        if (rt == OPRT_OK) s_pwm_running[ch_id] = 1u;
        return rt;
    }
}

OPERATE_RET tkl_pwm_stop(TUYA_PWM_NUM_E ch_id)
{
    if (!jieli_pwm_valid(ch_id) || !s_pwm_inited[ch_id]) {
        return OPRT_INVALID_PARM;
    }
    {
        OPERATE_RET rt = jieli_pwm_apply(ch_id, FALSE);
        if (rt == OPRT_OK) s_pwm_running[ch_id] = 0u;
        return rt;
    }
}

OPERATE_RET tkl_pwm_duty_set(TUYA_PWM_NUM_E ch_id, uint32_t duty)
{
    if (!jieli_pwm_valid(ch_id) || !s_pwm_inited[ch_id]) {
        return OPRT_INVALID_PARM;
    }
    if (duty > jieli_pwm_cycle(&s_pwm_cfg[ch_id])) {
        return OPRT_INVALID_PARM;
    }
    {
        uint32_t old = s_pwm_cfg[ch_id].duty;
        OPERATE_RET rt;
        s_pwm_cfg[ch_id].duty = duty;
        rt = jieli_pwm_apply(ch_id, s_pwm_running[ch_id]);
        if (rt != OPRT_OK) s_pwm_cfg[ch_id].duty = old;
        return rt;
    }
}

OPERATE_RET tkl_pwm_frequency_set(TUYA_PWM_NUM_E ch_id, uint32_t frequency)
{
    if (!jieli_pwm_valid(ch_id) || !s_pwm_inited[ch_id] || frequency == 0u) {
        return OPRT_INVALID_PARM;
    }
    {
        uint32_t old = s_pwm_cfg[ch_id].frequency;
        OPERATE_RET rt;
        s_pwm_cfg[ch_id].frequency = frequency;
        rt = jieli_pwm_apply(ch_id, s_pwm_running[ch_id]);
        if (rt != OPRT_OK) s_pwm_cfg[ch_id].frequency = old;
        return rt;
    }
}

OPERATE_RET tkl_pwm_polarity_set(TUYA_PWM_NUM_E ch_id, TUYA_PWM_POLARITY_E polarity)
{
    if (!jieli_pwm_valid(ch_id) || !s_pwm_inited[ch_id] || s_pwm_handle == NULL) {
        return OPRT_INVALID_PARM;
    }
    if (polarity != TUYA_PWM_POSITIVE && polarity != TUYA_PWM_NEGATIVE) {
        return OPRT_INVALID_PARM;
    }
#if defined(JIELI_SELECTED_CHIP_WL83)
    {
        pwm_config_t pc;

        pc.pwm_ch = jieli_pwm_channel_bit(ch_id);
        pc.duty = 0;
        pc.point_bit = 0;
        pc.deathtime = 0;
        pc.freq = 0;
        if (dev_ioctl(s_pwm_handle,
                      polarity == TUYA_PWM_NEGATIVE ? IOCTL_PWM_SET_REVDIRC : IOCTL_PWM_SET_FORDIRC,
                      (u32)(uintptr_t)&pc) < 0) {
            jieli_pwm_stop_after_error(ch_id, &pc);
            return OPRT_OS_ADAPTER_PWM_MODE_ERR;
        }
    }
#else
    {
        struct pwm_platform_data pd;

        pd.treg = NULL;
        pd.creg = NULL;
        pd.duty = 0.0f;
        pd.deathtime = 0u;
        pd.point_bit = 0u;
        pd.port = JIELI_WL82_PWM_PORT;
        pd.pwm_ch = jieli_pwm_channel_bit(ch_id);
        pd.freq = 0u;
        if (dev_ioctl(s_pwm_handle,
                      polarity == TUYA_PWM_NEGATIVE ? PWM_REVDIRC : PWM_FORDIRC,
                      (u32)(uintptr_t)&pd) < 0) {
            jieli_pwm_stop_after_error(ch_id, &pd);
            return OPRT_OS_ADAPTER_PWM_MODE_ERR;
        }
    }
#endif
    s_pwm_cfg[ch_id].polarity = polarity;
    return OPRT_OK;
}

OPERATE_RET tkl_pwm_info_set(TUYA_PWM_NUM_E ch_id, const TUYA_PWM_BASE_CFG_T *cfg)
{
    if (cfg == NULL || !jieli_pwm_valid(ch_id)) {
        return OPRT_INVALID_PARM;
    }
    {
        OPERATE_RET rt = jieli_pwm_cfg_validate(cfg);
        TUYA_PWM_BASE_CFG_T old;
        if (rt != OPRT_OK) return rt;
        old = s_pwm_cfg[ch_id];
        s_pwm_cfg[ch_id] = *cfg;
        if (!s_pwm_inited[ch_id]) return OPRT_OK;
        rt = jieli_pwm_apply(ch_id, s_pwm_running[ch_id]);
        if (rt != OPRT_OK) s_pwm_cfg[ch_id] = old;
        return rt;
    }
}

OPERATE_RET tkl_pwm_info_get(TUYA_PWM_NUM_E ch_id, TUYA_PWM_BASE_CFG_T *cfg)
{
    if (cfg == NULL || !jieli_pwm_valid(ch_id)) {
        return OPRT_INVALID_PARM;
    }
    *cfg = s_pwm_cfg[ch_id];
    return OPRT_OK;
}

OPERATE_RET tkl_pwm_multichannel_start(TUYA_PWM_NUM_E *ch_id, uint8_t num)
{
    uint8_t i;

    if (ch_id == NULL || num == 0u) {
        return OPRT_INVALID_PARM;
    }
#if !defined(JIELI_SELECTED_CHIP_WL83)
    /* The vendor's own example for this part states that after open only a
     * single channel group can be selected. A "multi-channel start" here would
     * silently leave all but the last channel unconfigured. */
    (void)i;
    return OPRT_NOT_SUPPORTED;
#else
    /* Not atomic: the vendor ioctl sets one group at a time, so consecutive
     * starts are the closest honest equivalent. */
    for (i = 0; i < num; i++) {
        OPERATE_RET rt = tkl_pwm_start(ch_id[i]);
        if (rt != OPRT_OK) {
            return rt;
        }
    }
    return OPRT_OK;
#endif
}

OPERATE_RET tkl_pwm_multichannel_stop(TUYA_PWM_NUM_E *ch_id, uint8_t num)
{
    uint8_t i;

    if (ch_id == NULL || num == 0u) {
        return OPRT_INVALID_PARM;
    }
#if !defined(JIELI_SELECTED_CHIP_WL83)
    (void)i;
    return OPRT_NOT_SUPPORTED;
#else
    for (i = 0; i < num; i++) {
        OPERATE_RET rt = tkl_pwm_stop(ch_id[i]);
        if (rt != OPRT_OK) {
            return rt;
        }
    }
    return OPRT_OK;
#endif
}

OPERATE_RET tkl_pwm_cap_start(TUYA_PWM_NUM_E ch_id, const TUYA_PWM_CAP_IRQ_T *cfg)
{
    (void)ch_id;
    (void)cfg;
    /* Input capture runs through the timer block and its own ioctl set, which
     * this backend does not drive. */
    return OPRT_NOT_SUPPORTED;
}

OPERATE_RET tkl_pwm_cap_stop(TUYA_PWM_NUM_E ch_id)
{
    (void)ch_id;
    return OPRT_NOT_SUPPORTED;
}
