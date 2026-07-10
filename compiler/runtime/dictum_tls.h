#ifndef DICTUM_TLS_H
#define DICTUM_TLS_H
/* dictum_tls.h — Dictum stdlib stub (Sprint 3). */
/* Functions marked TODO are not yet implemented; linking may fail. */
#include "dictum_core.h"
#include <stddef.h>

/* TODO */ static inline void* dictum_tls_connect(dictum_text host, int port) {
    return (void*)0; /* not yet implemented */
}

/* TODO */ static inline int dictum_tls_send(void *conn, dictum_text data) {
    return -1; /* not yet implemented */
}

/* TODO */ static inline dictum_text dictum_tls_recv(void *conn, int max_bytes) {
    return (dictum_text)0; /* not yet implemented */
}

/* TODO */ static inline void dictum_tls_close(void *conn) {
    (void)0; /* not yet implemented */
}

#endif /* DICTUM_TLS_H */
