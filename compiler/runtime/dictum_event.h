#ifndef DICTUM_EVENT_H
#define DICTUM_EVENT_H
/* dictum_event.h — Dictum stdlib stub (Sprint 3). */
/* Functions marked TODO are not yet implemented; linking may fail. */
#include "dictum_core.h"
#include <stddef.h>

/* TODO */ static inline void* dictum_event_create(void) {
    return (void*)0; /* not yet implemented */
}

/* TODO */ static inline void dictum_event_emit(void *e, dictum_text name, void *data) {
    (void)0; /* not yet implemented */
}

/* TODO */ static inline void dictum_event_on(void *e, dictum_text name, void(*cb)(void*)) {
    (void)0; /* not yet implemented */
}

/* TODO */ static inline void dictum_event_off(void *e, dictum_text name) {
    (void)0; /* not yet implemented */
}

/* TODO */ static inline void dictum_event_destroy(void *e) {
    (void)0; /* not yet implemented */
}

#endif /* DICTUM_EVENT_H */
