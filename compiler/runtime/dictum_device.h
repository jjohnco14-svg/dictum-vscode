#ifndef DICTUM_DEVICE_H
#define DICTUM_DEVICE_H
/* dictum_device.h — Dictum stdlib stub (Sprint 3). */
/* Functions marked TODO are not yet implemented; linking may fail. */
#include "dictum_core.h"
#include <stddef.h>

/* TODO */ static inline void* dictum_device_open(dictum_text path) {
    return (void*)0; /* not yet implemented */
}

/* TODO */ static inline void dictum_device_close(void *d) {
    (void)0; /* not yet implemented */
}

/* TODO */ static inline int dictum_device_write(void *d, void *buf, int n) {
    return -1; /* not yet implemented */
}

/* TODO */ static inline int dictum_device_read(void *d, void *buf, int n) {
    return -1; /* not yet implemented */
}

#endif /* DICTUM_DEVICE_H */
