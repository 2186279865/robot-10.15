#include "tft.h"
#include "delay.h"
#include "tft_font.h"

/* P2: 3V3, GND, PB15/MOSI, PB13/SCK, PB12/CS, PB14/DC, PC5/RES, PB1/BL.
 * Software SPI mode 0, MSB first. PB14 is GPIO, not SPI2 MISO.
 */
#define SCK GPIO_Pin_13
#define MOSI GPIO_Pin_15
#define CS GPIO_Pin_12
#define DC GPIO_Pin_14
#define BL GPIO_Pin_1
#define RES GPIO_Pin_5
volatile uint32_t g_tft_stage = 0U;

static void spi_pause(void)
{
    volatile uint32_t n;
    /* Conservative clock below 2 MHz at the inherited 168 MHz core clock. */
    for (n = 0U; n < 12U; ++n) { __NOP(); }
}

static void write_byte(uint8_t value)
{
    uint8_t bit;
    for (bit = 0U; bit < 8U; ++bit)
    {
        GPIO_ResetBits(GPIOB, SCK);
        GPIO_WriteBit(GPIOB, MOSI, (value & 0x80U) ? Bit_SET : Bit_RESET);
        spi_pause();
        GPIO_SetBits(GPIOB, SCK);
        spi_pause();
        value <<= 1;
    }
    GPIO_ResetBits(GPIOB, SCK);
}

static void command(uint8_t value)
{
    GPIO_ResetBits(GPIOB, CS | DC);
    write_byte(value);
    GPIO_SetBits(GPIOB, CS);
}

static void data(uint8_t value)
{
    GPIO_SetBits(GPIOB, DC);
    GPIO_ResetBits(GPIOB, CS);
    write_byte(value);
    GPIO_SetBits(GPIOB, CS);
}

static void address16(uint16_t value)
{
    data((uint8_t)(value >> 8));
    data((uint8_t)value);
}

static void window(uint16_t x, uint16_t y, uint16_t w, uint16_t h)
{
    command(0x2A);
    address16(x + TFT_X_OFFSET);
    address16(x + w - 1U + TFT_X_OFFSET);
    command(0x2B);
    address16(y + TFT_Y_OFFSET);
    address16(y + h - 1U + TFT_Y_OFFSET);
    command(0x2C);
}

void TFT_Backlight(uint8_t on)
{
    GPIO_WriteBit(GPIOB, BL,
        (on == TFT_BACKLIGHT_ACTIVE_HIGH) ? Bit_SET : Bit_RESET);
}

void TFT_Init(void)
{
    GPIO_InitTypeDef gpio;
    g_tft_stage = 3U;
    RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOB | RCC_AHB1Periph_GPIOC, ENABLE);
    GPIO_SetBits(GPIOB, CS | DC);
    GPIO_ResetBits(GPIOB, SCK | MOSI);
    /* Do not deliberately blank the display throughout initialization. */
    TFT_Backlight(1U);
    GPIO_SetBits(GPIOC, RES);
    GPIO_StructInit(&gpio);
    gpio.GPIO_Mode = GPIO_Mode_OUT;
    gpio.GPIO_OType = GPIO_OType_PP;
    gpio.GPIO_PuPd = GPIO_PuPd_NOPULL;
    gpio.GPIO_Speed = GPIO_Speed_25MHz;
    gpio.GPIO_Pin = SCK | MOSI | CS | DC | BL;
    GPIO_Init(GPIOB, &gpio);
    gpio.GPIO_Pin = RES;
    GPIO_Init(GPIOC, &gpio);
    g_tft_stage = 4U;
    Delay_ms(20U);
    GPIO_ResetBits(GPIOC, RES);
    Delay_ms(100U);
    GPIO_SetBits(GPIOC, RES);
    Delay_ms(50U);
    g_tft_stage = 5U;
    command(0x01); /* Software reset in addition to the external reset pulse. */
    Delay_ms(150U);
#include "tft_init.inc"
    Delay_ms(100U);
    g_tft_stage = 6U;
    /* White makes a working panel distinguishable from a dark backlight. */
    TFT_Clear(TFT_WHITE);
    TFT_Backlight(1U);
    g_tft_stage = 7U;
    Delay_ms(1500U);
}

void TFT_FillRect(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t color)
{
    uint32_t count;
    if (x >= TFT_WIDTH || y >= TFT_HEIGHT || w == 0U || h == 0U) return;
    if (w > TFT_WIDTH - x) w = TFT_WIDTH - x;
    if (h > TFT_HEIGHT - y) h = TFT_HEIGHT - y;
    window(x, y, w, h);
    GPIO_SetBits(GPIOB, DC);
    GPIO_ResetBits(GPIOB, CS);
    count = (uint32_t)w * h;
    while (count--)
    {
        write_byte((uint8_t)(color >> 8));
        write_byte((uint8_t)color);
    }
    GPIO_SetBits(GPIOB, CS);
}

void TFT_Clear(uint16_t color)
{
    TFT_FillRect(0U, 0U, TFT_WIDTH, TFT_HEIGHT, color);
}

void TFT_Text(uint16_t x, uint16_t y, const char *text, uint16_t fg, uint16_t bg)
{
    uint8_t ch, row, col;
    uint16_t color;
    while (*text && x + 8U <= TFT_WIDTH && y + 16U <= TFT_HEIGHT)
    {
        ch = (uint8_t)*text++;
        if (ch < 32U || ch > 126U) ch = '?';
        window(x, y, 8U, 16U);
        GPIO_SetBits(GPIOB, DC);
        GPIO_ResetBits(GPIOB, CS);
        for (row = 0U; row < 16U; ++row)
            for (col = 0U; col < 8U; ++col)
            {
                color = (tft_font[ch - 32U][row] & (0x80U >> col)) ? fg : bg;
                write_byte((uint8_t)(color >> 8));
                write_byte((uint8_t)color);
            }
        GPIO_SetBits(GPIOB, CS);
        x += 8U;
    }
}
