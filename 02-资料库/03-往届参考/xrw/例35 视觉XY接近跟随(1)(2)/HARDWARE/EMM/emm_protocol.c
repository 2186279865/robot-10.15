#include "emm_protocol.h"
#include <string.h>
#include <limits.h>

static uint8_t reply_length(uint8_t function)
{
    switch (function) {
    case 0x1F: return 5U;  /* 固件、硬件版本 */
    case 0x35: return 6U;  /* TI示例的实时转速：符号+2字节 */
    case 0x36: return 8U;  /* 符号 + 32位编码器计数；65536计数/圈 */
    case 0x00: case 0x3A: case 0x3B:
    case 0xF3: case 0xF6: case 0xFD: case 0xFE:
    case 0x9A: case 0x9C: case 0x0A: case 0x93:
        return 4U;
    default: return 0U;   /* 不猜测未知/可变长度回复 */
    }
}

void EmmParser_Reset(EmmParser *parser) { parser->used = 0U; }

uint8_t EmmParser_Feed(EmmParser *p, uint8_t address,
                       uint8_t byte, EmmFrame *frame)
{
    uint8_t size;
    if (p->used >= EMM_FRAME_MAX) p->used = 0U;
    p->bytes[p->used++] = byte;
    while (p->used) {
        if (p->bytes[0] != address) {
            memmove(p->bytes, p->bytes + 1, --p->used);
            continue;
        }
        if (p->used < 2U) return 0U;
        size = reply_length(p->bytes[1]);
        if (!size) {
            memmove(p->bytes, p->bytes + 1, --p->used);
            continue;
        }
        if (p->used < size) return 0U;
        if (p->bytes[size - 1U] != 0x6BU) {
            memmove(p->bytes, p->bytes + 1, --p->used);
            continue;
        }
        memcpy(frame->bytes, p->bytes, size);
        frame->length = size;
        p->used = 0U;
        return 1U;
    }
    return 0U;
}

uint8_t Emm_BuildEnable(uint8_t *o, uint8_t a, uint8_t on)
{
    o[0]=a; o[1]=0xF3; o[2]=0xAB; o[3]=on ? 1U : 0U;
    o[4]=0; o[5]=0x6B; return 6;
}

uint8_t Emm_BuildPosition(uint8_t *o, uint8_t a, int32_t pulses,
                         uint16_t rpm, uint8_t accel, uint8_t absolute,
                         uint8_t sync)
{
    uint32_t n = pulses < 0 ? 0U - (uint32_t)pulses : (uint32_t)pulses;
    o[0]=a; o[1]=0xFD; o[2]=pulses < 0 ? 1U : 0U;
    o[3]=(uint8_t)(rpm >> 8); o[4]=(uint8_t)rpm; o[5]=accel;
    o[6]=(uint8_t)(n >> 24); o[7]=(uint8_t)(n >> 16);
    o[8]=(uint8_t)(n >> 8); o[9]=(uint8_t)n;
    o[10]=absolute ? 1U : 0U; o[11]=sync ? 1U : 0U; o[12]=0x6B;
    return 13;
}

uint8_t Emm_BuildSpeed(uint8_t *o, uint8_t a, uint8_t dir,
                      uint16_t rpm, uint8_t accel)
{
    o[0]=a; o[1]=0xF6; o[2]=dir ? 1U : 0U;
    o[3]=(uint8_t)(rpm >> 8); o[4]=(uint8_t)rpm;
    o[5]=accel; o[6]=0; o[7]=0x6B; return 8;
}
uint8_t Emm_BuildStop(uint8_t *o, uint8_t a)
{ o[0]=a; o[1]=0xFE; o[2]=0x98; o[3]=0; o[4]=0x6B; return 5; }
uint8_t Emm_BuildHome(uint8_t *o, uint8_t a)
{ o[0]=a; o[1]=0x9A; o[2]=3; o[3]=0; o[4]=0x6B; return 5; }
uint8_t Emm_BuildAbortHome(uint8_t *o, uint8_t a)
{ o[0]=a; o[1]=0x9C; o[2]=0x48; o[3]=0x6B; return 4; }
uint8_t Emm_BuildClear(uint8_t *o, uint8_t a)
{ o[0]=a; o[1]=0x0A; o[2]=0x6D; o[3]=0x6B; return 4; }
uint8_t Emm_BuildRead(uint8_t *o, uint8_t a, uint8_t fn)
{ o[0]=a; o[1]=fn; o[2]=0x6B; return 3; }

uint8_t Emm_DecodePosition(const EmmFrame *f, int32_t *counts)
{
    uint32_t n;
    if (f->length != 8U || f->bytes[1] != 0x36 ||
        f->bytes[2] > 1U || f->bytes[7] != 0x6B) return 0;
    n = ((uint32_t)f->bytes[3] << 24) | ((uint32_t)f->bytes[4] << 16) |
        ((uint32_t)f->bytes[5] << 8) | f->bytes[6];
    if (n > (uint32_t)INT32_MAX) return 0; /* 框架不允许超过有符号范围 */
    *counts = f->bytes[2] ? -(int32_t)n : (int32_t)n;
    return 1;
}
