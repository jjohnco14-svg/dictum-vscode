#ifndef DICTUM_SHM_H
#define DICTUM_SHM_H
/* dictum_shm.h — Dictum stdlib stub (Sprint 3). */
/* Functions marked TODO are not yet implemented; linking may fail. */
#include "dictum_core.h"
#include <stddef.h>

/* TODO */ static inline void* dictum_shm_create(dictum_text name, int size) {
    return (void*)0; /* not yet implemented */
}

/* TODO */ static inline void* dictum_shm_open(dictum_text name) {
    return (void*)0; /* not yet implemented */
}

/* TODO */ static inline void dictum_shm_close(void *ptr, int size) {
    (void)0; /* not yet implemented */
}

/* TODO */ static inline void dictum_shm_unlink(dictum_text name) {
    (void)0; /* not yet implemented */
}

#endif /* DICTUM_SHM_H */
