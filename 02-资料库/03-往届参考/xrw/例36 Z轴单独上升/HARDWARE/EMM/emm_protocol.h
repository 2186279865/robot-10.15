#ifndef EMM_PROTOCOL_H
#define EMM_PROTOCOL_H

#include <stdint.h>

/* Emm_V5.0 自定义协议，不适用于 V4.2 或 X系列V2。
 * 固定校验 6B 不是有效载荷分隔符：位置数据本身可以包含 6B。
 * 原始文档：商家 Emm_V5.0 Rev1.3，CSDN article/132644047。
 */
#define EMM_FRAME_MAX 16U
typedef struct {
    uint8_t bytes[EMM_FRAME_MAX];
    uint8_t used;
} EmmParser;
typedef struct {
    uint8_t bytes[EMM_FRAME_MAX];
    uint8_t length;
} EmmFrame;

void EmmParser_Reset(EmmParser *parser);
uint8_t EmmParser_Feed(EmmParser *parser, uint8_t address,
                       uint8_t byte, EmmFrame *frame);
uint8_t Emm_BuildEnable(uint8_t *out, uint8_t address, uint8_t on);
uint8_t Emm_BuildPosition(uint8_t *out, uint8_t address, int32_t pulses,
                         uint16_t rpm, uint8_t accel, uint8_t absolute,
                         uint8_t sync);
uint8_t Emm_BuildSpeed(uint8_t *out, uint8_t address, uint8_t direction,
                      uint16_t rpm, uint8_t accel);
uint8_t Emm_BuildStop(uint8_t *out, uint8_t address);
uint8_t Emm_BuildHome(uint8_t *out, uint8_t address);
uint8_t Emm_BuildAbortHome(uint8_t *out, uint8_t address);
uint8_t Emm_BuildClear(uint8_t *out, uint8_t address);
uint8_t Emm_BuildRead(uint8_t *out, uint8_t address, uint8_t function);
uint8_t Emm_DecodePosition(const EmmFrame *frame, int32_t *counts);

#endif
