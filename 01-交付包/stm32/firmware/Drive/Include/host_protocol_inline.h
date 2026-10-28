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

/* 回读能力：哪些驱动器的 TXD 接到了回读线上，用 readback_mask 参数表示
 * （bit0=Z(1)，bit1=托盘(3)，bit2=X(4)，bit3=Y(5)），**出厂默认 15 = 四轴都接**。
 * hello 必须如实报出这份名单：上位机据此决定哪些轴显示实测值。
 * 多轴怎么共用一条回读线由硬件决定（**四路推挽 TXD 不能直接短接**：各串二极管做
 * 线与、各串 1kΩ 限流、模拟开关选路，或改 RS-485；详见 ghr/10.15/上位机/README.md）。
 * 某个轴还没接就先 `param_set,readback_mask,<掩码>` 把它去掉，查询会在
 * query_timeout_ms 后回 E04，没在名单里的轴一律回 E11，都不会编数据。 */

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
/* E14：闭环 goto 没能收敛（超时 / 补差次数用完 / 途中被停）。
 * 只在 motor_goto 上出现，细节见 reason= 字段（timeout / lost / stopped）。 */
#define HOST_E_GOTO               "E14"

/* ---------------- 运行参数 ---------------- */
/* 名字必须与上位机 protocol.PARAM_DEFAULTS 完全一致（那边有测试守着这张表）。 */

typedef struct
{
    const char *name;
    uint32_t    value;   /* 用 uint32：counts_per_rev 要能放 65536，uint16 不够 */
    uint32_t    min;
    uint32_t    max;
} HostParam;

#define HOST_PARAM_COUNT 22U

/* 出厂默认值。host_protocol_init() 会把下面的运行参数表恢复成这一份，
 * 所以 MCU 复位后参数一定回到默认，不会带着上一次会话的临时值。 */
static const HostParam host_param_defaults[HOST_PARAM_COUNT] =
{
    { "speed_rpm",         60U,   1U,  6000U  },
    { "acc",               1U,    0U,  255U   },
    { "speed_rpm_max",     300U,  1U,  6000U  },
    { "gripper_open_deg",  180U,  0U,  270U   },
    { "gripper_ready_deg", 150U,  0U,  270U   },
    { "gripper_grab_deg",  135U,  0U,  270U   },
    { "gripper_step_deg",  45U,   1U,  270U   },
    { "gripper_settle_ms", 200U,  0U,  60000U },
    /* 夹紧后最多保持多久就停脉冲卸力，0 = 不卸力（默认）。
     * 只在调试“长时间夹住物体”时调大，比赛流程保持 0。
     * 注意 HostParam 是 uint16_t，上限 60000 ms（60 秒）。 */
    { "gripper_hold_max_ms", 0U,  0U,  60000U },
    { "heartbeat_ms",      500U,  20U, 60000U },
    { "link_timeout_ms",   3000U, 100U, 60000U },
    { "query_timeout_ms",  100U,  10U, 60000U },
    { "telemetry_ms",      200U,  20U, 60000U },
    { "frame_gap_ms",      10U,   1U,  1000U  },
    /* 哪些轴接了驱动器回读线（bit0=Z=1、bit1=托盘=3、bit2=X=4、bit3=Y=5）。
     * 默认 15 = 四轴都接；某个轴还没接就改小这个参数，不用重烧固件。
     * 名单外的轴查询如实回 E11，名单内但收不到回包的轴超时回 E04。 */
    { "readback_mask",     15U,   1U,  15U    },

    /* ---- 闭环 goto（motor_goto）用的六个参数 ---- */
    /* 编码器一圈有多少计数。EMM 文档写的是 65536（0x36 回 16 位角度），
     * 但必须以现场标定为准：X 轴走 3200 脉冲，看 counts 变化多少，填进这里。
     * 标定方法和数值记录见 docs/标定操作手册。 */
    { "counts_per_rev",    65536U, 1U,  1048576U },
    /* 允许多少计数误差算“到位”。默认 200 ≈ 1.1°；机械误差通常比这大，
     * 设太小会在目标附近来回补差。 */
    { "goto_tol_counts",   200U,  1U,  65535U },
    /* 粗走转速 / 补差转速（RPM） */
    { "goto_rpm",          300U,  1U,  6000U  },
    { "goto_final_rpm",    60U,   1U,  1000U  },
    /* 最多补差几次，超过就报 E14 */
    { "goto_max_passes",   4U,    1U,  20U    },
    /* 单次 goto 总超时，0 = 按距离自动估算（估算值 ×3 + 4 s） */
    { "goto_max_ms",       0U,    0U,  120000U },
    /* 转盘角度模式允许多少计数误差算到位。默认 90 ≈ 0.5°（赛题要求盒位不偏）。 */
    { "angle_tol_counts",  90U,   1U,  1000U  }
};

static HostParam host_params[HOST_PARAM_COUNT];

static void host_params_restore_defaults(void)
{
    uint8_t i;

    for (i = 0U; i < HOST_PARAM_COUNT; i++)
    {
        host_params[i] = host_param_defaults[i];
    }
}

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

static uint32_t host_param_value(const char *name)
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
static void host_emit_telemetry(uint8_t addr);

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

/* 哪些轴接了回读线：bit i 对应 control_addr_table[i]（Z/托盘/X/Y）。
 * readback_mask 是运行参数，接线变了改参数即可，不用重新烧固件；
 * 没接线的轴就算打开也只是查询超时（E04），不会编数据出来。 */
static uint8_t host_readback_mask(void)
{
    return (uint8_t)(host_param_value("readback_mask") & 0x0FU);
}

static uint8_t host_axis_has_readback(uint8_t addr)
{
    uint8_t idx = control_index_of(addr);

    if (idx == 0xFFU)
    {
        return 0U;
    }
    return (uint8_t)((host_readback_mask() >> idx) & 1U);
}

/* 每个轴各存一份最近一次回读到的数：遥测轮着查时，绝不能用 A 轴的读数去报 B 轴 */
typedef struct
{
    uint8_t  have;         /* 收到过该轴的有效回包 */
    uint8_t  what;         /* 最后一笔是 EMM_QUERY_* 的哪一项 */
    int32_t  counts;       /* 0x36 */
    int16_t  rpm;          /* 0x35 */
    uint8_t  flag;         /* 0x3A */
    uint32_t stamp_ms;     /* 数据时间戳：界面据此显示"多少秒前的数" */
} HostAxisRead;

static HostAxisRead host_axis_read[CONTROL_AXIS_COUNT];

/* 遥测轮询走到哪了：一次只起一个事务，转一圈换下一个接了回读线的轴 */
static uint8_t host_sweep_index = 0U;

/* 转盘角度基准：motor_angle_zero 时把当时的 counts 记下来，之后 motor_angle
 * 用“基准 + 角度”算目标，驱动器自己的零点无所谓；掉电后基准失效，需重新对准。 */
static int32_t host_angle_base[CONTROL_AXIS_COUNT];
static uint8_t host_angle_base_ok[CONTROL_AXIS_COUNT];

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
    uint8_t i;

    EmmParser_Reset(&host_emm_parser);
    EmmQuery_Reset(&host_emm_query);
    host_defer_line    = 0U;
    host_query_pending = 0U;
    host_defer_id      = 0U;
    host_defer_req_len = 0U;
    host_defer_addr    = 0U;
    host_defer_what    = EMM_QUERY_NONE;
    host_sweep_index   = 0U;    /* 轮询位置也归零，否则新会话从中间某个轴开始 */
    for (i = 0U; i < CONTROL_AXIS_COUNT; i++)
    {
        host_axis_read[i].have     = 0U;
        host_axis_read[i].what     = EMM_QUERY_NONE;
        host_axis_read[i].counts   = 0;
        host_axis_read[i].rpm      = 0;
        host_axis_read[i].flag     = 0U;
        host_axis_read[i].stamp_ms = 0U;
    }
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

static HostAxisRead *host_read_of(uint8_t addr)
{
    uint8_t idx = control_index_of(addr);

    if (idx == 0xFFU)
    {
        return 0;
    }
    return &host_axis_read[idx];
}

/* 把刚收到的回包落到该轴自己的格子里（遥测和 motor_query 都从这里取数） */
static void host_query_store(void)
{
    HostAxisRead *r = host_read_of(host_emm_query.addr);

    if (r == 0)
    {
        return;
    }
    r->have     = 1U;
    r->what     = host_emm_query.what;
    r->counts   = host_emm_query.counts;
    r->rpm      = host_emm_query.rpm;
    r->flag     = host_emm_query.flag;
    r->stamp_ms = host_emm_query.stamp_ms;
}

static void host_query_answer(uint32_t now)
{
    HostAxisRead *r = host_read_of(host_defer_addr);

    host_body_ok(&host_out, host_defer_id);
    host_kv_u32(&host_out, "addr", (uint32_t)host_defer_addr);

    if (host_defer_what == EMM_QUERY_POS)
    {
        host_kv_i32(&host_out, "counts", (r != 0) ? r->counts : 0);
        host_kv_deg_x10(&host_out, "deg",
                        Emm_CountsToDegX10((r != 0) ? r->counts : 0));
    }
    else if (host_defer_what == EMM_QUERY_VEL)
    {
        host_kv_i32(&host_out, "rpm", (int32_t)((r != 0) ? r->rpm : 0));
    }
    else
    {
        host_kv_u32(&host_out, "flag", (uint32_t)((r != 0) ? r->flag : 0U));
    }
    host_kv_u32(&host_out, "age_ms",
                (r != 0) ? (uint32_t)(now - r->stamp_ms) : 0U);

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
    uint8_t was_active = host_emm_query.active;

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

    /* 事务从"在等"变成"结束了"且拿到了本次的新数据 = 这一笔查询成功 */
    if ((was_active != 0U) && (host_emm_query.active == 0U) &&
        host_query_has_fresh_data())
    {
        host_query_store();
        if (host_query_pending != 0U)
        {
            host_query_answer(now);
        }
        else if (host_tele_axis != 0U)
        {
            /* 后台轮到的轴：这一拍就把它报上去，不用等下一拍 */
            host_emit_telemetry(host_emm_query.addr);
        }
        return;
    }

    if ((host_emm_query.active != 0U) && EmmQuery_Expired(&host_emm_query, now))
    {
        if (host_query_pending != 0U)
        {
            host_query_timeout_answer();
        }
        /* 后台查询超时：只记账，下一拍自动换下一个轴 */
    }
}

/* 闭环 goto 的实现在本文件稍后；这里先声明，让 sweep 能优先刷 goto 的轴 */
static uint8_t host_goto_sweep_first(uint32_t now);

/* 遥测开着的时候轮着查接了回读线的轴：一次只起一个事务，
 * 不占 PC 的请求 id，也不和 PC 正在等的查询抢（PC 的优先）。
 * goto 激活时优先刷它那个轴：闭环要的是"自己的 counts 尽快变新"。 */
static void host_query_sweep(uint32_t now)
{
    uint8_t mask = host_readback_mask();
    uint8_t i;
    uint8_t idx;

    if (mask == 0U)
    {
        return;
    }
    if ((host_query_pending != 0U) || (host_emm_query.active != 0U))
    {
        return;
    }
    if (host_goto_sweep_first(now) != 0U)
    {
        return;
    }
    for (i = 0U; i < CONTROL_AXIS_COUNT; i++)
    {
        idx = (uint8_t)((host_sweep_index + i) % CONTROL_AXIS_COUNT);
        if (((mask >> idx) & 1U) == 0U)
        {
            continue;
        }
        if (EmmQuery_Start(&host_emm_query, control_addr_table[idx], EMM_QUERY_POS,
                           now, (uint32_t)host_param_value("query_timeout_ms")))
        {
            host_sweep_index = (uint8_t)((idx + 1U) % CONTROL_AXIS_COUNT);
            return;
        }
    }
}

/* ==========================================================================
 * 闭环 goto：用回读 counts 纠"丢步/背隙"的外层校正
 * ==========================================================================
 *
 * 为什么放固件：counts 就在本文件里，判定延迟 1 ms；放上位机要绕 WiFi 一圈
 * （200–400 ms），补差段会明显变慢。
 *
 * 为什么不用 motor_query 那条路：那条路目前有 bug（timeout 常驻），反正 counts
 * 已经由遥测轮询缓存好了，goto 直接读 host_axis_read[]，不新开查询通道。
 *
 * 流程：粗走一段 → 等 counts 稳定 → 量误差 → 误差超标就补一段 → 再来一次。
 * 判定"停住"不用固定延时，而是连续两个新样本 counts 不变（堵转也能识别）。
 */

#define HOST_GOTO_NONE    0U
#define HOST_GOTO_COARSE  1U
#define HOST_GOTO_SETTLE  2U
#define HOST_GOTO_MEASURE 3U
#define HOST_GOTO_DONE    4U
#define HOST_GOTO_FAIL    5U
#define HOST_GOTO_PRIME   6U   /* 等第一份新鲜的 counts，再决定起点 */
#define HOST_GOTO_GAP     7U   /* 发位移帧前先等总线安静 */

#define HOST_GOTO_FRESH_MS      3000U   /* counts 超过这么久没更新就不敢用它算误差 */
#define HOST_GOTO_STABLE_COUNTS 60      /* 两个新样本差多少算"没动"（放宽防误判）*/
#define HOST_GOTO_STABLE_NEED   1U      /* 连续几次"没动"算停稳（1 次即可）*/
#define HOST_GOTO_GAP_MS        10U     /* 离最后一份回读样本至少空这么久再发（20->10）*/

typedef struct
{
    uint8_t  active;
    uint8_t  phase;
    uint8_t  addr;
    uint8_t  dir;
    uint8_t  passes;
    uint8_t  stable;
    uint8_t  pending;        /* 1 = 有 PC 在等这条 goto 的应答 */
    uint16_t req_id;
    uint16_t rpm;
    int32_t  pulses;         /* 这一段发出的脉冲（绝对值） */
    int32_t  target_counts;  /* 期望 counts 变化量 */
    int32_t  tol_counts;
    int32_t  base_counts;    /* 起点 counts */
    int32_t  last_counts;    /* 最近一个样本 */
    int32_t  err_counts;     /* 最近一次测得的误差 */
    uint32_t started_ms;     /* 整条 goto 的起点（算超时） */
    uint32_t seg_start_ms;   /* 当前这一段的起点 */
    uint32_t seg_ms;         /* 当前这一段的估算时长 */
    uint32_t next_tx_ms;     /* GAP：等到这个时刻再发位移帧 */
    uint32_t last_stamp;     /* 最近一个样本的时间戳 */
    uint32_t max_ms;
    uint32_t next_query_ms;  /* goto 期间下一次允许发 0x36 的时刻 */
    uint32_t mute_query_ms;  /* 刚发完 FD 后的查询静默截止时刻 */
    uint8_t  tx_res;         /* 最近一次下发的返回值（1=成功） */
    uint8_t  tx_cnt;         /* 成功下发次数 */
    const char *fail_reason;
} HostGoto;

static HostGoto host_goto;

/* sweep 用：goto 正在跑的轴排在最前，这样它的 counts 刷新最快。
 * 返回 1 = 已经替它起了查询。 */
static uint8_t host_goto_sweep_first(uint32_t now)
{
    uint8_t idx;

    if (host_goto.active == 0U)
    {
        return 0U;
    }
    idx = control_index_of(host_goto.addr);
    if (idx == 0xFFU)
    {
        return 0U;
    }
    if (((host_readback_mask() >> idx) & 1U) == 0U)
    {
        return 0U;
    }
    if ((int32_t)(now - host_goto.mute_query_ms) < 0) { return 1U; }
    if ((int32_t)(now - host_goto.next_query_ms) < 0) { return 1U; }
    {
        uint8_t started = EmmQuery_Start(&host_emm_query, host_goto.addr, EMM_QUERY_POS,
                                         now, (uint32_t)host_param_value("query_timeout_ms"));
        if (started != 0U) { host_goto.next_query_ms = now + 20U; }
        return started;
    }
}

static uint32_t host_goto_counts_per_rev(void)
{
    uint32_t v = host_param_value("counts_per_rev");

    return (v == 0U) ? 1U : v;
}

static uint32_t host_goto_pulses_per_rev(void)
{
    /* 与 stepper.h 的 STEPPER_PULSES_PER_REV 保持一致（3200）。
     * 这里不直接引用宏，避免本文件被单独包含时的依赖。 */
    return 3200U;
}

/* 一段脉冲大概要走多久：脉冲 / (RPM × 3200 / 60) 秒，再留 250 ms 余量 */
static uint32_t host_goto_estimate_ms(int32_t pulses, uint32_t rpm)
{
    uint32_t abs_pulses = (uint32_t)((pulses < 0) ? -pulses : pulses);
    uint32_t ms;

    if (rpm == 0U)
    {
        rpm = 60U;
    }
    ms = (uint32_t)(((uint64_t)abs_pulses * 60000ULL) /
                    ((uint64_t)rpm * (uint64_t)host_goto_pulses_per_rev()));
    return ms + 250U;
}

static void host_goto_finish(void)
{
    if (host_goto.pending != 0U)
    {
        if (host_goto.phase == HOST_GOTO_DONE)
        {
            host_body_ok(&host_out, host_goto.req_id);
            host_kv_u32(&host_out, "addr", (uint32_t)host_goto.addr);
            host_kv_u32(&host_out, "passes", (uint32_t)host_goto.passes);
            host_kv_i32(&host_out, "err", host_goto.err_counts);
            host_kv_u32(&host_out, "elapsed_ms",
                        (uint32_t)(Stepper_Millis() - host_goto.started_ms));
            host_kv_u32(&host_out, "tx", (uint32_t)host_goto.tx_res);
            host_kv_u32(&host_out, "txc", (uint32_t)host_goto.tx_cnt);
            host_kv_u32(&host_out, "rpm", (uint32_t)host_goto.rpm);
            host_kv_u32(&host_out, "pulses", (uint32_t)host_goto.pulses);
            (void)host_emit(host_goto.req_id, host_defer_req, host_defer_req_len);
        }
        else
        {
            host_body_err(&host_out, host_goto.req_id, HOST_E_GOTO);
            host_kv_u32(&host_out, "addr", (uint32_t)host_goto.addr);
            host_kv_u32(&host_out, "passes", (uint32_t)host_goto.passes);
            host_kv_i32(&host_out, "err", host_goto.err_counts);
            host_kv_str(&host_out, "reason",
                        (host_goto.fail_reason != 0) ? host_goto.fail_reason : "lost");
            host_kv_u32(&host_out, "tx", (uint32_t)host_goto.tx_res);
            host_kv_u32(&host_out, "txc", (uint32_t)host_goto.tx_cnt);
            host_kv_u32(&host_out, "rpm", (uint32_t)host_goto.rpm);
            host_kv_u32(&host_out, "pulses", (uint32_t)host_goto.pulses);
            (void)host_emit(host_goto.req_id, host_defer_req, host_defer_req_len);
        }
        host_goto.pending = 0U;
        host_query_pending = 0U;
    }
    host_goto.active = 0U;
    host_goto.phase  = HOST_GOTO_NONE;
    host_flush_pending_event();
}

static void host_goto_fail(const char *reason)
{
    host_goto.fail_reason = reason;
    host_goto.phase = HOST_GOTO_FAIL;
    host_goto_finish();
}

/* 发一段相对位移（负数表示反向）。返回 0 = 下发失败 */
static uint8_t host_goto_move(int32_t pulses, uint16_t rpm)
{
    uint8_t dir = (uint8_t)((pulses < 0) ? 1U : 0U);
    int32_t mag = (pulses < 0) ? -pulses : pulses;

    if (mag <= 0)
    {
        return 0U;
    }
    host_goto.dir     = dir;
    host_goto.pulses  = mag;
    host_goto.rpm     = rpm;
    host_goto.seg_ms  = host_goto_estimate_ms(mag, (uint32_t)rpm);
    host_goto.seg_start_ms = Stepper_Millis();
    host_goto.stable  = 0U;
    host_goto.phase   = HOST_GOTO_COARSE;
    host_goto.tx_res = control_apply_position(host_goto.addr, dir, rpm,
                                              (uint8_t)host_param_value("acc"),
                                              (uint32_t)mag, 0U, 0);
    if (host_goto.tx_res == 1U)
    {
        host_goto.tx_cnt++;
        host_goto.mute_query_ms = Stepper_Millis() + 30U;
    }
    return (uint8_t)((host_goto.tx_res == 1U) ? 1U : 0U);
}

static void host_goto_begin(uint8_t addr, int32_t pulses, int32_t tol,
                            uint16_t rpm)
{
    HostAxisRead *r = host_read_of(addr);

    host_goto.active       = 1U;
    host_goto.phase        = HOST_GOTO_COARSE;
    host_goto.addr         = addr;
    host_goto.dir          = (uint8_t)((pulses < 0) ? 1U : 0U);
    host_goto.pulses       = (pulses < 0) ? -pulses : pulses;
    host_goto.passes       = 0U;
    host_goto.stable       = 0U;
    host_goto.err_counts   = 0;
    host_goto.target_counts = Emm_PulsesToCounts(pulses, host_goto_counts_per_rev(),
                                                 host_goto_pulses_per_rev());
    host_goto.tol_counts   = tol;
    host_goto.base_counts  = (r != 0) ? r->counts : 0;
    host_goto.last_counts  = host_goto.base_counts;
    host_goto.last_stamp   = (r != 0) ? r->stamp_ms : 0U;
    host_goto.started_ms   = Stepper_Millis();
    host_goto.seg_start_ms = host_goto.started_ms;
    host_goto.rpm          = rpm;
    host_goto.fail_reason  = 0;
    host_goto.tx_res       = 0U;
    host_goto.tx_cnt       = 0U;
    host_goto.next_query_ms = Stepper_Millis();
    host_goto.mute_query_ms = Stepper_Millis();

    {
        uint32_t max_ms = host_param_value("goto_max_ms");

        host_goto.max_ms = (max_ms != 0U) ? max_ms
            : (host_goto_estimate_ms(host_goto.pulses, (uint32_t)rpm) * 3U + 4000U);
    }

    /* 先进入 PRIME：等一份新鲜 counts 才定 base 并发出第一段，
     * 这样“刚连上、遥测还没刷新”也能直接用 goto。 */
    host_goto.phase = HOST_GOTO_PRIME;
    host_goto.stable = 0U;
}

/* 每轮调用。now 来自任务开头（与 query 同源，避免时间基不一致）。 */
static void host_goto_service(uint32_t now)
{
    HostAxisRead *r;

    if (host_goto.active == 0U)
    {
        return;
    }

    /* 时间差一律用有符号：(now 取自本轮任务开头，可能比 started_ms 早 1~2 ms，
     * 用无符号相减会回绕成天文数字，当拍就判超时 —— motor_query 的 E04 就是这类问题) */
    if ((int32_t)(now - host_goto.started_ms) > (int32_t)host_goto.max_ms)
    {
        /* 超时也要先停车，别让电机继续跑 */
        (void)control_apply_stop(host_goto.addr);
        host_goto_fail("timeout");
        return;
    }

    r = host_read_of(host_goto.addr);
    if (r == 0)
    {
        host_goto_fail("lost");
        return;
    }

    if (host_goto.phase == HOST_GOTO_PRIME)
    {
        /* 等第一份新鲜样本（goto 激活时 sweep 每拍都会开一次查询，几十毫秒就到） */
        if ((r->have != 0U) &&
            ((int32_t)(now - r->stamp_ms) <= (int32_t)HOST_GOTO_FRESH_MS))
        {
            host_goto.base_counts = r->counts;
            host_goto.last_counts = r->counts;
            host_goto.last_stamp  = r->stamp_ms;
            host_goto.next_tx_ms  = now + HOST_GOTO_GAP_MS;
            host_goto.phase       = HOST_GOTO_GAP;
        }
        return;
    }

    if (host_goto.phase == HOST_GOTO_GAP)
    {
        /* 实测：sweep 刚收到 0x36 应答就紧接着发 FD，驱动器会把 FD 整帧吞掉
         * （tx=1 但电机不动）。先让总线空 20ms 再发。 */
        if ((int32_t)(now - host_goto.next_tx_ms) >= 0)
        {
            {
                int32_t seg = (host_goto.dir != 0U)
                    ? -(int32_t)host_goto.pulses : (int32_t)host_goto.pulses;
            
                if (!host_goto_move(seg, host_goto.rpm))
                {
                    host_goto_fail("send");
                }
            }
        }
        return;
    }

    if (host_goto.phase == HOST_GOTO_COARSE)
    {
        /* 已发出这一段，等到估算时间（+250 ms 余量）再开始看 counts */
        if ((int32_t)(now - host_goto.seg_start_ms) >= (int32_t)host_goto.seg_ms)
        {
            host_goto.phase  = HOST_GOTO_SETTLE;
            host_goto.stable = 0U;
        }
        return;
    }

    /* 还能用这个样本吗：必须比 base 新，且不超时太旧 */
    if ((r->have == 0U) || (r->stamp_ms == host_goto.last_stamp) ||
        ((int32_t)(now - r->stamp_ms) > (int32_t)HOST_GOTO_FRESH_MS))
    {
        /* 没有新样本就先等；整条超时由上面的 elapsed 兜底 */
        return;
    }

    /* 有新样本：判断是不是停住了 */
    {
        int32_t delta = r->counts - host_goto.last_counts;

        host_goto.last_counts = r->counts;
        host_goto.last_stamp  = r->stamp_ms;

        if (host_goto.phase == HOST_GOTO_SETTLE)
        {
            if ((delta <= HOST_GOTO_STABLE_COUNTS) &&
                (delta >= -HOST_GOTO_STABLE_COUNTS))
            {
                host_goto.stable++;
            }
            else
            {
                host_goto.stable = 0U;
            }
            if (host_goto.stable < HOST_GOTO_STABLE_NEED)
            {
                return;
            }
            host_goto.phase = HOST_GOTO_MEASURE;
        }
    }

    if (host_goto.phase == HOST_GOTO_MEASURE)
    {
        int32_t measured = host_goto.last_counts - host_goto.base_counts;
        int32_t err      = host_goto.target_counts - measured;
        int32_t err_abs  = (err < 0) ? -err : err;
        int32_t corr_pulses;

        host_goto.err_counts = err;

        if (err_abs <= host_goto.tol_counts)
        {
            host_goto.phase = HOST_GOTO_DONE;
            host_goto_finish();
            return;
        }
        if (host_goto.passes >= (uint8_t)host_param_value("goto_max_passes"))
        {
            host_goto_fail("lost");
            return;
        }

        corr_pulses = Emm_CountsToPulses(err, host_goto_counts_per_rev(),
                                         host_goto_pulses_per_rev());
        if (corr_pulses == 0)
        {
            /* 误差不到一个脉冲的分辨率：再补也是原地踏步，当成到位 */
            host_goto.phase = HOST_GOTO_DONE;
            host_goto_finish();
            return;
        }

        host_goto.passes++;
        host_goto.pulses     = (corr_pulses < 0) ? -corr_pulses : corr_pulses;
        host_goto.dir        = (uint8_t)((corr_pulses < 0) ? 1U : 0U);
        host_goto.rpm        = (uint16_t)host_param_value("goto_final_rpm");
        host_goto.next_tx_ms = now + HOST_GOTO_GAP_MS;
        host_goto.phase      = HOST_GOTO_GAP;
    }
}

/* 外部要求中止（停止字符、stop_all、stop_clear 等）：先停车再报 E14 stopped */
static void host_goto_abort(const char *reason)
{
    if (host_goto.active != 0U)
    {
        (void)control_apply_stop(host_goto.addr);
        host_goto.fail_reason = reason;
        host_goto.phase  = HOST_GOTO_FAIL;
        host_goto_finish();
    }
}

/* 会话重新握手时静默丢弃 goto（不补发应答：那条连接已经不在了） */
static void host_goto_drop(void)
{
    host_goto.active  = 0U;
    host_goto.phase   = HOST_GOTO_NONE;
    host_goto.pending = 0U;
}

/* goto 跑着的时候，别让别的运动命令插进来抢同一个轴 */
static uint8_t host_goto_busy_error(uint16_t id)
{
    if (host_goto.active == 0U)
    {
        return 0U;
    }
    host_body_err(&host_out, id, HOST_E_BUSY);
    host_kv_str(&host_out, "reason", "goto_busy");
    return 1U;
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

/* 遥测：有回读的轴报该轴自己最近一次实测值，没接回读的轴显式写 readback=0 */
static void host_emit_telemetry(uint8_t addr)
{
    ControlAxis  *ax = control_get(addr);
    HostAxisRead *r  = host_read_of(addr);
    uint32_t      now;

    if (ax == 0)
    {
        return;
    }
    now = Stepper_Millis();

    host_body_head(&host_evt, 0U, "tele");
    host_kv_u32(&host_evt, "addr", ax->addr);
    host_kv_u32(&host_evt, "rpm", (uint32_t)((ax->mode == CONTROL_MODE_SPEED) ? ax->rpm : 0U));

    if (host_axis_has_readback(addr) && (r != 0) && (r->have != 0U))
    {
        host_kv_u32(&host_evt, "readback", 1U);
        if (r->what == EMM_QUERY_VEL)
        {
            host_kv_i32(&host_evt, "rpm_measured", (int32_t)r->rpm);
        }
        else if (r->what == EMM_QUERY_FLAG)
        {
            host_kv_u32(&host_evt, "flag", (uint32_t)r->flag);
        }
        else
        {
            host_kv_i32(&host_evt, "counts", r->counts);
            host_kv_deg_x10(&host_evt, "deg", Emm_CountsToDegX10(r->counts));
        }
        host_kv_u32(&host_evt, "age_ms", (uint32_t)(now - r->stamp_ms));
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

/* 按 readback_mask 生成 hello 里的回读名单，例如 "1/3/4/5"；一个都没有时给 "-" */
static void host_readback_list(char *buf, uint8_t cap)
{
    uint8_t mask = host_readback_mask();
    uint8_t i;
    uint8_t n = 0U;

    for (i = 0U; i < CONTROL_AXIS_COUNT; i++)
    {
        if (((mask >> i) & 1U) == 0U)
        {
            continue;
        }
        if (n > 0U)
        {
            if (n >= (uint8_t)(cap - 2U)) { break; }
            buf[n++] = '/';
        }
        if (n >= (uint8_t)(cap - 2U)) { break; }
        buf[n++] = (char)('0' + (char)control_addr_table[i]);
    }
    if (n == 0U)
    {
        buf[n++] = '-';
    }
    buf[n] = '\0';
}

static void host_cmd_hello(uint16_t id, uint8_t nargs)
{
    uint32_t pv;
    uint32_t session;
    char     rb[16];

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
    host_goto_drop();               /* 旧连接上没跑完的 goto 直接丢（不补应答） */
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
    /* 名单里的轴才是接了回读线的（readback_mask）；不在名单里的轴查询会回 E11 */
    host_readback_list(rb, (uint8_t)sizeof(rb));
    host_kv_str(&host_out, "readback", rb);
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
    if (host_goto_busy_error(id)) { return; }
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
    if (host_goto_busy_error(id)) { return; }
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
    if (host_goto_busy_error(id)) { return; }
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
    /* 手动停车：如果正在闭环 goto 就把它中止掉（会补发一条 E14 stopped） */
    host_goto_abort("stopped");
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

    host_goto_abort("stopped");
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
    if (host_goto_busy_error(id)) { return; }
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

/* motor_goto,<axis>,<delta_pulses>[,<tol_counts>[,<rpm>]]
 *
 * 走一段相对位移，然后用回读 counts 做外层校正：粗走 → 等停稳 → 量误差 →
 * 超标就补一小段，直到 |实测变化量 − 期望变化量| ≤ tol。tol 默认取参数
 * goto_tol_counts。失败（超时/补差用完/被停）回 E14，reason 说明原因。
 *
 * 前置：该轴必须在 readback_mask 里，而且手上要有一份不算太旧的 counts
 * （先 tele_on 或 motor_query 一次）。应答延后，由 host_goto_service 补发。 */
/* ---- 转盘角度模式 --------------------------------------------------------
 * motor_angle_zero <addr>
 *     把当前位置记为 0°（人工把 1 号盒对准 P0 后发一次），掉电失效。
 * motor_angle <addr> <deg100> [rpm]
 *     转到“基准 + deg100/100 度”，内部换算成闭环 goto，重复下发同一角度幂等。
 *     deg100 取 0..35999，按单圈最短路径走。
 * -------------------------------------------------------------------------- */
static void host_cmd_motor_angle_zero(uint16_t id, uint8_t nargs)
{
    uint8_t addr;
    uint8_t idx;
    HostAxisRead *r;

    if (!host_require_argc(id, nargs, 1U)) { return; }
    if (!host_arg_axis(id, host_tokens[2], &addr)) { return; }

    idx = control_index_of(addr);
    r = host_read_of(addr);
    if ((idx == 0xFFU) || (r == 0) || (r->have == 0U))
    {
        host_body_err(&host_out, id, HOST_E_NO_READBACK);
        host_kv_u32(&host_out, "addr", addr);
        host_kv_str(&host_out, "reason", "no_counts");
        return;
    }

    host_angle_base[idx]    = r->counts;
    host_angle_base_ok[idx] = 1U;

    host_body_ok(&host_out, id);
    host_kv_u32(&host_out, "addr", addr);
    host_kv_i32(&host_out, "base", host_angle_base[idx]);
}

static void host_cmd_motor_angle(uint16_t id, uint8_t nargs)
{
    uint8_t addr;
    uint8_t idx;
    uint32_t deg100;
    uint32_t rpm = 0U;
    uint32_t v;
    int64_t target;
    int32_t delta;
    int32_t cpr;
    int32_t pulses;
    int32_t tol;
    HostAxisRead *r;

    if ((nargs < 2U) || (nargs > 3U))
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "argc");
        return;
    }
    if (!host_arg_axis(id, host_tokens[2], &addr)) { return; }
    if (!host_arg_u32(id, host_tokens[3], 0U, 35999U, &deg100)) { return; }
    rpm = host_param_value("goto_rpm");
    if (nargs >= 3U)
    {
        if (!host_arg_u32(id, host_tokens[4], 1U, 6000U, &v)) { return; }
        rpm = v;
    }

    if (control_stop_latched())
    {
        host_body_err(&host_out, id, HOST_E_LATCHED);
        return;
    }
    idx = control_index_of(addr);
    r = host_read_of(addr);
    if (!host_axis_has_readback(addr) || (idx == 0xFFU) || (r == 0))
    {
        host_body_err(&host_out, id, HOST_E_NO_READBACK);
        host_kv_u32(&host_out, "addr", addr);
        return;
    }
    if (host_angle_base_ok[idx] == 0U)
    {
        host_body_err(&host_out, id, HOST_E_NO_READBACK);
        host_kv_u32(&host_out, "addr", addr);
        host_kv_str(&host_out, "reason", "no_angle_base");
        return;
    }
    if ((r->have == 0U) ||
        ((int32_t)(Stepper_Millis() - r->stamp_ms) > (int32_t)HOST_GOTO_FRESH_MS))
    {
        host_body_err(&host_out, id, HOST_E_NO_READBACK);
        host_kv_u32(&host_out, "addr", addr);
        host_kv_str(&host_out, "reason", "no_fresh_counts");
        return;
    }
    if (host_goto.active != 0U)
    {
        host_body_err(&host_out, id, HOST_E_BUSY);
        host_kv_str(&host_out, "reason", "goto_busy");
        return;
    }

    cpr    = host_goto_counts_per_rev();
    target = (int64_t)host_angle_base[idx] + ((int64_t)deg100 * (int64_t)cpr) / 36000;
    delta  = (int32_t)(target - (int64_t)r->counts);
    while (delta > (cpr / 2))  { delta -= cpr; }
    while (delta < -(cpr / 2)) { delta += cpr; }

    pulses = Emm_CountsToPulses(delta, (uint32_t)cpr, host_goto_pulses_per_rev());
    tol    = (int32_t)host_param_value("angle_tol_counts");

    if (pulses == 0)
    {
        host_body_ok(&host_out, id);
        host_kv_u32(&host_out, "addr", addr);
        host_kv_i32(&host_out, "moved", 0);
        return;
    }

    host_goto.req_id   = id;
    host_goto.pending  = 1U;
    host_defer_line    = 1U;
    host_defer_id      = id;
    host_defer_addr    = addr;
    host_defer_what    = EMM_QUERY_POS;

    host_goto_begin(addr, pulses, tol, (uint16_t)rpm);
    if (host_goto.active == 0U)
    {
        host_defer_line = 0U;
    }
}


static void host_cmd_motor_goto(uint16_t id, uint8_t nargs)
{
    uint8_t       addr;
    int32_t       pulses;
    int32_t       tol;
    uint32_t      rpm;
    uint32_t      v;
    HostAxisRead *r;

    /* tol / rpm 都是可选的：2~4 个参数都合法 */
    if ((nargs < 2U) || (nargs > 4U))
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "argc");
        return;
    }
    if (!host_arg_axis(id, host_tokens[2], &addr)) { return; }
    if (!host_parse_i32(host_tokens[3], &pulses))
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "pulses");
        return;
    }
    if ((pulses == 0) || (pulses > (100L * 3200L)) || (pulses < -(100L * 3200L)))
    {
        host_body_err(&host_out, id, HOST_E_ARG);
        host_kv_str(&host_out, "reason", "pulses");
        return;
    }

    tol = (int32_t)host_param_value("goto_tol_counts");
    if (nargs >= 3U)
    {
        if (!host_arg_u32(id, host_tokens[4], 1U, 65535U, &v)) { return; }
        tol = (int32_t)v;
    }
    rpm = host_param_value("goto_rpm");
    if (nargs >= 4U)
    {
        if (!host_arg_u32(id, host_tokens[5], 1U, 6000U, &v)) { return; }
        rpm = v;
    }

    if (control_stop_latched())
    {
        host_body_err(&host_out, id, HOST_E_LATCHED);
        return;
    }
    if (!host_axis_has_readback(addr))
    {
        host_body_err(&host_out, id, HOST_E_NO_READBACK);
        host_kv_u32(&host_out, "addr", addr);
        return;
    }
    r = host_read_of(addr);
    if (r == 0)
    {
        host_body_err(&host_out, id, HOST_E_NO_READBACK);
        host_kv_u32(&host_out, "addr", addr);
        host_kv_str(&host_out, "reason", "no_axis");
        return;
    }
    /* 起点 counts 可以是旧的：host_goto_begin 会先进 PRIME，等第一份新鲜样本再动 */
    if (host_goto.active != 0U)
    {
        host_body_err(&host_out, id, HOST_E_BUSY);
        host_kv_str(&host_out, "reason", "goto_busy");
        return;
    }

    /* 应答先压住（defer）：host_goto_service 收口时再补发。
     * 注意这里不复用 host_query_pending——那会让 sweep 停摆，goto 自己就没有
     * 新 counts 可用了；goto 用 host_goto.pending 单独记账。 */
    host_goto.req_id   = id;
    host_goto.pending  = 1U;
    host_defer_line    = 1U;
    host_defer_id      = id;
    host_defer_addr    = addr;
    host_defer_what    = EMM_QUERY_POS;

    host_goto_begin(addr, pulses, tol, (uint16_t)rpm);
    if (host_goto.active == 0U)
    {
        host_defer_line = 0U;   /* begin 里已经补发了失败应答 */
    }
}

static void host_cmd_gripper(uint16_t id, uint8_t nargs)
{
    uint32_t open_deg;
    uint32_t grab_deg;
    uint32_t ready_deg;
    uint32_t step_deg;
    uint32_t settle;
    int32_t  target;

    if (!host_require_argc(id, nargs, 1U)) { return; }

    open_deg = host_param_value("gripper_open_deg");
    grab_deg = host_param_value("gripper_grab_deg");
    ready_deg = host_param_value("gripper_ready_deg");
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
    else if (host_streq(host_tokens[2], "ready"))
    {
        target = (int32_t)ready_deg;
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

    control_gripper_set_hold_max((uint32_t)host_param_value("gripper_hold_max_ms"));
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

    control_gripper_set_hold_max((uint32_t)host_param_value("gripper_hold_max_ms"));
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
    if ((value < 0) || ((uint32_t)value < p->min) || ((uint32_t)value > p->max))
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
    p->value = (uint32_t)value;
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

    host_goto_abort("stopped");
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
    else if (host_streq(cmd, "motor_goto"))  { host_cmd_motor_goto(id, nargs); }
    else if (host_streq(cmd, "motor_angle_zero")) { host_cmd_motor_angle_zero(id, nargs); }
    else if (host_streq(cmd, "motor_angle"))      { host_cmd_motor_angle(id, nargs); }
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

    /* 同 id 同内容：只重发原应答，不重复执行（相对位移尤其重要）。
     * 例外：hello 是"开会话"命令。链路复位/重新握手时客户端会复用同一个 id，
     * 若被上一次会话的缓存挡住就会误回 E12，所以 hello 一律不看旧缓存。 */
    if ((ntok >= 2U) && host_streq(host_tokens[1], "hello"))
    {
        dup = 0;
    }
    else
    {
        dup = host_session_find(id);
    }
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
    if ((host_query_pending == 0U) && (host_goto.active == 0U))
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
    host_params_restore_defaults();
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

    /* 4.6 闭环 goto：推进状态机。
     *     它要的 counts 由 sweep 刷进 host_axis_read，所以下面第 7 步在
     *     goto 激活时会绕过遥测周期，每拍都试着开一次查询。 */
    host_goto_service(now);

    /* 5. 停止字符：置锁存 + 错发四轴停止 + 报事件 */
    if (host_stop_pending)
    {
        host_stop_pending = 0U;
        host_goto_abort("stopped");
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
            host_goto_abort("lost");
            control_latch_stop(now);
            (void)host_emit_link_event("timeout");
        }
    }

    /* 7. 遥测：默认关，只有 tele_on 订阅后才发。
     *    goto 激活时每拍都刷 goto 那个轴（闭环要快），遥测仍按自己的周期发。 */
    if (host_ready)
    {
        uint8_t tele_due = 0U;

        if ((host_tele_axis != 0U) && (host_tele_interval_ms > 0U) &&
            ((int32_t)(now - host_tele_next_ms) >= 0))
        {
            tele_due = 1U;
        }
        if ((tele_due != 0U) || (host_goto.active != 0U))
        {
            host_query_sweep(now);
        }
        if (tele_due != 0U)
        {
            uint8_t i;

            host_emit_telemetry(host_tele_axis);
            for (i = 0U; i < CONTROL_AXIS_COUNT; i++)
            {
                uint8_t a = control_addr_table[i];

                if ((a != host_tele_axis) && (host_axis_has_readback(a)))
                {
                    host_emit_telemetry(a);
                }
            }
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
