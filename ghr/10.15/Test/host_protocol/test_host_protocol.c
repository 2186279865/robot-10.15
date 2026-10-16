/* test_host_protocol.c - 上位机行协议的主机侧自测
 *
 * 这份测试直接编译真实的固件源码：
 *     Drive/Include/host_protocol_inline.h
 *     Drive/Include/control_inline.h
 * 只把硬件调用（Stepper_* / Servo_SetAngle / 毫秒时基）与 USB 传输层
 * （host_transport.h 里的 HostLink_*）换成桩，因此在 PC 上即可逐字节验证：
 *   分帧、校验、超长恢复、请求去重、错误码、锁存、心跳失联、遥测周期。
 *
 * 注意：本文件的字符串字面量必须保持纯 ASCII。
 * ARMCC V5 按本机区域设置（GBK）读源码，UTF-8 中文字符串会报
 * "missing closing quote"；注释里的中文没问题（与 stepper_inline.h 一致）。
 *
 * 编译运行：见同目录 run_host_tests.cmd
 * 跑完后把固件真实发出的每一帧写到 frames.txt，再用 check_frames.py
 * 交给上位机自己的 protocol.py 解析一遍，做跨实现交叉验证。
 */

#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdlib.h>

#define HOST_PROTOCOL_HOSTTEST 1
#include "host_protocol_inline.h"

/* ==========================================================================
 * 硬件桩：调用计数 + 最近一次参数
 * ========================================================================== */

static uint32_t fake_ms = 0;

static int c_enable = 0, c_stop = 0, c_stall = 0, c_zero = 0;
static int c_speed = 0, c_pos = 0, c_servo = 0;

static uint8_t  last_enable_addr = 0, last_enable_on = 0;
static uint8_t  last_speed_addr = 0, last_speed_dir = 0, last_speed_acc = 0;
static uint16_t last_speed_rpm = 0;
static uint8_t  last_pos_addr = 0, last_pos_dir = 0, last_pos_acc = 0;
static int32_t  last_pos_pulses = 0;
static uint32_t last_pos_wait = 0xFFFFFFFFUL;
static uint16_t last_servo_deg = 0xFFFFU;

uint32_t HostTest_Millis(void)
{
    return fake_ms;
}

uint8_t HostTest_EnableAt(uint8_t addr, uint8_t on)
{
    c_enable++;
    last_enable_addr = addr;
    last_enable_on = on;
    return 1U;
}

uint8_t HostTest_ZeroHereAt(uint8_t addr)
{
    (void)addr;
    c_zero++;
    return 1U;
}

uint8_t HostTest_ResetStallAt(uint8_t addr)
{
    (void)addr;
    c_stall++;
    return 1U;
}

uint8_t HostTest_MoveRelativeAt2(uint8_t addr, uint8_t dir, int32_t pulses,
                                 uint16_t rpm, uint8_t accel, uint32_t wait_ms)
{
    c_pos++;
    last_pos_addr = addr;
    last_pos_dir = dir;
    last_pos_pulses = pulses;
    last_pos_acc = accel;
    last_pos_wait = wait_ms;
    (void)rpm;
    return 1U;
}

uint8_t HostTest_RunSpeedAt2(uint8_t addr, uint8_t dir, uint16_t rpm, uint8_t accel)
{
    c_speed++;
    last_speed_addr = addr;
    last_speed_dir = dir;
    last_speed_rpm = rpm;
    last_speed_acc = accel;
    return 1U;
}

uint8_t HostTest_StopAt(uint8_t addr)
{
    (void)addr;
    c_stop++;
    return 1U;
}

void HostTest_ServoSetAngle(uint16_t deg)
{
    c_servo++;
    last_servo_deg = deg;
}

/* ==========================================================================
 * 传输层桩
 * ========================================================================== */

static uint8_t  rxq[4096];
static uint16_t rxq_head = 0, rxq_tail = 0;
static uint8_t  stop_req = 0;
static uint8_t  rxerr_flags = 0;

static char   txlog[16384];
static size_t txlog_len = 0;

static char   all_frames[32768];
static size_t all_frames_len = 0;

void HostLink_Init(void)
{
    rxq_head = 0; rxq_tail = 0;
    txlog_len = 0;
}

void HostLink_Poll(void)
{
}

uint8_t HostLink_IsConfigured(void)
{
    return 1U;
}

uint8_t HostLink_ReadByte(uint8_t *byte)
{
    if (rxq_head >= rxq_tail)
    {
        return 0U;
    }
    *byte = rxq[rxq_head];
    rxq_head++;
    return 1U;
}

uint8_t HostLink_QueueTx(const uint8_t *data, uint16_t len)
{
    if ((txlog_len + len) < sizeof(txlog))
    {
        memcpy(txlog + txlog_len, data, len);
        txlog_len += len;
    }
    if ((all_frames_len + len) < sizeof(all_frames))
    {
        memcpy(all_frames + all_frames_len, data, len);
        all_frames_len += len;
    }
    return HOST_LINK_OK;
}

uint8_t HostLink_TakeRxError(void)
{
    uint8_t f = rxerr_flags;
    rxerr_flags = 0U;
    return f;
}

uint8_t HostLink_TakeStopRequest(void)
{
    uint8_t s = stop_req;
    stop_req = 0U;
    return s;
}

/* ==========================================================================
 * 测试脚手架
 * ========================================================================== */

static int checks = 0;
static int failures = 0;
static const char *cur_test = "";

/* 应答帧检查用的游标 */
static size_t txoff = 0;
static char   cur_frame[256];

static void check(int cond, const char *what)
{
    checks++;
    if (!cond)
    {
        failures++;
        printf("  FAIL [%s] %s\n", cur_test, what);
    }
}

static void reset_world(void)
{
    fake_ms = 0;
    c_enable = c_stop = c_stall = c_zero = c_speed = c_pos = c_servo = 0;
    last_pos_wait = 0xFFFFFFFFUL;
    last_servo_deg = 0xFFFFU;
    rxq_head = rxq_tail = 0;
    stop_req = 0;
    rxerr_flags = 0;
    txlog_len = 0;
    txoff = 0;
    host_protocol_init();
}

static void feed(const char *s)
{
    while (*s != '\0')
    {
        rxq[rxq_tail] = (uint8_t)(*s);
        rxq_tail++;
        s++;
    }
}

static void pump(void)
{
    host_protocol_task();
}

/* 主循环每次最多处理 64 个接收字节（见 HOST_READ_BYTES_PER_TASK）。
 * 一整帧最长 96 字节，所以测试要像真主循环那样反复跑，直到收队列排空。 */
static void pump_until_drained(void)
{
    uint8_t guard = 0U;

    do
    {
        pump();
    } while ((rxq_head < rxq_tail) && (guard++ < 32U));
}

/* 造一条合法请求：payload 不含 '$' / '*' / 换行 */
static void mkreq(char *out, const char *payload)
{
    uint8_t cs = 0U;
    size_t  i, n = strlen(payload);

    for (i = 0U; i < n; i++)
    {
        cs = (uint8_t)(cs ^ (uint8_t)payload[i]);
    }
    sprintf(out, "$%s*%02X\n", payload, cs);
}

static void send(const char *payload)
{
    char line[256];

    mkreq(line, payload);
    feed(line);
    pump_until_drained();
}

static void send_raw(const char *raw)
{
    feed(raw);
    pump_until_drained();
}

/* ---- 应答帧检查 ---- */

static int pull_frame(void)
{
    size_t  start, n, i;
    uint8_t cs = 0U;
    char    want[4];

    start = txoff;
    while ((start < txlog_len) && (txlog[start] != '\n'))
    {
        start++;
    }
    if (start >= txlog_len)
    {
        return 0;
    }
    n = start - txoff;
    if ((n + 1U) > HOST_MAX_FRAME_BYTES)
    {
        failures++;
        printf("  FAIL [%s] frame longer than 96 bytes: %u\n", cur_test, (unsigned)(n + 1U));
        txoff = start + 1U;
        return 0;
    }
    memcpy(cur_frame, txlog + txoff, n);
    cur_frame[n] = '\0';
    txoff = start + 1U;

    if ((n < 4U) || (cur_frame[n - 3U] != '*'))
    {
        failures++;
        printf("  FAIL [%s] frame without checksum: %s\n", cur_test, cur_frame);
        return 0;
    }
    for (i = 1U; i < (n - 3U); i++)
    {
        cs = (uint8_t)(cs ^ (uint8_t)cur_frame[i]);
    }
    sprintf(want, "%02X", cs);
    if ((cur_frame[n - 2U] != want[0]) || (cur_frame[n - 1U] != want[1]))
    {
        failures++;
        printf("  FAIL [%s] checksum mismatch: %s (want %s)\n", cur_test, cur_frame, want);
        return 0;
    }
    return 1;
}

/* 取出下一帧并检查前缀 */
static void expect(const char *head)
{
    char msg[256];

    if (!pull_frame())
    {
        failures++;
        printf("  FAIL [%s] no frame available\n", cur_test);
        return;
    }
    if (strncmp(cur_frame, head, strlen(head)) != 0)
    {
        sprintf(msg, "head mismatch: got %s want %s", cur_frame, head);
        check(0, msg);
        return;
    }
    checks++;
}

static void expect_contains(const char *sub)
{
    char msg[256];

    if (strstr(cur_frame, sub) == NULL)
    {
        sprintf(msg, "missing %s in frame %s", sub, cur_frame);
        check(0, msg);
        return;
    }
    checks++;
}

static void expect_none(void)
{
    if (txoff < txlog_len)
    {
        char msg[300];
        sprintf(msg, "unexpected frame: %.240s", txlog + txoff);
        check(0, msg);
        return;
    }
    checks++;
}

static void do_hello(void)
{
    send("1,hello,1,12345");
    expect("#1,ok,");
    expect_contains("fw=");
    expect_contains("proto=1");
    expect_contains("axes=1/3/4/5");
    expect_contains("readback=-");
    check(strstr(cur_frame, "boot=") != NULL, "hello must carry boot=");
}

/* ==========================================================================
 * 用例
 * ========================================================================== */

static void test_hello_handshake(void)
{
    cur_test = "hello";
    reset_world();
    check(host_protocol_is_ready() == 0U, "not ready before handshake");
    do_hello();
    check(host_protocol_is_ready() == 1U, "ready after handshake");
    /* 版本不符要被拒 */
    send("2,hello,2,1");
    expect("#2,err,E02");
    expect_contains("want=1");
}

static void test_ping_and_status(void)
{
    cur_test = "ping/status";
    reset_world();
    do_hello();
    fake_ms = 1234U;
    send("3,ping");
    expect("#3,ok,");
    expect_contains("ms=1234");
    send("4,status");
    expect("#4,ok,");
    expect_contains("latch=0");
    expect_contains("owner=pc");
    expect_contains("running=-");
    expect_contains("queued=0");
}

static void test_doc_example_frame(void)
{
    /* 方案文档里逐字节验证过的例子：
     *   $12,motor_speed,1,0,60,1*4A\n  ->  #12,ok,addr=1,rpm=60*60\n
     * 请求用文档原文（含校验和 4A），应答允许带额外的 task= 键。 */
    cur_test = "doc-example";
    reset_world();
    do_hello();
    send_raw("$12,motor_speed,1,0,60,1*4A\n");
    expect("#12,ok,");
    expect_contains("addr=1");
    expect_contains("rpm=60");
    check(c_speed == 1, "exactly one speed command sent");
    check(last_speed_addr == 1U, "addr should be 1");
    check(last_speed_dir == 0U, "dir should be positive");
    check(last_speed_rpm == 60U, "rpm should be 60");
    check(last_speed_acc == 1U, "accel should be 1");
}

static void test_duplicate_and_conflict(void)
{
    char before[128];

    cur_test = "dedupe";
    reset_world();
    do_hello();
    send("12,motor_speed,1,0,60,1");
    expect("#12,ok,");
    check(c_speed == 1, "first request executes");
    strcpy(before, cur_frame);

    /* 同 id 同内容：只重发，不重复执行 */
    txoff = 0;
    txlog_len = 0;
    send("12,motor_speed,1,0,60,1");
    check(c_speed == 1, "duplicate must not execute again");
    pull_frame();
    check(strcmp(before, cur_frame) == 0, "duplicate reply must be byte identical");

    /* 同 id 不同内容：E12 */
    txoff = 0;
    txlog_len = 0;
    send("12,motor_speed,1,0,120,1");
    expect("#12,err,E12");
    check(c_speed == 1, "conflicting request must not execute");
}

static void test_checksum_unknown_axis(void)
{
    cur_test = "errors";
    reset_world();
    do_hello();

    txoff = 0; txlog_len = 0;
    send_raw("$5,ping*00\n");                 /* 校验和故意错 */
    expect("#5,err,E07");

    txoff = 0; txlog_len = 0;
    send("6,no_such_cmd");
    expect("#6,err,E01");

    txoff = 0; txlog_len = 0;
    send("7,motor_speed,2,0,60,1");           /* 2 不是合法轴 */
    expect("#7,err,E03");

    txoff = 0; txlog_len = 0;
    send("8,motor_speed,1,0,60");             /* 参数个数不够 */
    expect("#8,err,E02");
    expect_contains("reason=argc");

    txoff = 0; txlog_len = 0;
    send("9,motor_speed,1,0,1000,1");         /* 超过 speed_rpm_max=300 */
    expect("#9,err,E02");
    expect_contains("reason=speed_limit");

    /* 坏校验的帧不得毒化会话缓存：同一个 id 修好后必须能正常执行 */
    txoff = 0; txlog_len = 0;
    send("10,ping");
    expect("#10,ok,");
}

static void test_motor_enable(void)
{
    cur_test = "enable";
    reset_world();
    do_hello();

    send("110,motor_enable,1,1");
    expect("#110,ok,");
    expect_contains("addr=1");
    expect_contains("enabled=1");
    check(c_enable == 1, "one enable frame");
    check(last_enable_addr == 1U, "enable addr should be 1");
    check(last_enable_on == 1U, "enable state should be on");

    send("111,motor_enable,1,0");
    expect("#111,ok,");
    check(c_enable == 2, "disable frame sent");
    check(last_enable_on == 0U, "disable state should be off");

    txoff = 0; txlog_len = 0;
    send("112,motor_enable,2,1");
    expect("#112,err,E03");
}

static void test_position_zero_query(void)
{
    cur_test = "position";
    reset_world();
    do_hello();

    send("20,motor_pos,4,1,120,1,3200,rel");
    expect("#20,ok,");
    expect_contains("pulses=3200");
    expect_contains("mode=rel");
    check(c_pos == 1, "one position command");
    check(last_pos_addr == 4U, "addr should be 4");
    check(last_pos_dir == 1U, "dir should be negative here");
    check(last_pos_acc == 1U, "accel should be 1");
    check(last_pos_pulses == 3200, "pulse count");
    check(last_pos_wait == 0UL, "position command must be non blocking (wait_ms=0)");

    /* 位置基准无效 → E13 */
    txoff = 0; txlog_len = 0;
    send("21,motor_pos,4,1,120,1,100,abs");
    expect("#21,err,E13");

    /* 运动未停下时不许清零（E09）；先停再清 */
    txoff = 0; txlog_len = 0;
    send("215,motor_zero,4");
    expect("#215,err,E09");
    expect_contains("reason=moving");

    send("2151,motor_stop,4");
    expect("#2151,ok,");

    /* 清零后 abs 可用 */
    txoff = 0; txlog_len = 0;
    send("22,motor_zero,4");
    expect("#22,ok,");
    check(c_zero == 1, "zero command sent");
    txoff = 0; txlog_len = 0;
    send("23,motor_pos,4,1,120,1,100,abs");
    expect("#23,ok,");
    expect_contains("mode=abs");

    /* 没有回读 → 查询必须如实回 E11，不许编造 */
    txoff = 0; txlog_len = 0;
    send("24,motor_query,4,pos");
    expect("#24,err,E11");

    txoff = 0; txlog_len = 0;
    send("25,motor_stall_clear,4");
    expect("#25,ok,");
    check(c_stall == 1, "stall clear sent");
}

static void test_stop_all_and_clear(void)
{
    uint8_t i;

    cur_test = "latch";
    reset_world();
    do_hello();

    send("30,stop_all");
    expect("#30,ok,");
    expect_contains("axes=1/3/4/5");
    expect("#0,event,latch,");
    expect_contains("state=set");

    /* 锁存后用运动命令被拒 */
    txoff = 0; txlog_len = 0;
    send("31,motor_speed,1,0,60,1");
    expect("#31,err,E10");

    /* 补发四轴停止：每帧间隔 frame_gap_ms=10ms */
    for (i = 0U; i < 6U; i++)
    {
        fake_ms += 10U;
        pump();
    }
    check(c_stop == 4, "four stop frames after latch");

    txoff = 0; txlog_len = 0;
    send("32,stop_clear");
    expect("#32,ok,");
    expect_contains("latch=0");
    txoff = 0; txlog_len = 0;
    send("33,motor_speed,1,0,60,1");
    expect("#33,ok,");
}

static void test_stop_char(void)
{
    cur_test = "stop-char";
    reset_world();
    do_hello();
    send("40,motor_speed,1,0,60,1");
    expect("#40,ok,");

    /* 传输层锁存的 '!' */
    txoff = 0; txlog_len = 0;
    stop_req = 1U;
    pump();
    expect("#0,event,latch,");
    expect_contains("reason=stop_char");
    check(control_stop_latched() == 1U, "stop char must latch");

    /* 直接出现在字节流里的 '!' 也要生效 */
    reset_world();
    do_hello();
    txoff = 0; txlog_len = 0;
    send_raw("!");
    pump();
    expect("#0,event,latch,");
    check(control_stop_latched() == 1U, "'!' inside the byte stream must latch too");
}

static void test_framing_oversize(void)
{
    char payload[128];
    int  i;

    cur_test = "framing";
    reset_world();
    do_hello();

    /* 半条帧后面接一条完整帧：'$' 必须能把残帧丢掉重新开始 */
    txoff = 0; txlog_len = 0;
    feed("$50,ping");
    pump();
    send("51,ping");
    expect("#51,ok,");
    expect_none();

    /* 超长帧：payload 98 字符 → E06，且计数器 +1 */
    reset_world();
    do_hello();
    txoff = 0; txlog_len = 0;
    payload[0] = '6';
    payload[1] = '0';
    payload[2] = ',';
    for (i = 3; i < 98; i++)
    {
        payload[i] = 'x';
    }
    payload[98] = '\0';
    send(payload);
    expect("#60,err,E06");
    check(host_protocol_overflow_count() == 1U, "overflow counter should be 1");

    /* 刚好 91 字符 payload 的帧是允许的（整帧 96 字节） */
    reset_world();
    do_hello();
    txoff = 0; txlog_len = 0;
    {
        char big[256];
        char pl[128];
        size_t n = 0;
        pl[n++] = '6'; pl[n++] = '1'; pl[n++] = ',';
        while (n < 91U)
        {
            pl[n++] = 'y';
        }
        pl[91] = '\0';
        mkreq(big, pl);
        check(strlen(big) == 96U, "max frame must be exactly 96 bytes");
        send_raw(big);
    }
    expect("#61,err,E01");          /* 命令名超长 → 未知命令，但帧被完整处理 */
}

static void test_params(void)
{
    cur_test = "params";
    reset_world();
    do_hello();

    send("70,param_get,speed_rpm");
    expect("#70,ok,");
    expect_contains("name=speed_rpm");
    expect_contains("value=60");

    send("71,param_set,acc,2");
    expect("#71,ok,");
    expect_contains("before=1");
    expect_contains("value=2");

    txoff = 0; txlog_len = 0;
    send("72,param_set,no_such,1");
    expect("#72,err,E02");
    expect_contains("reason=unknown_param");

    txoff = 0; txlog_len = 0;
    send("73,param_set,speed_rpm,99999");
    expect("#73,err,E02");
    expect_contains("reason=range");

    txoff = 0; txlog_len = 0;
    send("74,param_set,speed_rpm_max,0");
    expect("#74,err,E02");
}

static void test_gripper(void)
{
    cur_test = "gripper";
    reset_world();
    do_hello();

    send("80,gripper,grab");
    expect("#80,ok,");
    expect_contains("angle=0");
    check(last_servo_deg == 0U, "grab should drive 0 deg");

    send("81,gripper,step_release");
    expect("#81,ok,");
    expect_contains("angle=45");
    check(last_servo_deg == 45U, "step release should drive 45 deg");

    send("82,gripper,open");
    expect("#82,ok,");
    expect_contains("angle=180");

    send("83,gripper,step_grab");
    expect("#83,ok,");
    expect_contains("angle=135");

    txoff = 0; txlog_len = 0;
    send("84,servo_angle,200");
    expect("#84,err,E02");
    expect_contains("reason=out_of_range");

    txoff = 0; txlog_len = 0;
    send("85,servo_angle,90");
    expect("#85,ok,");
    check(last_servo_deg == 90U, "debug angle goes straight out");
}

static void test_telemetry_and_settle_event(void)
{
    cur_test = "telemetry";
    reset_world();
    do_hello();

    send("90,tele_on,1,200");
    expect("#90,ok,");
    expect_contains("interval_ms=200");

    txoff = 0; txlog_len = 0;
    fake_ms += 199U;
    pump();
    expect_none();

    fake_ms += 1U;
    pump();
    expect("#0,tele,");
    expect_contains("addr=1");
    expect_contains("readback=0");

    /* 夹爪等待结束只报 settle_elapsed，不声称到位 */
    send("91,gripper,grab");
    expect("#91,ok,");
    txoff = 0; txlog_len = 0;
    fake_ms = control_gripper_ready_ms;
    pump();
    expect("#0,event,task,");
    expect_contains("state=settle_elapsed");

    txoff = 0; txlog_len = 0;
    send("92,tele_off");
    expect("#92,ok,");
    txoff = 0; txlog_len = 0;
    fake_ms += 1000U;
    pump();
    expect_none();
}

static void test_link_timeout(void)
{
    cur_test = "link-timeout";
    reset_world();
    do_hello();

    send("95,motor_speed,1,0,60,1");
    expect("#95,ok,");

    txoff = 0; txlog_len = 0;
    fake_ms += 3001U;
    pump();
    expect("#0,event,link,");
    expect_contains("state=timeout");
    check(host_protocol_is_link_lost() == 1U, "link lost flag");
    check(control_stop_latched() == 1U, "link loss must latch a stop");
    /* 失联只停车，不关使能（防止垂直机构下滑） */
    check(c_enable == 0, "link loss must not disable the axes");
}

static void test_split_frames(void)
{
    char line[256];

    cur_test = "split";
    reset_world();
    do_hello();

    /* 一帧拆成三段喂进来，仍要正确执行 */
    txoff = 0; txlog_len = 0;
    mkreq(line, "99,ping");
    {
        char part1[8], part2[8];

        part1[0] = line[0]; part1[1] = line[1]; part1[2] = '\0';
        part2[0] = line[2]; part2[1] = line[3]; part2[2] = '\0';
        feed(part1); pump();
        feed(part2); pump();
        feed(line + 4); pump();
    }
    expect("#99,ok,");

    /* 两帧粘在一个包里 */
    txoff = 0; txlog_len = 0;
    {
        char a[128], b[128], both[256];

        mkreq(a, "100,ping");
        mkreq(b, "101,ping");
        sprintf(both, "%s%s", a, b);
        send_raw(both);
    }
    expect("#100,ok,");
    expect("#101,ok,");

    /* 帧外的垃圾字节要被丢掉 */
    txoff = 0; txlog_len = 0;
    {
        char a[128], both[256];

        mkreq(a, "102,ping");
        sprintf(both, "garbage\n\n%s", a);
        send_raw(both);
    }
    expect("#102,ok,");
}

int main(void)
{
    printf("host protocol tests\n");

    test_hello_handshake();
    test_ping_and_status();
    test_doc_example_frame();
    test_duplicate_and_conflict();
    test_checksum_unknown_axis();
    test_position_zero_query();
    test_stop_all_and_clear();
    test_stop_char();
    test_framing_oversize();
    test_motor_enable();
    test_params();
    test_gripper();
    test_telemetry_and_settle_event();
    test_link_timeout();
    test_split_frames();

    {
        FILE *f = fopen("frames.txt", "wb");
        if (f != NULL)
        {
            fwrite(all_frames, 1U, all_frames_len, f);
            fclose(f);
            printf("wrote frames.txt (%u bytes) for check_frames.py\n",
                   (unsigned)all_frames_len);
        }
    }

    printf("\nchecks %d, failures %d\n", checks, failures);
    return (failures == 0) ? 0 : 1;
}
