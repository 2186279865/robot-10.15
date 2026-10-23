#ifndef EMM_PORT_H
#define EMM_PORT_H
#ifdef EMM_HOST_TEST
#include <stdint.h>
#else
#include "sys.h"
#endif
#include "app_config.h"

void EmmPort_Init(void);
uint32_t EmmPort_Millis(void);
uint8_t EmmPort_Write(uint8_t channel, const uint8_t *data, uint8_t length);
uint8_t EmmPort_Read(uint8_t channel, uint8_t *byte);
void EmmPort_Flush(uint8_t channel);
uint8_t EmmPort_RxError(uint8_t channel);
uint8_t Console_GetCommand(uint8_t *command);
uint8_t Console_Write(const uint8_t *data,uint8_t length);
uint8_t Console_TakeRxError(void);
uint8_t App_StopRequested(void);
void App_ClearStop(void);
void App_WaitMs(uint32_t ms);
#endif
