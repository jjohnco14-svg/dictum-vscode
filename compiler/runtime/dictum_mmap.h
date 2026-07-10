#ifndef DICTUM_MMAP_H
#define DICTUM_MMAP_H
/* dictum_mmap.h — Dictum stdlib stub (Sprint 3). */
/* Functions marked TODO are not yet implemented; linking may fail. */
#include "dictum_core.h"
#include <stddef.h>

/* TODO */ static inline void* dictum_mmap_open(dictum_text path, int size) {
    return (void*)0; /* not yet implemented */
}

/* TODO */ static inline void dictum_mmap_close(void *ptr, int size) {
    (void)0; /* not yet implemented */
}

/* TODO */ static inline void dictum_mmap_sync(void *ptr, int size) {
    (void)0; /* not yet implemented */
}

#endif /* DICTUM_MMAP_H */
