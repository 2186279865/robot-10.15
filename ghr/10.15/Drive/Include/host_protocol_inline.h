/* host_protocol_inline.h - 上位机行协议：分帧、校验、解析、分发、应答
 *
 * 由 main.c 在 host_transport.h / control_inline.h 之后包含。
 * 传输层接口见 host_transport.h；轴状态与非阻塞下发见 control_inline.h。
 *
 * 帧格式（方案 v3 第 7 节）：
 *   请求  $<id>,<cmd>[,<arg>...]*<CS>\n
 *   应答  #<id>,ok[,<key=value>...]*<CS>\n
 *   错误  #<id>,err,<code>[,<key=value>...]*<CS>\n
 *   事件  #0,event,<type>[,<key=value>...]*<CS>\n
 *   遥测  #0,tele,<key=value>...*<CS>\n
 *
 * <CS> = 起始符之后、星号之前所有 ASCII 字节的异或，两位大写十六进制。
 * 整帧最多 96 字节（含起始符与行尾 LF），即 '#'/'$' 之后最多 91 字节 payload。
 * 超长帧丢弃到 LF 并回 E06；校验错回 E07。
 *
 * 会话去重：同一条连接里缓存最近若干条已完成请求（id + payload + 应答）。
 * 同 id 同内容只重发原应答，不再执行；同 id 不同内容回 E12。
 * 这样 PC 在等应答超时后重发查询（ping/status/motor_query/param_get）不会出问题，
 * 也不会把相对位移命令重复执行一遍。
 *
 * `ok` 只表示命令被接纳，不表示动作完成。任务完成用 #0,event,task,... 报告，
 * 并用 state=sent/driver_ack/reached/settle_elapsed 标注证据等级。
 * 没有接回读的轴永远不发 reached——本文件在步骤 1 里所有轴都没有回读能力。
 */

#ifndef HOST_PROTOCOL_INLINE_H
#define HOST_PROTOCOL_INLINE_H

#include "host_transport.h"
#include "control_inline.h"
#include "emm_reply_inline.h"

/* ---------------- 常量 ---------------- */

#define HOST_PROTO_VERSION        1U
#define HOST_MAX_FRAME_BYTES      96U
#define HOST_MAX_PAYLOAD_CHARS    91U   /* '#'/'$' 之后、'*' 之前 */
#define HOST_MAX_LINE_CHARS       (HOST_MAX_PAYLOAD_CHARS + 3U)  /* + '*' + 2 位校验和 */
#define HOST_LINE_CAPACITY        (HOST_MAX_LINE_CHARS + 2U)
#define HOST_TX_CAPACITY          HOST_MAX_FRAME_BYTES
#define HOST_TX_BODY_MAX          HOST_MAX_PAYLOAD_CHARS
#define HOST_MAX_TOKENS           10U
#define HOST_SESSION_SLOTS        4U
#define HOST_READ_BYTES_PER_TASK  64U   /* 每次任务最多处理的接收字节数，避免独占主循环 */

#define HOST_FIRMWARE_VERSION     "0.1.0"

/* 回读能力：目前只有 Z 轴（地址 1）的驱动器 TXD 接到了 PA3。
 * hello 必须如实报出这张表：上位机据此决定哪些轴显示实测值。
 * 没接线时不会变成"有回读但读不到"——查询会在 query_timeout_ms 后回 E04。 */
#define HOST_READBACK_AXES        "1"

/* 错误码（与上位机 protocol.ERROR_CODES 一致） */
#define HOST_E_UNKNOWN_CMD        "E01"
#define HOST_E_ARG                "E02"
#define HOST_E_AXIS               "E03"
#define HOST_E_QUERY_TIMEOUT      "E04"
#define HOST_E_REPLY              "E05"
#define HOST_E_OVERFLOW           "E06"
#define HOST_E_CHECKSUM           "E07"
#define HOST_E_ENABLE             "E08"
#define HOST_E_BUSY               "E09"
#define HOST_E_LATCHED            "E10"
#define HOST_E_NO_READBACK        "E11"
#define HOST_E_ID_CONFLICT        "E12"
#define HOST_E_NO_POSITION_REF    "E13"

/* ---------------- 运行参数 ---------------- */
/* 名字必须与上位机 protocol.PARAM_DEFAULTS 完全一致（那边有测试守着这张表）。 */

typedef struct
{
    const char *name;
    uint16_t    value;
    uint16_t    min;
    uint16_t    max;
} HostParam;

#define HOST_PARAM_COUNT 12U

static HostParam host_params[HOST_PARAM_COUNT] =
{
    { "speed_rpm",         60U,   1U,  6000U  },
    { "acc",               1U,    0U,  255U   },
    { "speed_rpm_max",     300U,  1U,  6000U  },
    { "gripper_open_deg",  180U,  0U,  270U   },
    { "gripper_grab_deg",  0U,    0U,  270U   },
    { "gripper_step_deg",  45U,   1U,  270U   },
    { "gripper_settle_ms", 200U,  0U,  60000U },
    { "heartbeat_ms",      500U,  20U, 60000U },
    { "link_timeout_ms",   3000U, 100U, 60000U },
    { "query_timeout_ms",  100U,  10U, 60000U },
    { "telemetry_ms",      200U,  20U, 60000U },
    { "frame_gap_ms",      10U,   1U,  1000U  }
};

/* ---------------- 会话缓存 ---------------- */

typedef struct
{
    uint8_t  used;
    uint16_t req_id;
    uint8_t  req_len;
    uint8_t  rsp_len;
    char     req[HOST_MAX_LINE_CHARS + 1U];
    char     rsp[HOST_TX_CAPACITY];
} HostSession;

static HostSession host_sessions[HOST_SESSION_SLOTS];
static uint8_t     host_session_next = 0U;

/* ---------------- 接收分帧状态 ---------------- */

static char     host_line[HOST_LINE_CAPACITY];
static uint16_t host_line_len = 0U;
static uint8_t  host_line_active = 0U;
static uint8_t  host_line_oversize = 0U;
static uint16_t host_overflow_count = 0U;
static uint8_t  host_stop_pending = 0U;

/* ---------------- 输出组帧 ---------------- */

typedef struct
{
    char     buf[HOST_TX_CAPACITY];
    uint16_t len;
    uint8_t  overflow;
} HostOut;

static HostOut host_out;    /* 处理请求时用的应答缓冲 */
static HostOut host_evt;    /* 主动上报（事件/遥测）用的缓冲 */

static char *host_tokens[HOST_MAX_TOKENS];

/* ---------------- 运行状态 ---------------- */

static uint8_t       host_ready = 0U;          /* hello 握手完成 */
static uint32_t      host_boot_id = 0U;
static uint32_t      host_last_rx_ms = 0U;
static uint8_t       host_link_lost = 0U;
static const char   *host_owner = "none";
static uint8_t       host_tele_axis = 0U;      /* 0 = 未订阅 */
static uint16_t      host_tele_interval_ms = 0U;
static uint32_t      host_tele_next_ms = 0U;

/* ==========================================================================
 * 小工具
 * ========================================================================== */

static uint8_t host_streq(const char *a, const char *b)
{
    while ((*a != '\0') && (*b != '\0'))
    {
        if (*a != *b)
        {
            return 0U;
        }
        a++;
        b++;
    }
    return ((*a == '\0') && (*b == '\0')) ? 1U : 0U;
}

static char *host_strchr(const char *s, char c)
{
    while (*s != '\0')
    {
        if (*s == c)
        {
            return (char *)s;
        }
        s++;
    }
    return 0;
}

static char host_hex_digit(uint8_t v)
{
    return (v < 10U) ? (char)('0' + v) : (char)('A' + (v - 10U));
}

static uint8_t host_hex_value(char c)
{
    if ((c >= '0') && (c <= '9'))
    {
        return (uint8_t)(c - '0');
    }
    if ((c >= 'A') && (c <= 'F'))
    {
        return (uint8_t)(c - 'A' + 10);
    }
    if ((c >= 'a') && (c <= 'f'))
    {
        return (uint8_t)(c - 'a' + 10);
    }
    return 0xFFU;
}

/* 十进制解析：只接受纯数字，溢出返回 0 */
static uint8_t host_parse_u32(const char *s, uint32_t *out)
{
    uint32_t v = 0U;
    uint32_t d;

    if ((s == 0) || (*s == '\0'))
    {
        return 0U;
    }
    while (*s != '\0')
    {
        if ((*s < '0') || (*s > '9'))
        {
            return 0U;
        }
        d = (uint32_t)(*s - '0');
        if (v > ((0xFFFFFFFFUL - d) / 10UL))
        {
            return 0U;
        }
        v = (v * 10UL) + d;
        s++;
    }
    *out = v;
    return 1U;
}

/* 带符号解析，只用于 param_set 的 value */
static uint8_t host_parse_i32(const char *s, int32_t *out)
{
    uint32_t v;
    uint8_t  neg = 0U;

    if ((s == 0) || (*s == '\0'))
    {
        return 0U;
    }
    if (*s == '-')
    {
        neg = 1U;
        s++;
    }
    if (!host_parse_u32(s, &v))
    {
        return 0U;
    }
    if (v > 2147483647UL)
    {
        return 0U;
    }
    *out = neg ? -(int32_t)v : (int32_t)v;
    return 1U;
}

/* 取行首的 id：只接受纯数字，且后面必须紧跟 ',' 或结束 */
static uint8_t host_parse_lead_id(const char *s, uint32_t *out)
{
    uint32_t v = 0U;
    uint32_t d;
    uint8_t  digits = 0U;

    while ((*s >= '0') && (*s <= '9'))
    {
        d = (uint32_t)(*s - '0');
        if (v > ((0xFFFFFFFFUL - d) / 10UL))
        {
            return 0U;
        }
        v = (v * 10UL) + d;
        digits++;
        s++;
    }
    if ((digits == 0U) || ((*s != ',') && (*s != '\0')))
    {
        return 0U;
    }
    *out = v;
    return 1U;
}

static uint8_t host_mem_eq(const char *a, const char *b, uint8_t n)
{
    uint8_t i;

    for (i = 0U; i < n; i++)
    {
        if (a[i] != b[i])
        {
            return 0U;
        }
    }
    return 1U;
}

static HostParam *host_param_find(const char *name)
{
    uint8_t i;

    for (i = 0U; i < HOST_PARAM_COUNT; i++)
    {
        if (host_streq(host_params[i].name, name))
        {
            return &host_params[i];
        }
    }
    return 0;
}

static uint16_t host_param_value(const char *name)
{
    HostParam *p = host_param_find(name);

    return (p == 0) ? 0U : p->value;
}

/* ==========================================================================
 * 输出组帧
 * ========================================================================== */

static void host_out_reset(HostOut *o)
{
    o->buf[0]   = '#';
    o->len      = 1U;
    o->overflow = 0U;
}

static void host_out_char(HostOut *o, char c)
{
    if (o->len < (uint16_t)(1U + HOST_TX_BODY_MAX))
    {
        o->buf[o->len] = c;
        o->len++;
    }
    else
    {
        o->overflow = 1U;      /* 装不下就丢这一条，绝不发半帧 */
    }
}

static void host_out_str(HostOut *o, const char *s)
{
    while (*s != '\0')
    {
        host_out_char(o, *s);
        s++;
    }
}

static void host_out_u32(HostOut *o, uint32_t v)
{
    char    tmp[10];
    uint8_t n = 0U;

    if (v == 0U)
    {
        host_out_char(o, '0');
        return;
    }
    while ((v > 0U) && (n < 10U))
    {
        tmp[n] = (char)('0' + (uint8_t)(v % 10UL));
        n++;
        v /= 10UL;
    }
    while (n > 0U)
    {
        n--;
        host_out_char(o, tmp[n]);
    }
}

static void host_out_hex32(HostOut *o, uint32_t v)
{
    int8_t i;

    for (i = 7; i >= 0; i--)
    {
        host_out_char(o, host_hex_digit((uint8_t)((v >> (uint8_t)(i * 4)) & 0x0FU)));
    }
}

static void host_kv_str(HostOut *o, const char *key, const char *value)
{
    host_out_char(o, ',');
    host_out_str(o, key);
    host_out_char(o, '=');
    host_out_str(o, value);
}

static void host_kv_u32(HostOut *o, const char *key, uint32_t value)
{
    host_out_char(o, ',');
    host_out_str(o, key);
    host_out_char(o, '=');
    host_out_u32(o, value);
}

/* 有符号值（编码器计数、转速可以是负的） */
static void host_kv_i32(HostOut *o, const char *key, int32_t value)
{
    host_out_char(o, ',');
    host_out_str(o, key);
    host_out_char(o, '=');
    if (value < 0)
    {
        host_out_char(o, '-');
        host_out_u32(o, (uint32_t)(-(value + 1)) + 1U);   /* 避开 -2^31 溢出 */
    }
    else
    {
        host_out_u32(o, (uint32_t)value);
    }
}

/* 角度 ×10 打印成 12.3，固件里不用浮点 */
static void host_kv_deg_x10(HostOut *o, const char *key, int32_t deg_x10)
{
    int32_t whole;
    int32_t frac;

    host_out_char(o, ',');
    host_out_str(o, key);
    host_out_char(o, '=');
    if (deg_x10 < 0)
    {
        host_out_char(o, '-');
        deg_x10 = -deg_x10;
    }
    whole = deg_x10 / 10;
    frac  = deg_x10 % 10;
    host_out_u32(o, (uint32_t)whole);
    host_out_char(o, '.');
    host_out_char(o, (char)('0' + (char)frac));
}

static void host_body_head(HostOut *o, uint16_t id, const char *kind)
{
    host_out_reset(o);
    host_out_u32(o, (uint32_t)id);
    host_out_char(o, ',');
    host_out_str(o, kind);
}

static void host_body_ok(HostOut *o, uint16_t id)
{
    host_body_head(o, id, "ok");
}

static void host_body_err(HostOut *o, uint16_t id, const char *code)
{
    host_body_head(o, id, "err");
    host_out_char(o, ',');
    host_out_str(o, code);
}

/* 补 '*' + 两位大写校验和 + LF，并整帧交给传输层 */
static uint8_t host_out_finish(HostOut *o)
{
    uint8_t  cs = 0U;
    uint16_t i;

    if (o->overflow)
    {
        return 0U;
    }
    for (i = 1U; i < o->len; i++)
    {
        cs = (uint8_t)(cs ^ (uint8_t)o->buf[i]);
    }
    o->buf[o->len] = '*'; o->len++;
    o->buf[o->len] = host_hex_digit((uint8_t)(cs >> 4)); o->len++;
    o->buf[o->len] = host_hex_digit((uint8_t)(cs & 0x0FU)); o->len++;
    o->buf[o->len] = '\n'; o->len++;

    return HostLink_QueueTx((const uint8_t *)o->buf, o->len);
}

/* ==========================================================================
 * 会话缓存
 * ========================================================================== */

static void host_session_clear(void)
{
    uint8_t i;

    for (i = 0U; i < HOST_SESSION_SLOTS; i++)
    {
        host_sessions[i].used = 0U;
    }
    host_session_next = 0U;
}

static HostSession *host_session_find(uint16_t id)
{
    uint8_t i;

    for (i = 0U; i < HOST_SESSION_SLOTS; i++)
    {
        if ((host_sessions[i].used) && (host_sessions[i].req_id == id))
        {
            return &host_sessions[i];
        }
    }
    return 0;
}

static void host_session_store(uint16_t id, const char *req, uint8_t req_len,
                               const char *rsp, uint8_t rsp_len)
{
    HostSession *s = &host_sessions[host_session_next];
    uint8_t i;

    if (req_len > HOST_MAX_LINE_CHARS)
    {
        req_len = HOST_MAX_LINE_CHARS;
    }
    if (rsp_len > HOST_TX_CAPACITY)
    {
        rsp_len = HOST_TX_CAPACITY;
    }

    s->used    = 1U;
    s->req_id  = id;
    s->req_len = req_len;
    s->rsp_len = rsp_len;
    for (i = 0U; i < req_len; i++)
    {
        s->req[i] = req[i];
    }
    s->req[req_len] = '\0';
    for (i = 0U; i < rsp_len; i++)
    {
        s->rsp[i] = rsp[i];
    }

    host_session_next++;
    if (host_session_next >= HOST_SESSION_SLOTS)
    {
        host_session_next = 0U;
    }
}

/* 组完帧 → 发给链路 → 记进会话缓存 */
static uint8_t host_emit(uint16_t id, const char *req_text, uint16_t req_len)
{
    uint8_t r = host_out_finish(&host_out);

    if (id != 0U)
    {
        host_session_store(id, req_text, (uint8_t)req_len,
                           host_out.buf, (uint8_t)host_out.len);
    }
    return r;
}

/* ==========================================================================
 * 驱动器回读（PA3）：一次只留一个查询事务
 * ========================================================================== */

static EmmParser host_emm_parser;
static EmmQuery  host_emm_query;

static void host_flush_pending_event(void);   /* 定义在下面的"主动上报"一节 */

/* 查询要等驱动器回包（回包由 USART2 中断送进环形缓冲）。
 * 这期间不能先把 ok 发出去——否则上位机会先看到"ok 但没有数据"。
 * 所以把请求 id 与切分后的原文存下来，等回包或超时后补发（补发走 host_emit，
 * 一样进会话缓存，这样上位机重发同一个 id 时不会被重复执行）。 */
static uint8_t  host_defer_line    = 0U;   /* 本行的应答要等回包（每行开头清零） */
static uint8_t  host_query_pending = 0U;   /* 有一次查询正等回包（补发后清零） */
static uint16_t host_defer_id      = 0U;
static uint8_t  host_defer_req_len = 0U;
static char     host_defer_req[HOST_MAX_LINE_CHARS + 1U];
static uint8_t  host_defer_addr    = 0U;
static uint8_t  host_defer_what    = EMM_QUERY_NONE;

/* 目前只有 Z 轴驱动器 TXD 接到 PA3；其余三路没接，必须如实回 E11 */
static uint8_t host_axis_has_readback(uint8_t addr)
{
    return (uint8_t)((addr == 1U) ? 1U : 0U);
}

static void host_save_defer_req(const char *text, uint16_t len)
{
    uint16_t i;

    if (len > HOST_MAX_LINE_CHARS)
    {
        len = HOST_MAX_LINE_CHARS;
    }
    for (i = 0U; i < len; i++)
    {
        host_defer_req[i] = text[i];
    }
    host_defer_req[len]   = '\0';
    host_defer_req_len    = (uint8_t)len;
}

static void host_query_reset_all(void)
{
    EmmParser_Reset(&host_emm_parser);
    EmmQuery_Reset(&host_emm_query);
    host_defer_line    = 0U;
    host_query_pending = 0U;
    host_defer_id      = 0U;
    host_defer_req_len = 0U;
    host_defer_addr    = 0U;
    host_defer_what    = EMM_QUERY_NONE;
}

/* 回包到了：把数据补进应答，旧数据不装新（stamp 早于本次发出的不认） */
static uint8_t host_query_has_fresh_data(void)
{
    if (host_emm_query.have_data == 0U)
    {
        return 0U;
    }
    return (uint8_t)((host_emm_query.stamp_ms >= host_emm_query.sent_ms) ? 1U : 0U);
}

static void host_query_answer(uint32_t now)
{
    host_body_ok(&host_out, host_defer_id);
    host_kv_u32(&host_out, "addr", (uint32_t)host_defer_addr);

    if (host_defer_what == EMM_QUERY_POS)
    {
        host_kv_i32(&host_out, "counts", host_emm_query.counts);
        host_kv_deg_x10(&host_out, "deg", Emm_CountsToDegX10(host_emm_query.counts));
    }
    else if (host_defer_what == EMM_QUERY_VEL)
    {
        host_kv_i32(&host_out, "rpm", (int32_t)host_emm_query.rpm);
    }
    else
    {
        host_kv_u32(&host_out, "flag", (uint32_t)host_emm_query.flag);
    }
    host_kv_u32(&host_out, "age_ms", EmmQuery_AgeMs(&host_emm_query, now));

    (void)host_emit(host_defer_id, host_defer_req, host_defer_req_len);
    host_query_pending = 0U;
    host_flush_pending_event();
}

static void host_query_timeout_answer(void)
{
    host_body_err(&host_out, host_defer_id, HOST_E_QUERY_TIMEOUT);
    host_kv_u32(&host_out, "addr", (uint32_t)host_defer_addr);
    (void)host_emit(host_defer_id, host_defer_req, host_defer_req_len);
    host_query_pending = 0U;
    host_flush_pending_event();
}

/* 驱动器回了应答，但不是查的那一项（例如要位置却回了状态） */
static void host_query_bad_reply_answer(void)
{
    host_body_err(&host_out, host_defer_id, HOST_E_REPLY);
    host_kv_u32(&host_out, "addr", (uint32_t)host_defer_addr);
    (void)host_emit(host_defer_id, host_defer_req, host_defer_req_len);
    host_query_pending = 0U;
    host_flush_pending_event();
}

/* 每轮都要调：收字节、推进事务、按需补发应答 */
static void host_query_service(uint32_t now)
{
    (void)EmmQuery_Poll(&host_emm_query, &host_emm_parser, now);

    if (host_emm_query.bad_reply != 0U)
    {
        host_emm_query.bad_reply = 0U;
        if (host_query_pending != 0U)
        {
            host_query_bad_reply_answer();
        }
        return;
    }
    if (host_query_pending == 0U)
    {
        return;                 /* 没有在等回包的查询 */
    }
    if ((host_emm_query.active == 0U) && host_query_has_fresh_data())
    {
        host_query_answer(now);
        return;
    }
    if (EmmQuery_Expired(&host_emm_query, now))
    {
        host_query_timeout_answer();
    }
}

/* 遥测订阅了有回读的轴时，顺便起一次位置查询（不占 PC 的请求 id） */
static void host_query_background(uint32_t now)
{
    if (host_query_pending != 0U)
    {
        return;
    }
    if (host_emm_query.active != 0U)
    {
        return;
    }
    (void)EmmQuery_Start(&host_emm_query, 1U, EMM_QUERY_POS, now,
                         (uint32_t)host_param_value("query_timeout_ms"));
}

/* ==========================================================================
 * 主动上报
 * ========================================================================== */

static void host_emit_task_event(uint32_t task_id, const char *state)
{
    host_body_head(&host_evt, 0U, "event");
    host_out_str(&host_evt, ",task");
    host_kv_u32(&host_evt, "id", task_id);
    host_kv_str(&host_evt, "state", state);
    (void)host_out_finish(&host_evt);
}

static void host_emit_latch_event(const char *state, const char *reason)
{
    host_body_head(&host_evt, 0U, "event");
    host_out_str(&host_evt, ",latch");
    host_kv_str(&host_evt, "state", state);
    if (reason != 0)
    {
        host_kv_str(&host_evt, "reason", reason);
    }
    (void)host_out_finish(&host_evt);
}

static void host_emit_link_event(const char *state)
{
    host_body_head(&host_evt, 0U, "event");
    host_out_str(&host_evt, ",link");
    host_kv_str(&host_evt, "state", state);
    (void)host_out_finish(&host_evt);
}

/* 遥测：有回读的轴报实测计数/角度，没接回读的轴显式写 readback=0 */
static void host_emit_telemetry(uint8_t addr)
{
    ControlAxis *ax = control_get(addr);
    uint32_t     now;

    if (ax == 0)
    {
        return;
    }
    now = Stepper_Millis();

    host_body_head(&host_evt, 0U, "tele");
    host_kv_u32(&host_evt, "addr", ax->addr);
    host_kv_u32(&host_evt, "rpm", (uint32_t)((ax->mode == CONTROL_MODE_SPEED) ? ax->rpm : 0U));

    if (host_axis_has_readback(addr) && (host_emm_query.have_data != 0U))
    {
        host_kv_u32(&host_evt, "readback", 1U);
        host_kv_i32(&host_evt, "counts", host_emm_query.counts);
        host_kv_deg_x10(&host_evt, "deg", Emm_CountsToDegX10(host_emm_query.counts));
        host_kv_u32(&host_evt, "age_ms", EmmQuery_AgeMs(&host_emm_query, now));
    }
    else
    {
        host_kv_u32(&host_evt, "readback", 0U);
    }
    (void)host_out_finish(&host_evt);
}

/* ---- 待发事件 ----
 * 命令处理器只能排队，不能自己往外发：否则事件会抢在 #<id>,ok 之前出去，
 * 上位机就会看到"任务事件比应答先到"。统一由 host_handle_line 在应答之后发。 */

#define HOST_PENDING_NONE   0U
#define HOST_PENDING_TASK   1U
#define HOST_PENDING_LATCH  2U

static uint8_t     host_pending_kind = HOST_PENDING_NONE;
static uint32_t    host_pending_task = 0U;
static const char *host_pending_state = 0;
static const char *host_pending_reason = 0;

static void host_queue_task_event(uint32_t task_id, const char *state)
{
    host_pending_kind  = HOST_PENDING_TASK;
    host_pending_task  = task_id;
    host_pending_state = state;
}

static void host_queue_latch_event(const char *state, const char *reason)
{
    host_pending_kind   = HOST_PENDING_LATCH;
    host_pending_state  = state;
    host_pending_reason = reason;
}

static void host_flush_pending_event(void)
{
    switch (host_pending_kind)
    {
    case HOST_PENDING_TASK:
        host_emit_task_event(host_pending_task, host_pending_state);
        break;
    case HOST_PENDING_LATCH:
        host_emit_latch_event(host_pending_state, host_pending_reason);
        break;
    default:
        break;
    }
    host_pending_kind = HOST_PENDING_NONE;
}

/* ==========================================================================
 * 请求解析
 * ========================================================================== */

static void host_line_reset(void)
{
    host_line_len      = 0U;
    host_line_active   = 0U;
    host_line_oversize = 0U;
}

static uint8_t host_checksum_ok(const char *payload, uint16_t len,
                                char cs_hi, char cs_lo)
{
    uint8_t  cs = 0U;
    uint16_t i;

    for (i = 0U; i < len; i++)
    {
        cs = (uint8_t)(cs ^ (uint8_t)payload[i]);
    }
    return ((cs == (uint8_t)((host_hex_value(cs_hi) << 4) | host_hex_value(cs_lo))) ? 1U : 0U);
}

/* 把 payload 按 ',' 切成 token，返回 token 个数（0 表示失败） */
static uint8_t host_tokenize(char *text, uint8_t max_tokens)
{
    uint8_t n = 0U;
    char   *p = text;

    if (max_tokens == 0U)
    {
        return 0U;
    }
    host_tokens[n] = p;
    n++;
    while (*p != '\0')
    {
        if (*p == ',')
        {
            *p = '\0';
            if (n >= max_tokens)
            {
                return 0U;      /* 参数太多 */
            }
            host_tokens[n] = p + 1;
            n++;
        }
        p++;
    }
    return n;
}

static uint8_t host_require_argc(uint16_t id, uint8_t nargs, uint8_t want)
{
    if (nargs != want)
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "argc");
        return 0U;
    }
    return 1U;
}

/* 地址参数：只接受 1/3/4/5 */
static uint8_t host_arg_axis(uint16_t id, const char *text, uint8_t *addr)
{
    uint32_t v;

    if (!host_parse_u32(text, &v) || (v == 0U) || (v > 255UL))
    {
        host_body_err(&host_out, id, HOST_E_AXIS);
        return 0U;
    }
    if (control_index_of((uint8_t)v) == 0xFFU)
    {
        host_body_err(&host_out, id, HOST_E_AXIS);
        host_kv_u32(&host_out, "addr", v);
        return 0U;
    }
    *addr = (uint8_t)v;
    return 1U;
}

static uint8_t host_arg_u32(uint16_t id, const char *text, uint32_t min,
                            uint32_t max, uint32_t *out)
{
    if (!host_parse_u32(text, out) || (*out < min) || (*out > max))
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "range");
        return 0U;
    }
    return 1U;
}

/* ==========================================================================
 * 命令实现
 * ========================================================================== */

static void host_cmd_hello(uint16_t id, uint8_t nargs)
{
    uint32_t pv;
    uint32_t session;

    if (!host_require_argc(id, nargs, 2U))
    {
        return;
    }
    if (!host_arg_u32(id, host_tokens[2], 1U, 65535U, &pv))
    {
        return;
    }
    if (!host_arg_u32(id, host_tokens[3], 0U, 0xFFFFFFFFUL, &session))
    {
        return;
    }
    if (pv != HOST_PROTO_VERSION)
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_u32(&host_out, "want", HOST_PROTO_VERSION);
        host_kv_u32(&host_out, "got", pv);
        return;
    }

    host_session_clear();           /* 新会话：旧请求 id 全部作废 */
    host_query_reset_all();         /* 也丢掉上一次没回完的查询 */
    host_ready = 1U;
    host_link_lost = 0U;
    host_boot_id++;
    host_owner = "pc";

    host_body_ok(&host_out, id);
    host_kv_str(&host_out, "fw", HOST_FIRMWARE_VERSION);
    host_kv_u32(&host_out, "proto", HOST_PROTO_VERSION);
    host_out_char(&host_out, ','); host_out_str(&host_out, "boot=");
    host_out_hex32(&host_out, host_boot_id);
    host_kv_str(&host_out, "axes", "1/3/4/5");
    /* 只有 Z 轴驱动器 TXD 接了 PA3；没接线时查询会超时回 E04，不会编造读数 */
    host_kv_str(&host_out, "readback", HOST_READBACK_AXES);
}

static void host_cmd_ping(uint16_t id, uint8_t nargs)
{
    if (!host_require_argc(id, nargs, 0U))
    {
        return;
    }
    host_body_ok(&host_out, id);
    host_kv_u32(&host_out, "ms", Stepper_Millis());
}

static void host_cmd_status(uint16_t id, uint8_t nargs)
{
    uint8_t i;
    uint8_t first = 1U;

    if (!host_require_argc(id, nargs, 0U))
    {
        return;
    }
    host_body_ok(&host_out, id);
    host_kv_u32(&host_out, "latch", control_stop_latched());
    host_kv_str(&host_out, "owner", host_owner);
    host_out_char(&host_out, ','); host_out_str(&host_out, "running=");
    for (i = 0U; i < CONTROL_AXIS_COUNT; i++)
    {
        if ((control_axis[i].mode != CONTROL_MODE_STOP) && (control_axis[i].rpm > 0U))
        {
            if (!first)
            {
                host_out_char(&host_out, '/');
            }
            host_out_u32(&host_out, control_axis[i].addr);
            first = 0U;
        }
    }
    if (first)
    {
        host_out_char(&host_out, '-');
    }
    /* 步骤 2 的发送队列落地前，这里恒为 0 */
    host_kv_u32(&host_out, "queued", 0U);
    host_kv_u32(&host_out, "gripper", control_gripper_angle);
}

static void host_cmd_motor_enable(uint16_t id, uint8_t nargs)
{
    uint8_t  addr;
    uint32_t on;
    ControlAxis *ax;

    if (!host_require_argc(id, nargs, 2U)) { return; }
    if (!host_arg_axis(id, host_tokens[2], &addr)) { return; }
    if (!host_arg_u32(id, host_tokens[3], 0U, 1U, &on)) { return; }
    if (control_stop_latched())
    {
        host_body_err(&host_out, id, HOST_E_LATCHED);
        return;
    }
    if (!control_apply_enable(addr, (uint8_t)on))
    {
        host_body_err(&host_out, id, HOST_E_BUSY);
        return;
    }
    ax = control_get(addr);
    host_body_ok(&host_out, id);
    host_kv_u32(&host_out, "addr", addr);
    host_kv_u32(&host_out, "enabled", ax->enabled);
}

static void host_cmd_motor_speed(uint16_t id, uint8_t nargs)
{
    uint8_t  addr;
    uint32_t dir;
    uint32_t rpm;
    uint32_t acc;
    uint32_t task = 0U;

    if (!host_require_argc(id, nargs, 4U)) { return; }
    if (!host_arg_axis(id, host_tokens[2], &addr)) { return; }
    if (!host_arg_u32(id, host_tokens[3], 0U, 1U, &dir)) { return; }
    if (!host_arg_u32(id, host_tokens[4], 1U, 6000U, &rpm)) { return; }
    if (!host_arg_u32(id, host_tokens[5], 0U, 255U, &acc)) { return; }
    if (rpm > (uint32_t)host_param_value("speed_rpm_max"))
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "speed_limit");
        host_kv_u32(&host_out, "max", host_param_value("speed_rpm_max"));
        return;
    }
    if (control_stop_latched())
    {
        host_body_err(&host_out, id, HOST_E_LATCHED);
        return;
    }
    if (!control_apply_speed(addr, (uint8_t)dir, (uint16_t)rpm, (uint8_t)acc, &task))
    {
        host_body_err(&host_out, id, HOST_E_BUSY);
        return;
    }
    host_body_ok(&host_out, id);
    host_kv_u32(&host_out, "addr", addr);
    host_kv_u32(&host_out, "rpm", rpm);
    host_kv_u32(&host_out, "task", task);
    host_queue_task_event(task, "sent");
}

static void host_cmd_motor_pos(uint16_t id, uint8_t nargs)
{
    uint8_t  addr;
    uint32_t dir;
    uint32_t rpm;
    uint32_t acc;
    uint32_t pulses;
    uint8_t  absolute = 0U;
    uint32_t task = 0U;
    uint8_t  r;

    if (!host_require_argc(id, nargs, 6U)) { return; }
    if (!host_arg_axis(id, host_tokens[2], &addr)) { return; }
    if (!host_arg_u32(id, host_tokens[3], 0U, 1U, &dir)) { return; }
    if (!host_arg_u32(id, host_tokens[4], 1U, 6000U, &rpm)) { return; }
    if (!host_arg_u32(id, host_tokens[5], 0U, 255U, &acc)) { return; }
    if (!host_arg_u32(id, host_tokens[6], 0U, 0x7FFFFFFFUL, &pulses)) { return; }
    if (host_streq(host_tokens[7], "rel"))
    {
        absolute = 0U;
    }
    else if (host_streq(host_tokens[7], "abs"))
    {
        absolute = 1U;
    }
    else
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "mode");
        return;
    }
    if (rpm > (uint32_t)host_param_value("speed_rpm_max"))
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "speed_limit");
        host_kv_u32(&host_out, "max", host_param_value("speed_rpm_max"));
        return;
    }
    if (control_stop_latched())
    {
        host_body_err(&host_out, id, HOST_E_LATCHED);
        return;
    }

    r = control_apply_position(addr, (uint8_t)dir, (uint16_t)rpm, (uint8_t)acc,
                               pulses, absolute, &task);
    if (r == 2U)
    {
        host_body_err(&host_out, id, HOST_E_NO_POSITION_REF);
        host_kv_str(&host_out, "reason", "no_position_reference");
        return;
    }
    if (r != 1U)
    {
        host_body_err(&host_out, id, HOST_E_BUSY);
        return;
    }
    host_body_ok(&host_out, id);
    host_kv_u32(&host_out, "addr", addr);
    host_kv_u32(&host_out, "pulses", pulses);
    host_kv_str(&host_out, "mode", absolute ? "abs" : "rel");
    host_kv_u32(&host_out, "task", task);
    host_queue_task_event(task, "sent");
}

static void host_cmd_motor_stop(uint16_t id, uint8_t nargs)
{
    uint8_t addr;

    if (!host_require_argc(id, nargs, 1U)) { return; }
    if (!host_arg_axis(id, host_tokens[2], &addr)) { return; }
    if (!control_apply_stop(addr))
    {
        host_body_err(&host_out, id, HOST_E_BUSY);
        return;
    }
    host_body_ok(&host_out, id);
    host_kv_u32(&host_out, "addr", addr);
}

static void host_cmd_stop_all(uint16_t id, uint8_t nargs)
{
    if (!host_require_argc(id, nargs, 0U)) { return; }

    control_latch_stop(Stepper_Millis());
    host_body_ok(&host_out, id);
    host_kv_str(&host_out, "axes", "1/3/4/5");
    host_queue_latch_event("set", "stop_all");
}

static void host_cmd_motor_stall_clear(uint16_t id, uint8_t nargs)
{
    uint8_t addr;

    if (!host_require_argc(id, nargs, 1U)) { return; }
    if (!host_arg_axis(id, host_tokens[2], &addr)) { return; }
    if (!control_apply_stall_clear(addr))
    {
        host_body_err(&host_out, id, HOST_E_BUSY);
        return;
    }
    host_body_ok(&host_out, id);
    host_kv_u32(&host_out, "addr", addr);
}

static void host_cmd_motor_zero(uint16_t id, uint8_t nargs)
{
    uint8_t addr;
    uint8_t r;

    if (!host_require_argc(id, nargs, 1U)) { return; }
    if (!host_arg_axis(id, host_tokens[2], &addr)) { return; }
    r = control_apply_zero(addr);
    if (r == 2U)
    {
        host_body_err(&host_out, id, HOST_E_BUSY);
        host_kv_str(&host_out, "reason", "moving");
        return;
    }
    if (r != 1U)
    {
        host_body_err(&host_out, id, HOST_E_BUSY);
        return;
    }
    host_body_ok(&host_out, id);
    host_kv_u32(&host_out, "addr", addr);
    host_kv_u32(&host_out, "position", 0U);
}

static void host_cmd_motor_query(uint16_t id, uint8_t nargs)
{
    uint8_t  addr;
    uint8_t  what;
    uint32_t now;

    if (!host_require_argc(id, nargs, 2U)) { return; }
    if (!host_arg_axis(id, host_tokens[2], &addr)) { return; }

    if (host_streq(host_tokens[3], "vel"))       { what = EMM_QUERY_VEL; }
    else if (host_streq(host_tokens[3], "pos"))  { what = EMM_QUERY_POS; }
    else if (host_streq(host_tokens[3], "flag")) { what = EMM_QUERY_FLAG; }
    else
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "what");
        return;
    }

    /* 驱动器 TXD 没接到 PA3 的轴：如实报"无回读能力"，绝不编造实测值 */
    if (!host_axis_has_readback(addr))
    {
        host_body_err(&host_out, id, HOST_E_NO_READBACK);
        host_kv_u32(&host_out, "addr", addr);
        return;
    }

    now = Stepper_Millis();
    if (!EmmQuery_Start(&host_emm_query, addr, what, now,
                        (uint32_t)host_param_value("query_timeout_ms")))
    {
        /* 一次只留一个查询事务；上一次还没收完就先说忙，不排队、不覆盖 */
        host_body_err(&host_out, id, HOST_E_BUSY);
        host_kv_str(&host_out, "reason", "query_busy");
        host_kv_u32(&host_out, "addr", addr);
        return;
    }

    /* 应答要等驱动器回包：请求原文已由 host_handle_line 存好，
     * host_query_service 收到回包（或超时）后再补发 #<id>,ok/err */
    host_defer_line    = 1U;
    host_query_pending = 1U;
    host_defer_id      = id;
    host_defer_addr    = addr;
    host_defer_what    = what;
}

static void host_cmd_gripper(uint16_t id, uint8_t nargs)
{
    uint32_t open_deg;
    uint32_t grab_deg;
    uint32_t step_deg;
    uint32_t settle;
    int32_t  target;

    if (!host_require_argc(id, nargs, 1U)) { return; }

    open_deg = host_param_value("gripper_open_deg");
    grab_deg = host_param_value("gripper_grab_deg");
    step_deg = host_param_value("gripper_step_deg");
    settle   = host_param_value("gripper_settle_ms");

    if (host_streq(host_tokens[2], "open"))
    {
        target = (int32_t)open_deg;
    }
    else if (host_streq(host_tokens[2], "grab"))
    {
        target = (int32_t)grab_deg;
    }
    else if (host_streq(host_tokens[2], "step_grab"))
    {
        target = (int32_t)control_gripper_angle - (int32_t)step_deg;
        if (target < (int32_t)grab_deg)
        {
            target = (int32_t)grab_deg;
        }
    }
    else if (host_streq(host_tokens[2], "step_release"))
    {
        target = (int32_t)control_gripper_angle + (int32_t)step_deg;
        if (target > (int32_t)open_deg)
        {
            target = (int32_t)open_deg;
        }
    }
    else
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "action");
        return;
    }

    (void)control_gripper_apply((uint16_t)target, (uint16_t)settle, Stepper_Millis());
    host_body_ok(&host_out, id);
    host_kv_str(&host_out, "action", host_tokens[2]);
    host_kv_u32(&host_out, "angle", (uint32_t)target);
    host_kv_u32(&host_out, "settle_ms", settle);
}

static void host_cmd_servo_angle(uint16_t id, uint8_t nargs)
{
    uint32_t deg;
    uint32_t open_deg;
    uint32_t grab_deg;
    uint32_t settle;

    if (!host_require_argc(id, nargs, 1U)) { return; }
    if (!host_arg_u32(id, host_tokens[2], 0U, 270U, &deg)) { return; }

    open_deg = host_param_value("gripper_open_deg");
    grab_deg = host_param_value("gripper_grab_deg");
    settle   = host_param_value("gripper_settle_ms");
    if ((deg < grab_deg) || (deg > open_deg))
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "out_of_range");
        host_kv_u32(&host_out, "min", grab_deg);
        host_kv_u32(&host_out, "max", open_deg);
        return;
    }

    (void)control_gripper_apply((uint16_t)deg, (uint16_t)settle, Stepper_Millis());
    host_body_ok(&host_out, id);
    host_kv_u32(&host_out, "deg", deg);
    host_kv_u32(&host_out, "settle_ms", settle);
}

static void host_cmd_param_get(uint16_t id, uint8_t nargs)
{
    HostParam *p;

    if (!host_require_argc(id, nargs, 1U)) { return; }
    p = host_param_find(host_tokens[2]);
    if (p == 0)
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "unknown_param");
        return;
    }
    host_body_ok(&host_out, id);
    host_kv_str(&host_out, "name", p->name);
    host_kv_u32(&host_out, "value", p->value);
}

static void host_cmd_param_set(uint16_t id, uint8_t nargs)
{
    HostParam *p;
    int32_t    value;

    if (!host_require_argc(id, nargs, 2U)) { return; }
    p = host_param_find(host_tokens[2]);
    if (p == 0)
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "unknown_param");
        return;
    }
    if (!host_parse_i32(host_tokens[3], &value))
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "range");
        return;
    }
    if ((value < (int32_t)p->min) || (value > (int32_t)p->max))
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "range");
        host_kv_u32(&host_out, "min", p->min);
        host_kv_u32(&host_out, "max", p->max);
        return;
    }

    host_body_ok(&host_out, id);
    host_kv_str(&host_out, "name", p->name);
    host_kv_u32(&host_out, "before", p->value);
    p->value = (uint16_t)value;
    host_kv_u32(&host_out, "value", p->value);
}

static void host_cmd_tele_on(uint16_t id, uint8_t nargs)
{
    uint8_t  addr;
    uint32_t interval;

    if (!host_require_argc(id, nargs, 2U)) { return; }
    if (!host_arg_axis(id, host_tokens[2], &addr)) { return; }
    if (!host_arg_u32(id, host_tokens[3], 20U, 60000U, &interval)) { return; }

    host_tele_axis        = addr;
    host_tele_interval_ms = (uint16_t)interval;
    host_tele_next_ms     = Stepper_Millis();

    host_body_ok(&host_out, id);
    host_kv_u32(&host_out, "addr", addr);
    host_kv_u32(&host_out, "interval_ms", interval);
}

static void host_cmd_tele_off(uint16_t id, uint8_t nargs)
{
    if (!host_require_argc(id, nargs, 0U)) { return; }

    host_tele_axis        = 0U;
    host_tele_interval_ms = 0U;
    host_body_ok(&host_out, id);
    host_kv_u32(&host_out, "subs", 0U);
}

static void host_cmd_stop_clear(uint16_t id, uint8_t nargs)
{
    if (!host_require_argc(id, nargs, 0U)) { return; }

    control_clear_stop();
    host_body_ok(&host_out, id);
    host_kv_u32(&host_out, "latch", 0U);
}

/* ==========================================================================
 * 分发
 * ========================================================================== */

static void host_dispatch(uint16_t id, uint8_t ntok)
{
    const char *cmd;
    uint8_t     nargs;

    if (ntok < 2U)
    {
        host_body_err(&host_out, id, HOST_E_UNKNOWN_CMD);
        return;
    }
    cmd   = host_tokens[1];
    nargs = (uint8_t)(ntok - 2U);

    if (host_streq(cmd, "hello"))            { host_cmd_hello(id, nargs); }
    else if (host_streq(cmd, "ping"))        { host_cmd_ping(id, nargs); }
    else if (host_streq(cmd, "status"))      { host_cmd_status(id, nargs); }
    else if (host_streq(cmd, "motor_enable")){ host_cmd_motor_enable(id, nargs); }
    else if (host_streq(cmd, "motor_speed")) { host_cmd_motor_speed(id, nargs); }
    else if (host_streq(cmd, "motor_pos"))   { host_cmd_motor_pos(id, nargs); }
    else if (host_streq(cmd, "motor_stop"))  { host_cmd_motor_stop(id, nargs); }
    else if (host_streq(cmd, "stop_all"))    { host_cmd_stop_all(id, nargs); }
    else if (host_streq(cmd, "motor_stall_clear")) { host_cmd_motor_stall_clear(id, nargs); }
    else if (host_streq(cmd, "motor_zero"))  { host_cmd_motor_zero(id, nargs); }
    else if (host_streq(cmd, "motor_query")) { host_cmd_motor_query(id, nargs); }
    else if (host_streq(cmd, "gripper"))     { host_cmd_gripper(id, nargs); }
    else if (host_streq(cmd, "servo_angle")) { host_cmd_servo_angle(id, nargs); }
    else if (host_streq(cmd, "param_get"))   { host_cmd_param_get(id, nargs); }
    else if (host_streq(cmd, "param_set"))   { host_cmd_param_set(id, nargs); }
    else if (host_streq(cmd, "tele_on"))     { host_cmd_tele_on(id, nargs); }
    else if (host_streq(cmd, "tele_off"))    { host_cmd_tele_off(id, nargs); }
    else if (host_streq(cmd, "stop_clear"))  { host_cmd_stop_clear(id, nargs); }
    else
    {
        host_body_err(&host_out, id, HOST_E_UNKNOWN_CMD);
    }
}

/* 超长帧：内容已丢，但行首的 id 还在，用它回一条 E06。
 * 这种情况不入会话缓存——缓存里必须是完整请求，否则会把 id 毒化。 */
static void host_handle_oversize(void)
{
    uint32_t id32 = 0U;
    uint16_t id = 0U;

    host_line[host_line_len] = '\0';
    if (host_parse_lead_id(host_line, &id32) && (id32 >= 1UL) && (id32 <= 65535UL))
    {
        id = (uint16_t)id32;
    }
    host_body_err(&host_out, id, HOST_E_OVERFLOW);
    (void)host_out_finish(&host_out);
}

/* 一行收齐（LF 已去掉，行内不含 '$'） */
static void host_handle_line(void)
{
    char        *star;
    uint16_t     id = 0U;
    uint8_t      has_id = 0U;
    uint8_t      cs_ok = 0U;
    uint8_t      ntok;
    uint16_t     payload_len;
    uint32_t     id32;
    HostSession *dup;

    host_line[host_line_len] = '\0';

    /* 先按 payload（'*' 之前）算校验和，再破坏性地切 token */
    star = host_strchr(host_line, '*');
    if ((star != 0) &&
        (host_hex_value(star[1]) != 0xFFU) &&
        (host_hex_value(star[2]) != 0xFFU) &&
        (star[3] == '\0'))
    {
        payload_len = (uint16_t)(star - host_line);
        cs_ok = host_checksum_ok(host_line, payload_len, star[1], star[2]);
    }

    /* 校验不过也要尽量把 id 取出来，方便上位机把错误对上是哪条请求 */
    if (host_parse_lead_id(host_line, &id32))
    {
        if ((id32 >= 1UL) && (id32 <= 65535UL))
        {
            id = (uint16_t)id32;
            has_id = 1U;
        }
    }
    if (!has_id)
    {
        host_body_err(&host_out, 0U, HOST_E_ARG);
        (void)host_out_finish(&host_out);
        return;
    }

    if (!cs_ok)
    {
        /* 校验错的帧不进程缓存：否则同一个 id 修好再发会被判成冲突 */
        host_body_err(&host_out, id, HOST_E_CHECKSUM);
        (void)host_out_finish(&host_out);
        return;
    }

    *star = '\0';
    payload_len = (uint16_t)(star - host_line);

    /* 先切 token（把 ',' 换成 '\0'）。切分是确定性的，缓存里存的也是切分后的文本，
     * 所以"同 id 同内容"的比较不受影响。 */
    ntok = host_tokenize(host_line, HOST_MAX_TOKENS);

    /* 同 id 同内容：只重发原应答，不重复执行（相对位移尤其重要） */
    dup = host_session_find(id);
    if (dup != 0)
    {
        if ((dup->req_len == (uint8_t)payload_len) &&
            (host_mem_eq(dup->req, host_line, (uint8_t)payload_len)))
        {
            if (dup->rsp_len > 0U)
            {
                (void)HostLink_QueueTx((const uint8_t *)dup->rsp, dup->rsp_len);
            }
            return;
        }
        host_body_err(&host_out, id, HOST_E_ID_CONFLICT);
        (void)host_out_finish(&host_out);   /* 保留原内容，不覆盖缓存 */
        return;
    }

    if (ntok == 0U)
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        (void)host_out_finish(&host_out);
        return;
    }

    /* 需要等驱动器回包的查询（motor_query）要靠这段原文补发应答；
     * 存的是已切分（',' 换成 '\0'）的文本，和会话缓存里的格式一致。
     * 上一条查询还没收完时不能覆盖它——那条查询的应答也要用这段原文。 */
    host_defer_line = 0U;
    if (host_query_pending == 0U)
    {
        host_save_defer_req(host_line, payload_len);
    }

    host_dispatch(id, ntok);
    if (host_defer_line == 0U)
    {
        (void)host_emit(id, host_line, payload_len);
    }
    host_flush_pending_event();     /* 应答先出，事件后出 */
}

/* ==========================================================================
 * 收字节
 * ========================================================================== */

static void host_feed(uint8_t b)
{
    if (b == (uint8_t)'$')
    {
        /* 起始符：无论当前有没有残帧都重新开始。
         * 半条帧（丢字节、超长、链路复位）不允许污染下一条命令。 */
        host_line_len      = 0U;
        host_line_oversize = 0U;
        host_line_active   = 1U;
        return;
    }
    if (b == (uint8_t)'!')
    {
        host_stop_pending = 1U;     /* 帧内帧外都算停止请求 */
        host_line_reset();          /* 当前残帧作废 */
        return;
    }
    if (!host_line_active)
    {
        return;                     /* 帧外的一切字节都丢弃 */
    }

    if (b == (uint8_t)'\r')
    {
        return;
    }
    if (b == (uint8_t)'\n')
    {
        if (host_line_oversize)
        {
            host_overflow_count++;
            host_handle_oversize();
        }
        else
        {
            host_handle_line();
        }
        host_line_reset();
        return;
    }
    if (host_line_len >= HOST_MAX_LINE_CHARS)
    {
        host_line_oversize = 1U;
        host_line_active   = 1U;
        return;
    }
    host_line[host_line_len] = (char)b;
    host_line_len++;
}

/* ==========================================================================
 * 对外入口
 * ========================================================================== */

static void host_protocol_init(void)
{
    host_line_reset();
    host_overflow_count = 0U;
    host_stop_pending   = 0U;
    host_pending_kind   = HOST_PENDING_NONE;
    host_ready          = 0U;
    host_boot_id        = 0U;
    host_last_rx_ms     = Stepper_Millis();
    host_link_lost      = 0U;
    host_owner          = "none";
    host_tele_axis      = 0U;
    host_tele_interval_ms = 0U;
    host_tele_next_ms   = 0U;

    host_session_clear();
    host_query_reset_all();
    control_reset_state();
}

static void host_protocol_task(void)
{
    uint32_t now = Stepper_Millis();
    uint16_t gap = host_param_value("frame_gap_ms");
    uint8_t  b;
    uint8_t  n = 0U;

    /* 1. 传输层锁存的停止字符 */
    if (HostLink_TakeStopRequest())
    {
        host_stop_pending = 1U;
    }

    /* 2. 链路错误：步骤 1 只清计数；等 host_usb.c 接上后在这里上报 link 事件 */
    (void)HostLink_TakeRxError();

    /* 3. 停止错发与夹爪等待（都靠毫秒时基，不阻塞） */
    (void)control_service(now, gap);
    if (control_take_gripper_settled())
    {
        host_emit_task_event(0U, "settle_elapsed");
    }

    /* 4. 收字节（每轮上限，保证按键/停止回路的响应） */
    while ((n < HOST_READ_BYTES_PER_TASK) && HostLink_ReadByte(&b))
    {
        host_last_rx_ms = now;
        host_feed(b);
        n++;
    }

    /* 4.5 驱动器回读（PA3）：本轮的字节已经在上面收完，这里解析并补发应答 */
    host_query_service(now);

    /* 5. 停止字符：置锁存 + 错发四轴停止 + 报事件 */
    if (host_stop_pending)
    {
        host_stop_pending = 0U;
        if (!control_stop_latched())
        {
            control_latch_stop(now);
            (void)host_emit_latch_event("set", "stop_char");
        }
    }

    /* 6. 心跳监视：失联只停车 + 锁存，不关 Z 轴使能（防止垂直机构下滑） */
    if (host_ready && !host_link_lost)
    {
        if ((uint32_t)(now - host_last_rx_ms) > (uint32_t)host_param_value("link_timeout_ms"))
        {
            host_link_lost = 1U;
            host_owner     = "none";
            control_latch_stop(now);
            (void)host_emit_link_event("timeout");
        }
    }

    /* 7. 遥测：默认关，只有 tele_on 订阅后才发。
     *    订阅的是有回读的轴时，顺便起一次位置查询，下一拍就有实测值。 */
    if ((host_ready) && (host_tele_axis != 0U) && (host_tele_interval_ms > 0U))
    {
        if ((int32_t)(now - host_tele_next_ms) >= 0)
        {
            if (host_axis_has_readback(host_tele_axis))
            {
                host_query_background(now);
            }
            host_emit_telemetry(host_tele_axis);
            host_tele_next_ms = now + (uint32_t)host_tele_interval_ms;
        }
    }
}

static uint8_t host_protocol_is_ready(void)
{
    return host_ready;
}

static uint8_t host_protocol_is_link_lost(void)
{
    return host_link_lost;
}

static uint16_t host_protocol_overflow_count(void)
{
    return host_overflow_count;
}

#endif /* HOST_PROTOCOL_INLINE_H */
