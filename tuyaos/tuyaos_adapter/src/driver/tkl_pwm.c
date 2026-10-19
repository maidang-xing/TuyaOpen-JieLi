#include "tkl_pwm.h"
#include "tuya_error_code.h"

#include <stdint.h>

#include "device.h"
#if defined(JIELI_SELECTED_CHIP_WL83)
/* wl83's asm/pwm.h is self-contained: generic/typedef.h, generic/ioctl.h and
 * device/device.h only. */
#include "asm/pwm.h"
#else
/* wl82's asm/pwm.h begins with #include "includes.h", which drags in the
 * vendor filesystem header and redefines FILE / fread / fwrite / fseek against
 * the libc the adapter already uses - six hard redefinition errors. The parts
 * this file needs are the platform-data shape and the ioctl command words.
 *
 * The command words are spelled out rather than pulled from asm/pwm.h because
 * that header's _IOW ends in _IOC_TYPECHECK, which forms sizeof(t[1]) and so
 * only accepts the vendor's own scalar typedefs; reaching those means including
 * generic/typedef.h, which pulls asm/cpu.h and string.h. The encoding is
 * fixed and public (generic/ioctl.h): dir<<30 | size<<16 | type<<8 | nr, with
 * _IOC_WRITE = 1 and a u32 payload of size 4. The values below are what
 * _IOW('P', n, u32) expands to; they are asserted against the real macro in
 * tests/ if that ever changes.
 */
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
#endif
