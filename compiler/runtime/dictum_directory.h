#ifndef DICTUM_DIRECTORY_H
#define DICTUM_DIRECTORY_H
/* dictum_directory.h — Dictum stdlib stub (Sprint 3). */
/* Functions marked TODO are not yet implemented; linking may fail. */
#include "dictum_core.h"
#include <stddef.h>

/* TODO */ static inline void* dictum_dir_open(dictum_text path) {
    return (void*)0; /* not yet implemented */
}

/* TODO */ static inline dictum_text dictum_dir_read(void *d) {
    return (dictum_text)0; /* not yet implemented */
}

/* TODO */ static inline void dictum_dir_close(void *d) {
    (void)0; /* not yet implemented */
}

/* TODO */ static inline int dictum_dir_mkdir(dictum_text path) {
    return -1; /* not yet implemented */
}

/* TODO */ static inline int dictum_dir_rmdir(dictum_text path) {
    return -1; /* not yet implemented */
}

#endif /* DICTUM_DIRECTORY_H */
