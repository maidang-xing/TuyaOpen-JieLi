/**
 * @file jieli_atomic_fix.c
 * @brief WL82 atomic fetch-and-sub runtime override for libcompiler_rt.a
 *
 * @copyright Copyright (c) 2021-2026 Tuya Inc. All Rights Reserved.
 */

#include <stdint.h>

#if defined(JIELI_SELECTED_CHIP_WL82)

/*
 * WL82's prebuilt libcompiler_rt.a(sync_fetch_and_sub.o) computes (val - old)
 * instead of (old - val) before writing back to memory across all four widths
 * (1, 2, 4, 8), corrupting atomic decrements such as __atomic_sub_fetch().
 * WL83's SDK fixed this to (old - val).
 *
 * Provide strong definitions here (linked in $(OBJS) ahead of libcompiler_rt.a
 * and retained across LTO with -u flags in sdk_overlay.py) mirroring WL83's
 * IRQ-disabled read-modify-write + csync sequence.
 *
 * Clang rejects defining __sync_fetch_and_sub_{1,2,4,8} directly as C
 * identifiers because they are builtin names, so bind the symbol names via
 * asm labels (matching tal_cxx_shim.c).
 */
extern void __rt_local_irq_disable(void);
extern void __rt_local_irq_enable(void);

#define DEFINE_JIELI_SYNC_FETCH_AND_SUB(suffix, type)                                                                  \
    type jieli_sync_fetch_and_sub_##suffix(volatile void *ptr, type val) __asm__("__sync_fetch_and_sub_" #suffix);     \
    type jieli_sync_fetch_and_sub_##suffix(volatile void *ptr, type val)                                               \
    {                                                                                                                  \
        __rt_local_irq_disable();                                                                                      \
        type old              = *(volatile type *)ptr;                                                                 \
        *(volatile type *)ptr = (type)(old - val);                                                                     \
        __asm__ volatile("csync" ::: "memory");                                                                        \
        __rt_local_irq_enable();                                                                                       \
        return old;                                                                                                    \
    }

DEFINE_JIELI_SYNC_FETCH_AND_SUB(1, uint8_t)
DEFINE_JIELI_SYNC_FETCH_AND_SUB(2, uint16_t)
DEFINE_JIELI_SYNC_FETCH_AND_SUB(4, uint32_t)
DEFINE_JIELI_SYNC_FETCH_AND_SUB(8, uint64_t)

#else
typedef int jieli_atomic_fix_unused_t;
#endif
