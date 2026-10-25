/* emm_reply_inline.h - EMM V5 驱动器回包解析 + "一次一个查询事务"
 *
 * 来源：xrw/例36 Z轴单独上升/HARDWARE/EMM/emm_protocol.c 的解析器（已在实际项目里跑过），
 *       搬过来只做两件事：加上转速/状态解码，外面套一层非阻塞查询事务。
 *
 * 回包格式（115200 8N1，多字节大端，帧尾固定 0x6B 不是 CRC）：
 *   [地址] [功能码] [数据…] [0x6B]
 * 已知长度：
 *   0x1F = 5   固件/硬件版本
 *   0x35 = 6   实时转速：符号 + 2 字节幅值
 *   0x36 = 8   编码器位置：符号 + 32 位计数（65536 计数 = 1 圈）
 *   0x3A/0x3B = 4   状态标志
 *   0xFD = 4   到位通知（驱动器自己发，不是查询应答）
 *   0xF3/0xF6/0xFE/0x0A/0x9A/0x9C = 4   各命令的确认
 *   其他功能码长度未知 —— 一个字节一个字节丢弃，不猜、不拼。
 *
 * "位置"和"脉冲"不是一个东西：0x36 回的是编码器计数（65536/圈），位置指令用的是
 * 驱动器自己的脉冲当量（本工程 3200 脉冲/圈）。两者只在"位置基准有效"时才有对应关系，
 * 所以界面显示实测位置时只显示角度与计数，不拿它当位置指令的输入。
 */

#include <stdint.h>

/* 本层只用到这两个函数：发询问帧、取接收字节。
 * 故意不 include "stepper.h"（那会拉进 stm32f4xx.h），这样 PC 上的桩件测试
 * 也能直接编译这份真实源码。原型与 stepper.h 里的一致。 */
uint8_t Stepper_SendFrame(const uint8_t *frame, uint8_t len);
uint8_t Stepper_Read(uint8_t *byte);

#ifndef __EMM_REPLY_INLINE_H
#define __EMM_REPLY_INLINE_H

#define EMM_FRAME_MAX      8U      /* 最长应答是 0x36 的 8 字节 */
#define EMM_COUNTS_PER_REV 65536L  /* 编码器计数/圈 */

/* 查询类型 */
#define EMM_QUERY_NONE 0U
#define EMM_QUERY_VEL  1U          /* 0x35 实时转速 */
#define EMM_QUERY_POS  2U          /* 0x36 编码器位置 */
#define EMM_QUERY_FLAG 3U          /* 0x3A 状态 */

typedef struct
{
    uint8_t bytes[EMM_FRAME_MAX];
    uint8_t length;
} EmmFrame;

typedef struct
{
    uint8_t used;
    uint8_t bytes[EMM_FRAME_MAX];
} EmmParser;

/* ---------------- 长度表与解析器 ---------------- */

static uint8_t emm_reply_length(uint8_t function)
{
    switch (function)
    {
    case 0x1F: return 5U;
    case 0x35: return 6U;
    case 0x36: return 8U;
    case 0x00: case 0x3A: case 0x3B:
    case 0xF3: case 0xF6: case 0xFD: case 0xFE:
    case 0x9A: case 0x9C: case 0x0A: case 0x93:
        return 4U;
    default: return 0U;        /* 长度未知：不解析，逐字节丢弃 */
    }
}

static void EmmParser_Reset(EmmParser *p)
{
    p->used = 0U;
}

/* 喂一个字节。返回 1 = 拼出一帧完整回包（放在 frame 里），0 = 还没凑齐 */
static uint8_t EmmParser_Feed(EmmParser *p, uint8_t address,
                              uint8_t byte, EmmFrame *frame)
{
    uint8_t size;
    uint8_t i;

    if (p->used >= EMM_FRAME_MAX)
    {
        p->used = 0U;          /* 防御：正常不会走到这里 */
    }
    p->bytes[p->used] = byte;
    p->used++;

    while (p->used != 0U)
    {
        if ((address != 0U) && (p->bytes[0] != address))
        {
            for (i = 1U; i < p->used; i++)
            {
                p->bytes[i - 1U] = p->bytes[i];
            }
            p->used--;
            continue;
        }
        if (p->used < 2U)
        {
            return 0U;
        }
        size = emm_reply_length(p->bytes[1]);
        if (size == 0U)
        {
            for (i = 1U; i < p->used; i++)
            {
                p->bytes[i - 1U] = p->bytes[i];
            }
            p->used--;
            continue;
        }
        if (p->used < size)
        {
            return 0U;
        }
        if (p->bytes[size - 1U] != 0x6BU)
        {
            for (i = 1U; i < p->used; i++)
            {
                p->bytes[i - 1U] = p->bytes[i];
            }
            p->used--;
            continue;
        }
        for (i = 0U; i < size; i++)
        {
            frame->bytes[i] = p->bytes[i];
        }
        frame->length = size;
        p->used = 0U;
        return 1U;
    }
    return 0U;
}

/* ---------------- 解码 ---------------- */

static uint8_t Emm_DecodePosition(const EmmFrame *f, int32_t *counts)
{
    uint32_t n;

    if ((f->length != 8U) || (f->bytes[1] != 0x36U) ||
        (f->bytes[2] > 1U) || (f->bytes[7] != 0x6BU))
    {
        return 0U;
    }
    n = ((uint32_t)f->bytes[3] << 24) | ((uint32_t)f->bytes[4] << 16) |
        ((uint32_t)f->bytes[5] << 8) | (uint32_t)f->bytes[6];
    if (n > 0x7FFFFFFFUL)
    {
        return 0U;             /* 超出有符号范围：当作无效，不猜符号 */
    }
    *counts = f->bytes[2] ? -(int32_t)n : (int32_t)n;
    return 1U;
}

static uint8_t Emm_DecodeSpeed(const EmmFrame *f, int16_t *rpm)
{
    uint16_t n;

    if ((f->length != 6U) || (f->bytes[1] != 0x35U) ||
        (f->bytes[2] > 1U) || (f->bytes[5] != 0x6BU))
    {
        return 0U;
    }
    n = (uint16_t)(((uint16_t)f->bytes[3] << 8) | (uint16_t)f->bytes[4]);
    if (n > 0x7FFFU)
    {
        return 0U;
    }
    *rpm = f->bytes[2] ? (int16_t)(-(int16_t)n) : (int16_t)n;
    return 1U;
}

static uint8_t Emm_DecodeFlag(const EmmFrame *f, uint8_t *flag)
{
    if ((f->length != 4U) || (f->bytes[1] != 0x3AU) || (f->bytes[3] != 0x6BU))
    {
        return 0U;
    }
    *flag = f->bytes[2];
    return 1U;
}

/* 是不是"到位通知"（驱动器主动发，不需要查询） */
static uint8_t Emm_IsReachedNotice(const EmmFrame *f)
{
    return (uint8_t)((f->length == 4U) && (f->bytes[1] == 0xFDU));
}

/* 编码器计数 -> 角度 ×10（保留一位小数，避免在固件里用浮点） */
static int32_t Emm_CountsToDegX10(int32_t counts)
{
    int64_t v = (int64_t)counts * 3600LL;

    return (int32_t)(v / (int64_t)EMM_COUNTS_PER_REV);
}

/* 脉冲 <-> 编码器计数换算（给闭环 goto 用）。
 * counts_per_rev 由运行参数给出，不写死：标定出来不是 65536 也能改。
 * 四舍五入到最近的计数，避免每次补差都系统性偏小。 */
static int32_t Emm_PulsesToCounts(int32_t pulses, uint32_t counts_per_rev,
                                  uint32_t pulses_per_rev)
{
    int64_t v;

    if ((counts_per_rev == 0U) || (pulses_per_rev == 0U))
    {
        return 0;
    }
    v = (int64_t)pulses * (int64_t)counts_per_rev;
    if (v >= 0)
    {
        v += (int64_t)(pulses_per_rev / 2U);
    }
    else
    {
        v -= (int64_t)(pulses_per_rev / 2U);
    }
    return (int32_t)(v / (int64_t)pulses_per_rev);
}

static int32_t Emm_CountsToPulses(int32_t counts, uint32_t counts_per_rev,
                                  uint32_t pulses_per_rev)
{
    int64_t v;

    if ((counts_per_rev == 0U) || (pulses_per_rev == 0U))
    {
        return 0;
    }
    v = (int64_t)counts * (int64_t)pulses_per_rev;
    if (v >= 0)
    {
        v += (int64_t)(counts_per_rev / 2U);
    }
    else
    {
        v -= (int64_t)(counts_per_rev / 2U);
    }
    return (int32_t)(v / (int64_t)counts_per_rev);
}

/* ---------------- 一次一个查询事务 ---------------- */

typedef struct
{
    uint8_t  active;           /* 1 = 有查询在等回包 */
    uint8_t  addr;             /* 等的是哪个地址 */
    uint8_t  what;             /* EMM_QUERY_* */
    uint32_t sent_ms;
    uint32_t timeout_ms;
    uint8_t  timeout_count;    /* 累计超时次数，界面/日志用 */

    uint8_t  have_data;        /* 收到过有效回包 */
    int32_t  counts;           /* 0x36 */
    int16_t  rpm;              /* 0x35 */
    uint8_t  flag;             /* 0x3A */
    uint32_t stamp_ms;         /* 数据时间戳（超时也不清，界面据此判断"过期"） */
    uint8_t  reached;          /* 收到过 0xFD 到位通知 */
    uint32_t reached_ms;
    uint8_t  bad_reply;        /* 回包是本地址发的，但不是查的那一项（E05） */
} EmmQuery;

static void EmmQuery_Reset(EmmQuery *q)
{
    q->active = 0U;
    q->addr = 0U;
    q->what = EMM_QUERY_NONE;
    q->sent_ms = 0U;
    q->timeout_ms = 100U;
    q->timeout_count = 0U;
    q->have_data = 0U;
    q->counts = 0;
    q->rpm = 0;
    q->flag = 0U;
    q->stamp_ms = 0U;
    q->reached = 0U;
    q->reached_ms = 0U;
    q->bad_reply = 0U;
}

static uint8_t emm_query_function(uint8_t what)
{
    if (what == EMM_QUERY_VEL)
    {
        return 0x35U;
    }
    if (what == EMM_QUERY_POS)
    {
        return 0x36U;
    }
    if (what == EMM_QUERY_FLAG)
    {
        return 0x3AU;
    }
    return 0U;
}

/* 发起一次查询：发 3 字节询问帧，然后等中断收字节（非阻塞） */
static uint8_t EmmQuery_Start(EmmQuery *q, uint8_t addr, uint8_t what,
                              uint32_t now_ms, uint32_t timeout_ms)
{
    uint8_t frame[3];
    uint8_t fn = emm_query_function(what);

    if ((fn == 0U) || (q->active != 0U))
    {
        return 0U;             /* 不认识的查询类型，或上一次还没结束 */
    }
    frame[0] = addr;
    frame[1] = fn;
    frame[2] = 0x6BU;
    if (!Stepper_SendFrame(frame, 3U))
    {
        return 0U;
    }
    q->active = 1U;
    q->addr = addr;
    q->what = what;
    q->sent_ms = now_ms;
    q->timeout_ms = (timeout_ms == 0U) ? 100U : timeout_ms;
    q->bad_reply = 0U;
    return 1U;
}

/* 收字节 -> 解析 -> 落到查询事务上。返回 1 = 这次查询已有结果（成功或不是本事务的帧） */
static uint8_t EmmQuery_Poll(EmmQuery *q, EmmParser *p, uint32_t now_ms)
{
    uint8_t b;
    uint8_t got = 0U;
    EmmFrame f;
    int32_t counts;
    int16_t rpm;
    uint8_t flag;

    while (Stepper_Read(&b))
    {
        if (!EmmParser_Feed(p, (q->active != 0U) ? q->addr : 0U, b, &f))
        {
            continue;
        }
        if (Emm_IsReachedNotice(&f))
        {
            q->reached = 1U;
            q->reached_ms = now_ms;
            got = 1U;
            continue;
        }
        if (q->active == 0U)
        {
            continue;
        }
        if ((q->what == EMM_QUERY_POS) && Emm_DecodePosition(&f, &counts))
        {
            q->counts = counts;
            q->have_data = 1U;
            q->stamp_ms = now_ms;
            q->active = 0U;
            got = 1U;
        }
        else if ((q->what == EMM_QUERY_VEL) && Emm_DecodeSpeed(&f, &rpm))
        {
            q->rpm = rpm;
            q->have_data = 1U;
            q->stamp_ms = now_ms;
            q->active = 0U;
            got = 1U;
        }
        else if ((q->what == EMM_QUERY_FLAG) && Emm_DecodeFlag(&f, &flag))
        {
            q->flag = flag;
            q->have_data = 1U;
            q->stamp_ms = now_ms;
            q->active = 0U;
            got = 1U;
        }
        else if ((f.bytes[1] == 0x35U) || (f.bytes[1] == 0x36U) ||
                 (f.bytes[1] == 0x3AU))
        {
            /* 本地址确实回了查询应答，却不是这次要的那一项：算回包错误，
             * 不能拿它当答案，也不该继续等到超时（E05） */
            q->bad_reply = 1U;
            q->active = 0U;
            got = 1U;
        }
        else
        {
            /* 不是这次要的帧（例如别条命令的确认帧）：丢掉，继续等 */
        }
    }
    return got;
}

/* 查询超时（回包没来）。超时只记账，不清掉上一次的读数。 */
static uint8_t EmmQuery_Expired(EmmQuery *q, uint32_t now_ms)
{
    if (q->active == 0U)
    {
        return 0U;
    }
    if ((uint32_t)(now_ms - q->sent_ms) < q->timeout_ms)
    {
        return 0U;
    }
    q->active = 0U;
    q->timeout_count++;
    return 1U;
}

#endif /* __EMM_REPLY_INLINE_H */
