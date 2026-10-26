/**
 * @file lcd_mipi_st7701s_rotate90.c
 * @brief Wrapper to compile ST7701S 480x800 Rotate 90 driver when enabled
 */

#if defined(JIELI_SELECTED_CHIP_WL83)

#include "app_config.h"

#if defined(TCFG_LCD_MIPI_ST7701S_480x800_ROTATE_90) && (TCFG_LCD_MIPI_ST7701S_480x800_ROTATE_90 == 1)
#include "mipi_lcd/lcd_mipi_st7701s_480x800_rotate_90.c"
#endif

#endif
