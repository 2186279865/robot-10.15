/* stepper.c - EMM V5 步进电机模块的独立实现
 *
 * 注意：当前工程【没有】把本文件加入 Keil 工程，【也没有】编译它。
 * 实际参与编译的是 Drive\Include\stepper_inline.h（由 main.c 内联包含），
 * 原因见该文件头部的说明：Keil 一直开着工程，外部改 .uvprojx 会被覆盖。
 *
 * 以后若要正式拆分：
 *   1. 在 Keil 里右键 Drive 分组 → Add Existing Files → 选本文件；
 *   2. 删掉 main.c 里的 #include "stepper_inline.h"（否则函数重复定义）；
 *   3. 确认本文件已包含下面这些头文件，编译一次。
 */

#include "stepper.h"
#include "delay.h"

#define STEPPER_RX_SIZE 32U

static volatile uint32_t stepper_ms;
static volatile uint16_t rx_head, rx_tail;
static volatile uint8_t  rx_error;
static uint8_t rx_buf[STEPPER_RX_SIZE];

/* ---------------- TIM2：独立 1ms 时基 ---------------- */

uint32_t Stepper_Millis(void)
{
    return stepper_ms;
}

static void stepper_timer_init(void)
{
    TIM_TimeBaseInitTypeDef t;
    NVIC_InitTypeDef n;
    RCC_ClocksTypeDef clocks;
    uint32_t timer_clock;

    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM2, ENABLE);

    RCC_GetClocksFreq(&clocks);
    timer_clock = clocks.PCLK1_Frequency;
    if ((RCC->CFGR & RCC_CFGR_PPRE1) != 0U)
    {
        timer_clock *= 2U;                  /* APB1 分频不为 1 时定时器时钟 ×2 */
    }

    TIM_TimeBaseStructInit(&t);
    t.TIM_Prescaler   = (uint16_t)(timer_clock / 1000000UL - 1UL);
    t.TIM_Period      = 999U;
    t.TIM_CounterMode = TIM_CounterMode_Up;
    TIM_TimeBaseInit(TIM2, &t);

    TIM_ClearITPendingBit(TIM2, TIM_IT_Update);
    n.NVIC_IRQChannel = TIM2_IRQn;
    n.NVIC_IRQChannelPreemptionPriority = 1;
    n.NVIC_IRQChannelSubPriority = 0;
    n.NVIC_IRQChannelCmd = ENABLE;
    NVIC_Init(&n);
    TIM_ITConfig(TIM2, TIM_IT_Update, ENABLE);
    TIM_Cmd(TIM2, ENABLE);
}

void TIM2_IRQHandler(void)
{
    if (TIM_GetITStatus(TIM2, TIM_IT_Update) != RESET)
    {
        TIM_ClearITPendingBit(TIM2, TIM_IT_Update);
        stepper_ms++;
    }
}

/* ---------------- USART2：PA2 只发 ---------------- */

static void stepper_uart_init(void)
{
    GPIO_InitTypeDef g;
    USART_InitTypeDef u;
    NVIC_InitTypeDef n;

    RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOA, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_USART2, ENABLE);

    GPIO_StructInit(&g);
    g.GPIO_Pin   = GPIO_Pin_2;              /* 只配 TX，PA3 留空 */
    g.GPIO_Mode  = GPIO_Mode_AF;
    g.GPIO_Speed = GPIO_Speed_50MHz;
    g.GPIO_OType = GPIO_OType_PP;
    g.GPIO_PuPd  = GPIO_PuPd_UP;
    GPIO_PinAFConfig(GPIOA, GPIO_PinSource2, GPIO_AF_USART2);
    GPIO_Init(GPIOA, &g);

    USART_StructInit(&u);
    u.USART_BaudRate = 115200UL;
    u.USART_Mode     = USART_Mode_Tx | USART_Mode_Rx;
    USART_Init(USART2, &u);
    USART_Cmd(USART2, ENABLE);

    n.NVIC_IRQChannel = USART2_IRQn;
    n.NVIC_IRQChannelPreemptionPriority = 2;
    n.NVIC_IRQChannelSubPriority = 0;
    n.NVIC_IRQChannelCmd = ENABLE;
    NVIC_Init(&n);
    USART_ITConfig(USART2, USART_IT_RXNE, ENABLE);
}

void USART2_IRQHandler(void)
{
    uint32_t sr = USART2->SR;
    uint8_t b;

    if (sr & (USART_SR_RXNE | USART_SR_ORE | USART_SR_FE | USART_SR_NE | USART_SR_PE))
    {
        b = (uint8_t)USART2->DR;
        if (sr & (USART_SR_ORE | USART_SR_FE | USART_SR_NE | USART_SR_PE))
        {
            rx_error = 1U;
            return;
        }
        {
            uint16_t next = (uint16_t)((rx_head + 1U) % STEPPER_RX_SIZE);
            if (next == rx_tail)
            {
                rx_error = 1U;
                return;
            }
            rx_buf[rx_head] = b;
            rx_head = next;
        }
    }
}

/* ---------------- 发送与指令构造 ---------------- */

uint8_t Stepper_SendFrame(const uint8_t *frame, uint8_t len)
{
    uint8_t i;
    uint32_t begin;

    for (i = 0U; i < len; i++)
    {
        begin = Stepper_Millis();
        while (USART_GetFlagStatus(USART2, USART_FLAG_TXE) == RESET)
        {
            if ((uint32_t)(Stepper_Millis() - begin) > 10UL)
            {
                return 0U;
            }
        }
        USART_SendData(USART2, frame[i]);
    }

    begin = Stepper_Millis();
    while (USART_GetFlagStatus(USART2, USART_FLAG_TC) == RESET)
    {
        if ((uint32_t)(Stepper_Millis() - begin) > 10UL)
        {
            return 0U;
        }
    }
    return 1U;
}

static uint8_t build_enable(uint8_t *o, uint8_t addr, uint8_t on)
{
    o[0] = addr; o[1] = 0xF3; o[2] = 0xAB; o[3] = on ? 1U : 0U;
    o[4] = 0U;   o[5] = 0x6B;
    return 6U;
}

static uint8_t build_position(uint8_t *o, uint8_t addr, uint8_t dir,
                             int32_t pulses, uint16_t rpm, uint8_t accel,
                             uint8_t absolute)
{
    uint32_t n = (pulses < 0) ? (uint32_t)(-pulses) : (uint32_t)pulses;

    o[0]  = addr;
    o[1]  = 0xFD;
    o[2]  = (uint8_t)(dir ? 1U : 0U);
    o[3]  = (uint8_t)(rpm >> 8);
    o[4]  = (uint8_t)rpm;
    o[5]  = accel;
    o[6]  = (uint8_t)(n >> 24);
    o[7]  = (uint8_t)(n >> 16);
    o[8]  = (uint8_t)(n >> 8);
    o[9]  = (uint8_t)n;
    o[10] = absolute ? 1U : 0U;
    o[11] = 0U;
    o[12] = 0x6B;
    return 13U;
}

static uint8_t build_speed(uint8_t *o, uint8_t addr, uint8_t dir, uint16_t rpm,
                           uint8_t accel)
{
    o[0] = addr; o[1] = 0xF6; o[2] = (uint8_t)(dir ? 1U : 0U);
    o[3] = (uint8_t)(rpm >> 8); o[4] = (uint8_t)rpm;
    o[5] = accel; o[6] = 0U; o[7] = 0x6B;
    return 8U;
}

static uint8_t build_stop(uint8_t *o, uint8_t addr)
{
    o[0] = addr; o[1] = 0xFE; o[2] = 0x98; o[3] = 0U; o[4] = 0x6B;
    return 5U;
}

/* ---------------- 对外接口（单机，带地址） ---------------- */

void Stepper_Init(void)
{
    rx_head = 0U;
    rx_tail = 0U;
    rx_error = 0U;
    stepper_ms = 0U;

    stepper_uart_init();
    stepper_timer_init();
}

uint8_t Stepper_EnableAt(uint8_t addr, uint8_t on)
{
    uint8_t f[6];
    uint8_t n = build_enable(f, addr, on);
    return Stepper_SendFrame(f, n);
}

uint8_t Stepper_ZeroHereAt(uint8_t addr)
{
    uint8_t f[4];
    f[0] = addr; f[1] = 0x0A; f[2] = 0x6D; f[3] = 0x6B;
    return Stepper_SendFrame(f, 4U);
}

uint8_t Stepper_MoveRelativeAt(uint8_t addr, uint8_t dir, int32_t pulses,
                               uint32_t wait_ms)
{
    uint8_t f[13];
    uint8_t n = build_position(f, addr, dir, pulses,
                               STEPPER_SPEED_RPM, STEPPER_ACCEL, 0U);

    if (!Stepper_SendFrame(f, n))
    {
        return 0U;
    }
    Delay_ms(wait_ms);
    return 1U;
}

uint8_t Stepper_MoveAbsoluteAt(uint8_t addr, uint8_t dir, int32_t pulses,
                               uint32_t wait_ms)
{
    uint8_t f[13];
    uint8_t n = build_position(f, addr, dir, pulses,
                               STEPPER_SPEED_RPM, STEPPER_ACCEL, 1U);

    if (!Stepper_SendFrame(f, n))
    {
        return 0U;
    }
    Delay_ms(wait_ms);
    return 1U;
}

uint8_t Stepper_RunSpeedAt(uint8_t addr, uint8_t dir, uint16_t rpm)
{
    uint8_t f[8];
    uint8_t n = build_speed(f, addr, dir, rpm, 0U);
    return Stepper_SendFrame(f, n);
}

uint8_t Stepper_StopAt(uint8_t addr)
{
    uint8_t f[5];
    uint8_t n = build_stop(f, addr);
    return Stepper_SendFrame(f, n);
}

/* ---------------- 全部电机：按 STEPPER_MOTOR_1..COUNT 逐个发送 ---------------- */

static const uint8_t stepper_all_addr[STEPPER_MOTOR_COUNT] = {
    STEPPER_MOTOR_1, STEPPER_MOTOR_2, STEPPER_MOTOR_3, STEPPER_MOTOR_4
};

uint8_t Stepper_StopAll(void)
{
    uint8_t i, ok = 1U;
    for (i = 0U; i < STEPPER_MOTOR_COUNT; i++)
    {
        if (!Stepper_StopAt(stepper_all_addr[i])) ok = 0U;
    }
    return ok;
}

uint8_t Stepper_EnableAll(uint8_t on)
{
    uint8_t i, ok = 1U;
    for (i = 0U; i < STEPPER_MOTOR_COUNT; i++)
    {
        if (!Stepper_EnableAt(stepper_all_addr[i], on)) ok = 0U;
    }
    return ok;
}

uint8_t Stepper_ZeroAll(void)
{
    uint8_t i, ok = 1U;
    for (i = 0U; i < STEPPER_MOTOR_COUNT; i++)
    {
        if (!Stepper_ZeroHereAt(stepper_all_addr[i])) ok = 0U;
    }
    return ok;
}

uint8_t Stepper_MoveAll(uint8_t dir, int32_t pulses, uint32_t wait_ms)
{
    uint8_t i, ok = 1U;
    uint8_t f[13];
    uint8_t n;

    for (i = 0U; i < STEPPER_MOTOR_COUNT; i++)
    {
        n = build_position(f, stepper_all_addr[i], dir, pulses,
                           STEPPER_SPEED_RPM, STEPPER_ACCEL, 0U);
        if (!Stepper_SendFrame(f, n)) ok = 0U;
    }
    Delay_ms(wait_ms);
    return ok;
}

uint8_t Stepper_RunSpeedAll(uint8_t dir, uint16_t rpm)
{
    uint8_t i, ok = 1U;
    uint8_t f[8];
    uint8_t n;

    for (i = 0U; i < STEPPER_MOTOR_COUNT; i++)
    {
        n = build_speed(f, stepper_all_addr[i], dir, rpm, 0U);
        if (!Stepper_SendFrame(f, n)) ok = 0U;
    }
    return ok;
}

uint8_t Stepper_Read(uint8_t *byte)
{
    if (rx_head == rx_tail)
    {
        return 0U;
    }
    *byte = rx_buf[rx_tail];
    rx_tail = (uint16_t)((rx_tail + 1U) % STEPPER_RX_SIZE);
    return 1U;
}

uint8_t Stepper_RxError(void)
{
    return rx_error;
}
