/* control_inline.h - 统一控制状态与非阻塞动作派发
 *
 * 由 main.c 在 stepper_inline.h / servo.c 之后包含。
 *
 * 职责：
 *   1. 每根轴保存"指令状态"：使能、方向、目标转速、加速度、运动模式、最近任务号。
 *      区分"已下发（sent）"和"驱动器已应答/实测到位"——后两者要等接上回读才有，
 *      没有回读时绝不允许对外声称到位。
 *   2. 所有下发都是非阻塞的：位置指令用 wait_ms=0（驱动器自己走完），
 *      停止用 Stepper_Millis() 时基按帧间隔错开发送，主循环永不被 Delay_ms 卡住。
 *   3. 停止锁存：一旦置位，运动类命令一律拒绝（E10），直到 stop_clear。
 *      锁存只停车、不关 Z 轴使能，避免垂直机构失去保持力矩下滑。
 *   4. 夹爪：主机路径由本文件直接驱动 Servo_SetAngle() 并自己记账，
 *      等待时间用毫秒时基非阻塞计时。
 *      gripper_inline.h 那套阻塞接口（每次动作 Delay 200ms）仍留给 PA0/诊断路径，
 *      步骤 2 会把两者合并成唯一的夹爪角度接口。
 */

#ifndef CONTROL_INLINE_H
#define CONTROL_INLINE_H

#ifdef HOST_PROTOCOL_HOSTTEST
/* 主机侧自测：用桩函数顶替真实硬件调用，见 Test/host_protocol/host_test_stubs.h */
#include "host_test_stubs.h"
#else
#include "stepper.h"
#include "servo.h"
#endif

#define CONTROL_AXIS_COUNT   4U

/* 运动模式 */
#define CONTROL_MODE_STOP    0U
#define CONTROL_MODE_SPEED   1U
#define CONTROL_MODE_POS     2U

typedef struct
{
    uint8_t  addr;          /* 轴地址：1=Z 3=托盘 4=X 5=Y */
    uint8_t  enabled;       /* 最近一次下发的使能状态 */
    uint8_t  dir;           /* 0=正 1=负 */
    uint8_t  acc;           /* 加速度档位 */
    uint16_t rpm;           /* 目标转速，0 表示停 */
    uint8_t  mode;          /* CONTROL_MODE_* */
    uint8_t  pos_valid;     /* 位置基准是否有效；没有回读/没有清零前恒为 0 */
    uint32_t task;          /* 最近一次运动任务号，0 表示无 */
} ControlAxis;

/* 轴地址表：与 stepper.h 的 STEPPER_ADDR_* 保持一致 */
static const uint8_t control_addr_table[CONTROL_AXIS_COUNT] = { 1U, 3U, 4U, 5U };

static ControlAxis control_axis[CONTROL_AXIS_COUNT];

/* 停止锁存与错开发送 */
static uint8_t  control_latch = 0U;
static uint8_t  control_stop_index = 0U;
static uint8_t  control_stop_active = 0U;
static uint32_t control_stop_next_ms = 0U;

static uint32_t control_task_seq = 0U;

/* 夹爪 */
static uint16_t control_gripper_angle = 180U;   /* 上电停在松开位 */
static uint8_t  control_gripper_busy = 0U;
static uint32_t control_gripper_ready_ms = 0U;
static uint8_t  control_gripper_settled = 0U;   /* 等待刚结束，供上层报一次事件 */

/* ---------------- 轴查表 ---------------- */

static uint8_t control_index_of(uint8_t addr)
{
    uint8_t i;

    for (i = 0U; i < CONTROL_AXIS_COUNT; i++)
    {
        if (control_addr_table[i] == addr)
        {
            return i;
        }
    }
    return 0xFFU;
}

static ControlAxis *control_get(uint8_t addr)
{
    uint8_t i = control_index_of(addr);

    return (i == 0xFFU) ? 0 : &control_axis[i];
}

/* ---------------- 生命周期 ---------------- */

static void control_reset_state(void)
{
    uint8_t i;

    for (i = 0U; i < CONTROL_AXIS_COUNT; i++)
    {
        control_axis[i].addr     = control_addr_table[i];
        control_axis[i].enabled  = 0U;
        control_axis[i].dir      = 0U;
        control_axis[i].acc      = 1U;
        control_axis[i].rpm      = 0U;
        control_axis[i].mode     = CONTROL_MODE_STOP;
        control_axis[i].pos_valid = 0U;
        control_axis[i].task     = 0U;
    }

    control_latch          = 0U;
    control_stop_index     = 0U;
    control_stop_active    = 0U;
    control_stop_next_ms   = 0U;
    control_task_seq       = 0U;
    control_gripper_angle  = 180U;
    control_gripper_busy   = 0U;
    control_gripper_ready_ms = 0U;
    control_gripper_settled  = 0U;
}

/* 夹爪硬件初始化：必须在上电时调用一次，否则 PB6 根本没有 PWM 输出，
 * 上位机怎么点都不会动。PWM 直接从松开位起振，避免上电瞬间夹一下。 */
static void control_gripper_init(void)
{
    Servo_InitAt(control_gripper_angle);        /* 180 = 松开位 */
    control_gripper_busy     = 0U;
    control_gripper_settled  = 0U;
    control_gripper_ready_ms = 0U;
}

static uint32_t control_next_task_id(void)
{
    control_task_seq++;
    if (control_task_seq == 0U)
    {
        control_task_seq = 1U;      /* 0 保留给广播/事件 */
    }
    return control_task_seq;
}

/* ---------------- 停止锁存 ---------------- */

static void control_latch_stop(uint32_t now_ms)
{
    uint8_t i;

    control_latch = 1U;
    for (i = 0U; i < CONTROL_AXIS_COUNT; i++)
    {
        control_axis[i].rpm  = 0U;
        control_axis[i].mode = CONTROL_MODE_STOP;
        control_axis[i].task = 0U;
    }

    /* 依次补发停止：每次 Control_Service 只发一帧，帧间由 gap 控制 */
    control_stop_index   = 0U;
    control_stop_active  = 1U;
    control_stop_next_ms = now_ms;
}

static uint8_t control_stop_latched(void)
{
    return control_latch;
}

static void control_clear_stop(void)
{
    control_latch = 0U;
}

/* ---------------- 动作下发（全部非阻塞） ---------------- */

static uint8_t control_apply_enable(uint8_t addr, uint8_t on)
{
    ControlAxis *ax = control_get(addr);

    if (ax == 0)
    {
        return 0U;
    }
    if (!Stepper_EnableAt(addr, on))
    {
        return 0U;
    }
    ax->enabled = on ? 1U : 0U;
    if (!on)
    {
        ax->rpm  = 0U;
        ax->mode = CONTROL_MODE_STOP;
    }
    return 1U;
}

static uint8_t control_apply_speed(uint8_t addr, uint8_t dir, uint16_t rpm,
                                   uint8_t acc, uint32_t *task_out)
{
    ControlAxis *ax = control_get(addr);

    if (ax == 0)
    {
        return 0U;
    }
    if (!Stepper_RunSpeedAt2(addr, dir, rpm, acc))
    {
        return 0U;
    }
    ax->enabled = 1U;               /* 驱动器要求先使能；沿用当前状态 */
    ax->dir     = dir;
    ax->rpm     = rpm;
    ax->acc     = acc;
    ax->mode    = CONTROL_MODE_SPEED;
    ax->task    = control_next_task_id();
    if (task_out != 0)
    {
        *task_out = ax->task;
    }
    return 1U;
}

static uint8_t control_apply_position(uint8_t addr, uint8_t dir, uint16_t rpm,
                                      uint8_t acc, uint32_t pulses,
                                      uint8_t absolute, uint32_t *task_out)
{
    ControlAxis *ax = control_get(addr);

    if (ax == 0)
    {
        return 0U;
    }
    if (absolute && !ax->pos_valid)
    {
        return 2U;                  /* 位置基准无效：上层报 E13 */
    }

    /* wait_ms = 0：只发一帧就走，驱动器自己走完，主循环不阻塞。
     * 也正因为不等待，这里只报告"已下发"，到位与否必须靠回读。 */
    if (!Stepper_MoveRelativeAt2(addr, dir, (int32_t)pulses, rpm, acc, 0UL))
    {
        return 0U;
    }
    ax->enabled = 1U;
    ax->dir     = dir;
    ax->rpm     = rpm;
    ax->acc     = acc;
    ax->mode    = CONTROL_MODE_POS;
    ax->task    = control_next_task_id();
    if (task_out != 0)
    {
        *task_out = ax->task;
    }
    return 1U;
}

static uint8_t control_apply_stop(uint8_t addr)
{
    ControlAxis *ax = control_get(addr);

    if (ax == 0)
    {
        return 0U;
    }
    if (!Stepper_StopAt(addr))
    {
        return 0U;
    }
    ax->rpm  = 0U;
    ax->mode = CONTROL_MODE_STOP;
    ax->task = 0U;
    return 1U;
}

static uint8_t control_apply_stall_clear(uint8_t addr)
{
    if (control_get(addr) == 0)
    {
        return 0U;
    }
    return Stepper_ResetStallAt(addr);
}

static uint8_t control_apply_zero(uint8_t addr)
{
    ControlAxis *ax = control_get(addr);

    if (ax == 0)
    {
        return 0U;
    }
    if (ax->mode != CONTROL_MODE_STOP)
    {
        return 2U;                  /* 运动中不允许清零：上层报 E09 */
    }
    if (!Stepper_ZeroHereAt(addr))
    {
        return 0U;
    }
    ax->pos_valid = 1U;             /* 清零后位置基准有效（相对零点） */
    return 1U;
}

/* ---------------- 夹爪（非阻塞） ---------------- */

static uint8_t control_gripper_apply(uint16_t deg, uint16_t settle_ms,
                                     uint32_t now_ms)
{
    Servo_SetAngle(deg);
    control_gripper_angle    = deg;
    control_gripper_busy     = 1U;
    control_gripper_settled  = 0U;
    control_gripper_ready_ms = now_ms + (uint32_t)settle_ms;
    return 1U;
}

/* 等待结束只报告"等待结束"，不声称夹到位（没有传感器） */
static uint8_t control_take_gripper_settled(void)
{
    uint8_t settled = control_gripper_settled;

    control_gripper_settled = 0U;
    return settled;
}

/* ---------------- 主循环服务 ---------------- */

/* 返回 1 表示本次真的发出了一帧停止（调用方可据此做日志） */
static uint8_t control_service(uint32_t now_ms, uint16_t gap_ms)
{
    uint8_t sent = 0U;

    if (control_stop_active)
    {
        if ((int32_t)(now_ms - control_stop_next_ms) >= 0)
        {
            if (control_stop_index < CONTROL_AXIS_COUNT)
            {
                (void)Stepper_StopAt(control_addr_table[control_stop_index]);
                sent = 1U;
                control_stop_index++;
                if (gap_ms == 0U)
                {
                    gap_ms = 1U;
                }
                control_stop_next_ms = now_ms + (uint32_t)gap_ms;
            }
            else
            {
                control_stop_active = 0U;   /* 四轴都已补发停止 */
            }
        }
    }

    if (control_gripper_busy)
    {
        if ((int32_t)(now_ms - control_gripper_ready_ms) >= 0)
        {
            control_gripper_busy    = 0U;
            control_gripper_settled = 1U;
        }
    }

    return sent;
}

#endif /* CONTROL_INLINE_H */
