#ifndef __JIELI_PIN_H__
#define __JIELI_PIN_H__

#include "tuya_cloud_types.h"

/*
 * JieLi pin naming -> the numeric pin a Tuya TKL call takes.
 *
 * The two are the SAME NUMBER, so no lookup table is involved. The vendor
 * header spells a pin as IO_PORT<port>_<bit> and defines it arithmetically:
 *
 *     #define IO_GROUP_NUM  16
 *     #define IO_PORTA_07   (IO_GROUP_NUM * 0 + 7)   ->  7
 *     #define IO_PORTB_01   (IO_GROUP_NUM * 1 + 1)   -> 17
 *     #define IO_PORTD_07   (IO_GROUP_NUM * 3 + 7)   -> 55
 *     #define IO_PORTE_11   (IO_GROUP_NUM * 4 + 11)  -> 75
 *
 * Port A occupies 0..15, B 16..31, and so on, which is the same flat layout
 * TUYA_GPIO_NUM_E uses. So "PA7" == IO_PORTA_07 == 7, and a board that says
 * IO_PORTA_05 in Kconfig is pin 5 to the TKL.
 *
 * What the numbers do NOT tell you is which of them exist. That comes from the
 * per-port masks in the chip's asm/gpio.h, and the two chips differ:
 *
 *   wl82  asm/gpio.h declares A..I at 16 bits each and sets IO_MAX_NUM to
 *         IO_PORTI_15 + 1 == 144, but WL82.h has no JL_PORTI_BASE and no
 *         JL_PORTI - it stops at JL_PORTH. Port I therefore has names but no
 *         register block, so this file stops at H (pin 127). The PR and USB
 *         entries sit above IO_MAX_NUM and are not general-purpose ports.
 *
 *   wl83  the masks are not uniform - PORTC is 0x3fff and PORTF is 0x003f - so
 *         PC14, PC15 and PF6..PF15 do not exist. The USB pairs are sparse
 *         (IO_PORT_USB_DPA/DMA at 96/97, IO_PORT_USB_DPB/DMB at 112/113) and
 *         IO_MAX_NUM stops at IO_PORT_USB_DMB + 1, so the R and V ports beyond
 *         it are not addressable through this numbering.
 *
 *         PORTF needs a caveat: WL83.h aliases JL_PORTF to JL_PORTTZ_TypeDef
 *         over JL_PFTZ_BASE and comments out the normal JL_PORTF, so PF0..PF5
 *         are declared as pins (mask 0x003f, and the SDK's own SDIO header uses
 *         IO_PORTF_04..07 as mux targets) but are not obviously driven by the
 *         plain GPIO register block. They are kept here because the GPIO
 *         header's own mask includes them, and they sit above
 *         TUYA_GPIO_NUM_MAX anyway so tkl_gpio never reaches them; treat PF as a
 *         mux target, not a verified GPIO, until that is measured.
 *
 * This file encodes exactly that. It is derived from the SDK headers, not from
 * a datasheet: a mask says how wide the GPIO controller's port is, not whether
 * a given die/package bonds the pin out. Electrical usability on a specific
 * board is still a schematic question.
 *
 * Note also that the ADC channel numbering is a DIFFERENT namespace and is not
 * derived from this one - AD_CH_PB01 is channel 3 while IO_PORTB_01 is pin 17.
 * tkl_adc.c takes vendor channel numbers directly.
 *
 * The Tuya contract reaches as far as TUYA_GPIO_NUM_MAX, which is 128: ports
 * A..H on wl82 and, on wl83, A..F plus the sparse USB pairs. Pins above each
 * chip's populated set are still refused by jieli_pin_exists() below.
 */

#define JIELI_PORT_WIDTH 16u

#if defined(JIELI_SELECTED_CHIP_WL83)

/* asm/gpio.h: PORTA 0xffff, PORTB 0xffff, PORTC 0x3fff, PORTD 0xffff,
 * PORTE 0xffff, PORTF 0x003f, plus the two sparse USB pairs. */
static inline int jieli_pin_exists(uint32_t pin)
{
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
        /* Ports R and V are defined but sit above IO_MAX_NUM, so the vendor's
         * own numbering does not reach them. */
        return 0;
    }
}

#else /* JIELI_SELECTED_CHIP_WL82 */

/* WL82.h defines JL_PORTA..JL_PORTH, 16 bits each. It stops at H: asm/gpio.h
 * names IO_PORTI_00..15 and derives IO_MAX_NUM from them, but no JL_PORTI base
 * or pointer exists, so those names have no register block behind them. */
static inline int jieli_pin_exists(uint32_t pin)
{
    return pin < 128u;
}

#endif

#endif /* __JIELI_PIN_H__ */
