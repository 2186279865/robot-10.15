/* stepper_inline.h - EMM V5 步进电机模块的实现（供本工程内联编译用）
 *
 * 为什么实现放在头文件里：
 *   Keil 一直开着本工程，外部修改 .uvprojx 会被它保存时覆盖，新加的 .c 文件
 *   无法参与编译。放在已在包含路径（..\Drive\Include）的头文件里，由 main.c
 *   编译时带入，就不需要改工程文件。
 *
 * 以后若把 Drive\Source\stepper.c 正式加进 Keil 工程，请删掉 main.c 里的
 * #include "stepper_inline.h"，避免函数重复定义。
 *
 * 资源占用（与现有模块无冲突）：
 *   USART2 + PA2(TX) + PA3(RX) —— 只有本模块使用
 *   TIM2 + TIM2_IRQn  —— 独立 1ms 计数；舵机用 TIM4，延时用 SysTick
 *
 * 接收（PA3）：PA3 要接到驱动器的 TXD 才能收到回包。**只接 Z 轴（地址 1）这一路**，
 * 四路 TX 不能因为地址不同就直接并联（推挽输出会互相打架），要并联必须各串二极管/
 * 电阻或者改用 RS-485。没接 PA3 时接收中断不会来，查询一律超时 E04，不影响发送。
 */

#include "stepper.h"
#include "delay.h"

#define STEPPER_RX_SIZE 64U

static volatile uint32_t stepper_ms;            /* TIM2 累加的毫秒数 */
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

    /* APB1 分频不为 1 时，定时器时钟是 PCLK1 的 2 倍（STM32F4 时钟树规则） */
    RCC_GetClocksFreq(&clocks);
    timer_clock = clocks.PCLK1_Frequency;
    if ((RCC->CFGR & RCC_CFGR_PPRE1) != 0U)
    {
        timer_clock *= 2U;
    }

    TIM_TimeBaseStructInit(&t);
    t.TIM_Prescaler = (uint16_t)(timer_clock / 1000000UL - 1UL);   /* 计数频率 1MHz */
    t.TIM_Period    = 999U;                                        /* 1000 计数 = 1ms */
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

/* ---------------- USART2：PA2 发 / PA3 收 ---------------- */

static void stepper_uart_init(void)
{
    GPIO_InitTypeDef g;
    USART_InitTypeDef u;
    NVIC_InitTypeDef n;

    RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOA, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_USART2, ENABLE);

    GPIO_StructInit(&g);
    g.GPIO_Pin   = GPIO_Pin_2 | GPIO_Pin_3;     /* PA2=TX、PA3=RX */
    g.GPIO_Mode  = GPIO_Mode_AF;
    g.GPIO_Speed = GPIO_Speed_50MHz;
    g.GPIO_OType = GPIO_OType_PP;
    g.GPIO_PuPd  = GPIO_PuPd_UP;                /* 没接 TXD 时保持高电平，不产生假起始位 */
    GPIO_PinAFConfig(GPIOA, GPIO_PinSource2, GPIO_AF_USART2);
    GPIO_PinAFConfig(GPIOA, GPIO_PinSource3, GPIO_AF_USART2);
    GPIO_Init(GPIOA, &g);

    USART_StructInit(&u);
    u.USART_BaudRate = 115200UL;
    u.USART_Mode     = USART_Mode_Tx | USART_Mode_Rx;
    USART_Init(USART2, &u);
    USART_Cmd(USART2, ENABLE);

    /* 接收中断：抢占优先级 3（毫秒时基 TIM2=1、USB OTG=2 都比它高），
     * 中断里只做"读 DR -> 进环形缓冲"，解析放到主循环。 */
    USART_ClearFlag(USART2, USART_FLAG_ORE | USART_FLAG_NE | USART_FLAG_FE | USART_FLAG_PE);
    USART_ITConfig(USART2, USART_IT_RXNE, ENABLE);
    n.NVIC_IRQChannel = USART2_IRQn;
    n.NVIC_IRQChannelPreemptionPriority = 3;
    n.NVIC_IRQChannelSubPriority = 0;
    n.NVIC_IRQChannelCmd = ENABLE;
    NVIC_Init(&n);
}

void USART2_IRQHandler(void)
{
    uint16_t next;

    if (USART_GetITStatus(USART2, USART_IT_RXNE) != RESET)
    {
        uint8_t b = (uint8_t)(USART2->DR & 0xFFU);   /* 读 DR 同时清 RXNE */

        next = (uint16_t)((rx_head + 1U) % STEPPER_RX_SIZE);
        if (next == rx_tail)
        {
            rx_error = 1U;                            /* 缓冲满：丢弃新字节并记账 */
        }
        else
        {
            rx_buf[rx_head] = b;
            rx_head = next;
        }
    }

    /* 溢出/帧错误等：清标志并记账，否则会反复进中断 */
    if (USART_GetFlagStatus(USART2, USART_FLAG_ORE) != RESET)
    {
        (void)USART2->SR;
        (void)USART2->DR;
        rx_error = 1U;
    }
    if (USART_GetFlagStatus(USART2, USART_FLAG_FE) != RESET)
    {
        (void)USART2->SR;
        (void)USART2->DR;
        rx_error = 1U;
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
                return 0U;                  /* 10ms 超时，不阻塞死等 */
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
    o[6]  = (uint8_t)(n >> 24);             /* 大端：高字节在前 */
    o[7]  = (uint8_t)(n >> 16);
    o[8]  = (uint8_t)(n >> 8);
    o[9]  = (uint8_t)n;
    o[10] = absolute ? 1U : 0U;             /* raF: 0=相对 1=绝对 */
    o[11] = 0U;                             /* snF: 0=立即执行 */
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

/* ---------------- 串口自检（回环） ---------------- */

/* 把 PA2 临时当普通输入读一下：有外部低电平（例如接 GND 或回环线）时为 1 */
static uint8_t pa2_external_low(void)
{
    GPIO_InitTypeDef g;
    uint8_t low;

    GPIO_StructInit(&g);
    g.GPIO_Pin   = GPIO_Pin_2;
    g.GPIO_Mode  = GPIO_Mode_IN;
    g.GPIO_PuPd  = GPIO_PuPd_UP;
    g.GPIO_Speed = GPIO_Speed_2MHz;
    GPIO_Init(GPIOA, &g);
    Delay_ms(2);
    low = (uint8_t)(GPIO_ReadInputDataBit(GPIOA, GPIO_Pin_2) == Bit_RESET);

    /* 恢复 PA2 为 USART2 复用功能 */
    g.GPIO_Mode  = GPIO_Mode_AF;
    g.GPIO_OType = GPIO_OType_PP;
    g.GPIO_Speed = GPIO_Speed_50MHz;
    g.GPIO_PuPd  = GPIO_PuPd_UP;
    GPIO_Init(GPIOA, &g);
    GPIO_PinAFConfig(GPIOA, GPIO_PinSource2, GPIO_AF_USART2);
    return low;
}

uint8_t Stepper_LoopbackTest(void)
{
    uint8_t saw_line;
    uint8_t got = 0U;
    uint32_t begin;
    uint8_t i;

    saw_line = pa2_external_low();          /* PA2 上是否有外部低电平 */

    /* 清掉残留接收字节 */
    while (USART_GetFlagStatus(USART2, USART_FLAG_RXNE) != RESET)
    {
        (void)USART2->DR;
    }

    for (i = 0U; i < 2U; i++)
    {
        USART_SendData(USART2, (uint8_t)(0x55U + i));   /* 0x55, 0x56 */

        begin = Stepper_Millis();
        while ((uint32_t)(Stepper_Millis() - begin) < 5UL)
        {
            if (USART_GetFlagStatus(USART2, USART_FLAG_RXNE) != RESET)
            {
                uint8_t rxb = (uint8_t)USART2->DR;
                if ((rxb == 0x55U) || (rxb == 0x56U))
                {
                    got = 1U;
                }
            }
        }
    }

    /* 没有回显但 PA2 被外部拉低：说明只接了地线、没接回环线，也算有接线 */
    if (!got && saw_line)
    {
        got = 2U;
    }
    return got;
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

/* 0E 52：解除堵转保护。堵转保护一旦触发，只发 F6/FD 电机也不会转，
 * 直到断电重启或在驱动器上清除；上电先发一帧成本很低。 */
uint8_t Stepper_ResetStallAt(uint8_t addr)
{
    uint8_t f[4];
    f[0] = addr; f[1] = 0x0E; f[2] = 0x52; f[3] = 0x6B;
    return Stepper_SendFrame(f, 4U);
}

uint8_t Stepper_MoveRelativeAt2(uint8_t addr, uint8_t dir, int32_t pulses,
                                uint16_t rpm, uint8_t accel, uint32_t wait_ms)
{
    uint8_t f[13];
    uint8_t n = build_position(f, addr, dir, pulses, rpm, accel, 0U);

    if (!Stepper_SendFrame(f, n))
    {
        return 0U;
    }
    /* 相对位置命令只发一次：驱动器自行走完，重复发送会叠加圈数 */
    Delay_ms(wait_ms);
    return 1U;
}

uint8_t Stepper_MoveRelativeAt(uint8_t addr, uint8_t dir, int32_t pulses,
                               uint32_t wait_ms)
{
    return Stepper_MoveRelativeAt2(addr, dir, pulses,
                                   STEPPER_SPEED_RPM, STEPPER_ACCEL, wait_ms);
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
/* 带加速度的 F6 速度模式：加速度档位越大，驱动器加速斜坡越缓、起转力矩越小。
 * accel = 0 或 1 时近乎直接启动，起转最猛。 */
uint8_t Stepper_RunSpeedAt2(uint8_t addr, uint8_t dir, uint16_t rpm, uint8_t accel)
{
    uint8_t f[8];
    uint8_t n = build_speed(f, addr, dir, rpm, accel);
    return Stepper_SendFrame(f, n);
}
uint8_t Stepper_StopAt(uint8_t addr)
{
    uint8_t f[5];
    uint8_t n = build_stop(f, addr);
    return Stepper_SendFrame(f, n);
}

/* ---------------- 全部电机：按 STEPPER_MOTOR_1..COUNT 逐个发送 ----------------
 * 地址表可在这里按实际接线调整；顺序发送 4 帧约 3ms，对运动同步影响可忽略。
 */
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

    /* 先把命令都发给每一台，再统一等一次到位时间（不叠加等待） */
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

uint8_t Stepper_NudgeAll(uint8_t dir, uint16_t rpm, uint32_t run_ms)
{
    uint8_t ok;
    uint32_t begin;

    ok = Stepper_RunSpeedAll(dir, rpm);
    begin = Stepper_Millis();
    while ((uint32_t)(Stepper_Millis() - begin) < run_ms)
    {
        /* 等运动时间，期间保持补发，防止偶发丢帧 */
        Delay_ms(1UL);
    }
    if (!Stepper_StopAll())
    {
        ok = 0U;
    }
    return ok;
}

/* 供以后做非阻塞控制使用：取一帧接收字节 */
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
