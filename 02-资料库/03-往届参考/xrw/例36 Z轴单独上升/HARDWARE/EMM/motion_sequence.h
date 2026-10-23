#ifndef MOTION_SEQUENCE_H
#define MOTION_SEQUENCE_H
#include <stdint.h>
typedef enum {
    SEQUENCE_WAIT=0,SEQUENCE_XY,SEQUENCE_Z_DOWN,SEQUENCE_GRAB,
    SEQUENCE_Z_UP,SEQUENCE_DONE,SEQUENCE_FAULT,SEQUENCE_ABORT
} SequenceState;
extern volatile uint8_t sequence_state;
uint8_t MotionSequence_Init(void);
uint8_t MotionSequence_RunOnce(void);
void MotionSequence_Task(uint8_t key_pressed);
#endif
