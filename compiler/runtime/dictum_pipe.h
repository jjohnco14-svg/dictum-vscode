#ifndef DICTUM_PIPE_H
#define DICTUM_PIPE_H
/* dictum_pipe.h — Dictum stdlib stub (Sprint 3). */
/* Functions marked TODO are not yet implemented; linking may fail. */
#include "dictum_core.h"
#include <stddef.h>

/* TODO */ static inline int dictum_pipe_create(int fds[2]) {
    return -1; /* not yet implemented */
}

/* TODO */ static inline int dictum_pipe_write(int fd, void *buf, int n) {
    return -1; /* not yet implemented */
}

/* TODO */ static inline int dictum_pipe_read(int fd, void *buf, int n) {
    return -1; /* not yet implemented */
}

#endif /* DICTUM_PIPE_H */
