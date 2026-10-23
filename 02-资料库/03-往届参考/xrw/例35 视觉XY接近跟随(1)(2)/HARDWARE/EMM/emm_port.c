#include "emm_port.h"
#include <stdio.h>

#define RX_SIZE 128U
typedef struct {
    volatile uint16_t head, tail;
    volatile uint8_t error;
    uint8_t data[RX_SIZE];
} RxRing;
static RxRing motor_rx[3];
static RxRing console_rx;
static volatile uint32_t milliseconds;
static volatile uint8_t stop_latched;
static USART_TypeDef *const ports[3] = {USART2, USART3, UART4};
static const IRQn_Type irqs[3] = {USART2_IRQn, USART3_IRQn, UART4_IRQn};

static void push(RxRing *r, uint8_t b)
{
    uint16_t next=(uint16_t)((r->head + 1U) % RX_SIZE);
    if (next == r->tail) { r->error=1; return; }
    r->data[r->head]=b;
    r->head=next;
}
static uint8_t pop(RxRing *r, uint8_t *b)
{
    if (r->head == r->tail) return 0;
    *b=r->data[r->tail]; r->tail=(uint16_t)((r->tail+1U)%RX_SIZE);
    return 1;
}
static void gpio_uart(GPIO_TypeDef *port, uint16_t pins,
                      uint8_t tx, uint8_t rx, uint8_t af)
{
    GPIO_InitTypeDef g;
    GPIO_StructInit(&g);
    g.GPIO_Pin=pins; g.GPIO_Mode=GPIO_Mode_AF;
    g.GPIO_Speed=GPIO_Speed_50MHz; g.GPIO_OType=GPIO_OType_PP;
    g.GPIO_PuPd=GPIO_PuPd_UP;
    GPIO_PinAFConfig(port,tx,af); GPIO_PinAFConfig(port,rx,af);
    GPIO_Init(port,&g);
}
static void uart_setup(USART_TypeDef *port, IRQn_Type irq)
{
    USART_InitTypeDef u;
    NVIC_InitTypeDef n;
    USART_StructInit(&u);
    u.USART_BaudRate=EMM_BAUD;
    u.USART_Mode=USART_Mode_Rx | USART_Mode_Tx;
#if EMM_TX_ONLY
    if (port==USART2) u.USART_Mode=USART_Mode_Tx;
#endif
    USART_Init(port,&u); USART_Cmd(port,ENABLE);
#if EMM_TX_ONLY
    if (port==USART2) return; /* 电机TX不接，本测试不使用接收中断 */
#endif
    n.NVIC_IRQChannel=irq; n.NVIC_IRQChannelPreemptionPriority=2;
    n.NVIC_IRQChannelSubPriority=0; n.NVIC_IRQChannelCmd=ENABLE;
    NVIC_Init(&n); USART_ITConfig(port,USART_IT_RXNE,ENABLE);
}
void EmmPort_Init(void)
{
    TIM_TimeBaseInitTypeDef t;
    NVIC_InitTypeDef n;
    RCC_ClocksTypeDef clocks;
    uint32_t timer_clock;
    uint8_t i;
    /* USART1 PA9/PA10专用于视觉坐标和ACK；USART2 PA2仅控制X轴张大头。 */
    RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOA,ENABLE);
    RCC_APB2PeriphClockCmd(RCC_APB2Periph_USART1,ENABLE);
    gpio_uart(GPIOA,GPIO_Pin_9|GPIO_Pin_10,9,10,GPIO_AF_USART1);
    uart_setup(USART1,USART1_IRQn);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_USART2|RCC_APB1Periph_TIM2,ENABLE);
#if EMM_TX_ONLY
    {
        GPIO_InitTypeDef motor_tx;
        GPIO_StructInit(&motor_tx);
        motor_tx.GPIO_Pin=GPIO_Pin_2;
        motor_tx.GPIO_Mode=GPIO_Mode_AF;
        motor_tx.GPIO_Speed=GPIO_Speed_50MHz;
        motor_tx.GPIO_OType=GPIO_OType_PP;
        motor_tx.GPIO_PuPd=GPIO_PuPd_UP;
        GPIO_PinAFConfig(GPIOA,GPIO_PinSource2,GPIO_AF_USART2);
        GPIO_Init(GPIOA,&motor_tx); /* PA3不配置，留空 */
    }
#else
    gpio_uart(GPIOA,GPIO_Pin_2|GPIO_Pin_3,2,3,GPIO_AF_USART2);
#endif
#if EMM_CHANNEL_COUNT > 1
    RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOB|RCC_AHB1Periph_GPIOC,ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_USART3|RCC_APB1Periph_UART4,ENABLE);
    gpio_uart(GPIOB,GPIO_Pin_10|GPIO_Pin_11,10,11,GPIO_AF_USART3);
    gpio_uart(GPIOC,GPIO_Pin_10|GPIO_Pin_11,10,11,GPIO_AF_UART4);
#endif
    for(i=0;i<EMM_CHANNEL_COUNT;i++) uart_setup(ports[i],irqs[i]);
#if PHYSICAL_ESTOP_ENABLED
    {
        GPIO_InitTypeDef g;
        RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOD,ENABLE);
        GPIO_StructInit(&g); g.GPIO_Pin=GPIO_Pin_2;
        g.GPIO_Mode=GPIO_Mode_IN; g.GPIO_PuPd=GPIO_PuPd_UP;
        GPIO_Init(GPIOD,&g);
    }
#endif
    /* TIM2独立1ms时基；不占用delay.c所用SysTick，也不产生STEP脉冲。 */
    RCC_GetClocksFreq(&clocks); timer_clock=clocks.PCLK1_Frequency;
    if ((RCC->CFGR & RCC_CFGR_PPRE1) != 0U) timer_clock*=2U;
    TIM_TimeBaseStructInit(&t);
    t.TIM_Prescaler=(uint16_t)(timer_clock/1000000UL-1UL);
    t.TIM_Period=999; TIM_TimeBaseInit(TIM2,&t);
    TIM_ClearITPendingBit(TIM2,TIM_IT_Update);
    n.NVIC_IRQChannel=TIM2_IRQn; n.NVIC_IRQChannelPreemptionPriority=1;
    n.NVIC_IRQChannelSubPriority=0; n.NVIC_IRQChannelCmd=ENABLE;
    NVIC_Init(&n); TIM_ITConfig(TIM2,TIM_IT_Update,ENABLE);
    TIM_Cmd(TIM2,ENABLE);
}
uint32_t EmmPort_Millis(void) { return milliseconds; }
uint8_t App_StopRequested(void)
{
#if PHYSICAL_ESTOP_ENABLED
    if (!GPIO_ReadInputDataBit(GPIOD,GPIO_Pin_2)) stop_latched=1;
#endif
    return stop_latched;
}
void App_ClearStop(void) { stop_latched=0; }
void App_WaitMs(uint32_t ms)
{
    uint32_t begin=milliseconds;
    while ((uint32_t)(milliseconds-begin)<ms && !App_StopRequested()) {
    }
}
uint8_t EmmPort_Write(uint8_t c, const uint8_t *data, uint8_t length)
{
    uint8_t i;
    uint32_t begin;
    if (c>=EMM_CHANNEL_COUNT) return 0;
    for(i=0;i<length;i++) {
        begin=milliseconds;
        while(USART_GetFlagStatus(ports[c],USART_FLAG_TXE)==RESET)
            if((uint32_t)(milliseconds-begin)>10UL) return 0;
        USART_SendData(ports[c],data[i]);
    }
    begin=milliseconds;
    while(USART_GetFlagStatus(ports[c],USART_FLAG_TC)==RESET)
        if((uint32_t)(milliseconds-begin)>10UL) return 0;
    return 1;
}
uint8_t EmmPort_Read(uint8_t c,uint8_t *b)
{ return c<EMM_CHANNEL_COUNT ? pop(&motor_rx[c],b) : 0; }
void EmmPort_Flush(uint8_t c)
{
    uint32_t key;
    if(c>=EMM_CHANNEL_COUNT) return;
    key=__get_PRIMASK(); __disable_irq();
    motor_rx[c].tail=motor_rx[c].head; motor_rx[c].error=0;
    __set_PRIMASK(key);
}
uint8_t EmmPort_RxError(uint8_t c)
{ return c<EMM_CHANNEL_COUNT ? motor_rx[c].error : 1; }
uint8_t Console_GetCommand(uint8_t *c) { return pop(&console_rx,c); }
void Console_FlushRx(void)
{
 uint32_t mask=__get_PRIMASK();
 __disable_irq();
 console_rx.tail=console_rx.head;console_rx.error=0U;
 __set_PRIMASK(mask);
 /* 不清除stop_latched：软件停止不得被下一轮自动复位。 */
}
uint8_t Console_TakeRxError(void)
{
    uint8_t error;
    uint32_t mask=__get_PRIMASK();
    __disable_irq();
    error=console_rx.error;
    if(error){console_rx.tail=console_rx.head;console_rx.error=0U;}
    __set_PRIMASK(mask);
    return error;
}
uint8_t Console_Write(const uint8_t *data,uint8_t length)
{
    uint8_t i;
    uint32_t begin;
    for(i=0;i<length;i++){
        begin=milliseconds;
        while(USART_GetFlagStatus(USART1,USART_FLAG_TXE)==RESET)
            if((uint32_t)(milliseconds-begin)>10UL)return 0U;
        USART_SendData(USART1,data[i]);
    }
    return 1U;
}

static void receive(USART_TypeDef *port,RxRing *r,uint8_t debug)
{
    uint32_t sr=port->SR;
    uint8_t b;
    if(sr & (USART_SR_RXNE|USART_SR_ORE|USART_SR_FE|USART_SR_NE|USART_SR_PE)) {
        b=(uint8_t)port->DR; /* SR后DR清除ORE/FE/NE；错误字节不交给解析器 */
        if(sr & (USART_SR_ORE|USART_SR_FE|USART_SR_NE|USART_SR_PE)) {
            r->error=1; return;
        }
        if(debug && b=='!') { stop_latched=1; return; }
        push(r,b);
    }
}
void USART1_IRQHandler(void) { receive(USART1,&console_rx,1); }
void USART2_IRQHandler(void)
{
    receive(USART2,&motor_rx[0],0);
}
void USART3_IRQHandler(void) { receive(USART3,&motor_rx[1],0); }
void UART4_IRQHandler(void) { receive(UART4,&motor_rx[2],0); }
void TIM2_IRQHandler(void)
{
    if(TIM_GetITStatus(TIM2,TIM_IT_Update)!=RESET) {
        TIM_ClearITPendingBit(TIM2,TIM_IT_Update); milliseconds++;
    }
}

/* ARMCC标准输出只走USART1，不允许把printf文字送给电机串口。 */
#pragma import(__use_no_semihosting)
struct __FILE { int handle; };
FILE __stdout;
void _sys_exit(int code) { (void)code; for(;;) {} }
int fputc(int ch, FILE *stream)
{
    uint32_t begin=milliseconds;
    (void)stream;
    while(USART_GetFlagStatus(USART1,USART_FLAG_TXE)==RESET)
        if((uint32_t)(milliseconds-begin)>10UL) return ch;
    USART_SendData(USART1,(uint8_t)ch); return ch;
}
