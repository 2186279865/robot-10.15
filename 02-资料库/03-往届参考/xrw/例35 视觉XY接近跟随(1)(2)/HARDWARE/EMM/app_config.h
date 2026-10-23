#ifndef APP_CONFIG_H
#define APP_CONFIG_H

/* 复用例32 PA2/USART2发送方式，不改驱动协议或存储参数。 */
#define EMM_CHANNEL_COUNT 1U
#define EMM_TX_ONLY 1U /* 不等待电机反馈；PA2发送，PA3及三台驱动TX不接 */
#define EMM_BAUD 115200UL
#define PHYSICAL_ESTOP_ENABLED 0
#define X_ADDRESS 4U
#define Y_ADDRESS 5U
#define Z_ADDRESS 1U
#define TRAY_ADDRESS 3U
#define TRAY_ENABLED 1U
/* 每“托盘”一圈的电机脉冲数，含传动比；暂按16细分、1:1直驱。 */
#define TRAY_PULSES_PER_REV 3200UL
#define TRAY_SPEED_RPM 30U
#define TRAY_ACCEL 0U
#define TRAY_POSITIVE_DIRECTION 0U /* 0->300度的旋转方向，实物反向时改1 */
#define TRAY_POSITION_WAIT_MS 3000UL /* 不等待反馈，转位后固定等3秒才下降放置 */
#define TRAY_BOOT_ZERO_ENABLED 1U /* 人工对准1号位后，仅将当前角度计数清零 */
#define X_AXIS_ENABLED 1U
#define Y_AXIS_ENABLED 1U

/* ===== XY平移，单位RPM/毫秒，1000ms=1s ===== */
#define X_SPEED_RPM 34U
#define X_MOVE_MS 0UL
#define X_DIRECTION 0U
#define Y_SPEED_RPM 150U
#define Y_MOVE_MS 0UL
#define Y_DIRECTION 0U /* 已按实测反向：视觉Y正偏差使用方向00 */
#define MOTOR_ENABLE_WAIT_MS 500UL
#define VISION_TARGET_ID 0U
#define VISION_TOLERANCE_PX 8
#define VISION_TIMEOUT_MS 600UL
#define VISION_MAX_SESSION_MS 0UL
#define MOTOR_COMMAND_RETRY_MS 250UL /* 偏差持续存在时重发F6，防止首帧丢失 */
#define XY_FINAL_STOP_REPEAT 3U
#define XY_FINAL_STOP_GAP_MS 30UL
#define XY_FINAL_SETTLE_MS 500UL
#define XY_SPEED_ZERO_BEFORE_Z 1U

/* ===== Z与爪子：升降抓取方式沿用例32 ===== */
#define Z_SPEED_RPM 2300U
#define Z_PULSES_PER_REV 3200UL /* 暂按1.8度电机16细分，32细分改6400 */
#define Z_TRAVEL_TURNS 40UL /* 抓取和放置的下降、上升均为40圈 */
#define Z_TRAVEL_PULSES (Z_PULSES_PER_REV * Z_TRAVEL_TURNS)
#define Z_ACCEL 0U /* 0=沿用原来的无曲线加减速；1~255为驱动加速度档位 */
#define Z_POSITION_WAIT_MS 2500UL /* 发40圈FD后等待2.5秒，不读取到位反馈 */
#define Z_ENABLE_SETTLE_MS 500UL
#define Z_DOWN_DIRECTION 0U
#define Z_UP_DIRECTION 1U /* 当前CCW向上约定 */
#define Z_BOTTOM_WAIT_MS 500UL
#define CLAW_OPEN_DEG 0U /* 270度舵机：上电0度松开 */
#define CLAW_OPEN_WAIT_MS 1000UL
#define CLAW_GRAB_DEG 180U /* 270度舵机：180度抓紧 */
#define CLAW_GRAB_WAIT_MS 3000UL
/* 例32抓取后抬升；若只验证下降抓紧，可设0，Z停在下方保持。 */
#define LIFT_AFTER_GRAB 1U
#define RETURN_TO_START_ENABLED 1U
#define RETURN_X_SPEED_RPM X_SPEED_RPM
#define RETURN_Y_SPEED_RPM Y_SPEED_RPM
#define RETURN_MAX_AXIS_MS 60000UL /* 超出估算回程上限则故障停车，不盲目回程 */
#define RETURN_SETTLE_MS 500UL /* XY回程停车后稳定0.5秒，再下降放置 */
#define PLACE_AFTER_RETURN_ENABLED 1U
#define CLAW_RELEASE_WAIT_MS 1000UL /* 松爪0度后等待1秒再抬升 */
#define NEXT_CYCLE_GUARD_MS 500UL /* DONE后丢弃旧坐标0.5秒，下一轮重新收完整帧 */
#define X_CLEARANCE_ENABLED 1U
#define BOOT_X_CLEARANCE_ENABLED 1U /* 上电首轮追踪前执行一次相同X负向移动 */
#define X_CLEARANCE_SPEED_RPM X_SPEED_RPM
#define X_CLEARANCE_MOVE_MS 4500UL /* 放置并抬升后，X负向移动4.5秒 */
#define X_CLEARANCE_DIRECTION (1U-X_DIRECTION) /* X正方向00，因此负方向01 */
#define X_CLEARANCE_SETTLE_MS 200UL /* 短移停车后稳定，再DONE/查找下一目标 */

#if (X_AXIS_ENABLED && X_SPEED_RPM==0) || (Y_AXIS_ENABLED && Y_SPEED_RPM==0)
#error Speed_must_not_be_zero
#endif
#if (X_AXIS_ENABLED && X_DIRECTION>1) || (Y_AXIS_ENABLED && Y_DIRECTION>1) || Z_DOWN_DIRECTION>1 || Z_UP_DIRECTION>1
#error Invalid_motor_direction
#endif
#if Y_AXIS_ENABLED>1
#error Y_axis_enabled_must_be_0_or_1
#endif
#if X_AXIS_ENABLED>1
#error X_axis_enabled_must_be_0_or_1
#endif
#if Z_SPEED_RPM==0 || Z_TRAVEL_TURNS==0 || Z_PULSES_PER_REV==0 || \
    Z_TRAVEL_PULSES>2147483647UL || Z_ACCEL>255 || Z_POSITION_WAIT_MS==0
#error Invalid_Z_position_parameters
#endif
#if RETURN_TO_START_ENABLED && (!LIFT_AFTER_GRAB || RETURN_X_SPEED_RPM==0 || RETURN_Y_SPEED_RPM==0)
#error Return_requires_Z_lift_and_nonzero_XY_speed
#endif
#if PLACE_AFTER_RETURN_ENABLED && (!RETURN_TO_START_ENABLED || !LIFT_AFTER_GRAB)
#error Place_requires_return_and_Z_lift
#endif
#if X_CLEARANCE_ENABLED && (!PLACE_AFTER_RETURN_ENABLED || !RETURN_TO_START_ENABLED || \
    X_CLEARANCE_SPEED_RPM==0 || X_CLEARANCE_MOVE_MS==0 || X_CLEARANCE_MOVE_MS>5000UL)
#error Invalid_X_clearance_parameters
#endif
#if BOOT_X_CLEARANCE_ENABLED && !X_CLEARANCE_ENABLED
#error Boot_X_clearance_requires_X_clearance
#endif
#if TRAY_ENABLED && (!PLACE_AFTER_RETURN_ENABLED || TRAY_SPEED_RPM==0 || \
    TRAY_PULSES_PER_REV==0 || TRAY_PULSES_PER_REV>2147483647UL || \
    TRAY_ACCEL>255 || TRAY_POSITIVE_DIRECTION>1 || TRAY_POSITION_WAIT_MS==0)
#error Invalid_tray_parameters
#endif
#if TRAY_ADDRESS==X_ADDRESS || TRAY_ADDRESS==Y_ADDRESS || TRAY_ADDRESS==Z_ADDRESS
#error Tray_address_must_be_unique
#endif
#endif
