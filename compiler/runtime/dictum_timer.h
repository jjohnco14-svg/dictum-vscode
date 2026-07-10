#ifndef DICTUM_TIMER_H
#define DICTUM_TIMER_H
/* dictum_timer.h — Dictum stdlib stub (Sprint 3). */
/* Functions marked TODO are not yet implemented; linking may fail. */
#include "dictum_core.h"
#include <stddef.h>

/* TODO */ static inline double dictum_timer_now(void) {
    return 0.0; /* not yet implemented */
}

/* TODO */ static inline void dictum_timer_sleep(double seconds) {
    (void)0; /* not yet implemented */
}

/* TODO */ static inline void* dictum_timer_create(double interval_s, void(*cb)(void*), void *arg) {
    return (void*)0; /* not yet implemented */
}

/* TODO */ static inline void dictum_timer_cancel(void *t) {
    (void)0; /* not yet implemented */
}

#endif /* DICTUM_TIMER_H */
