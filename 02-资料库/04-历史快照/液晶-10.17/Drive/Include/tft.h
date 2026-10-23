#ifndef TFT_H
#define TFT_H

#include "stm32f4xx.h"

#define TFT_WIDTH       128U
#define TFT_HEIGHT      160U
/* Adjust only after checking the one-pixel border on the actual panel. */
#define TFT_X_OFFSET    0U
#define TFT_Y_OFFSET    0U
#define TFT_MADCTL      0xC0U /* Vendor orientation; use 0xC8 if red/blue swap. */
/* PB1 is a logic output: use a suitable backlight driver if necessary. */
#define TFT_BACKLIGHT_ACTIVE_HIGH 1

#define TFT_BLACK       0x0000U
#define TFT_WHITE       0xFFFFU
#define TFT_RED         0xF800U
#define TFT_GREEN       0x07E0U
#define TFT_BLUE        0x001FU
#define TFT_YELLOW      0xFFE0U
#define TFT_CYAN        0x07FFU

void TFT_Init(void);
/* Keil Watch: 1=main, 2=boot blink, 3=GPIO, 4=reset,
 * 5=commands, 6=clear, 7=init complete, 8=test loop. */
extern volatile uint32_t g_tft_stage;
void TFT_Backlight(uint8_t on);
void TFT_FillRect(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t color);
void TFT_Clear(uint16_t color);
void TFT_Text(uint16_t x, uint16_t y, const char *text, uint16_t fg, uint16_t bg);

#endif
