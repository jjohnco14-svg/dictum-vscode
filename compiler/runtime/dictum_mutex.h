#ifndef DICTUM_MUTEX_H
#define DICTUM_MUTEX_H
/*
 * dictum_mutex.h — real implementation (closes Problem 0 for Mutex.*).
 *
 * NOTE: unlike Http/Json/File, this correctly needs the REAL system
 * header (<pthread.h>) rather than the "import from C" header-free
 * extern trick — pthread_mutex_t is a real struct whose size/layout is
 * platform-defined, not an opaque pointer we can safely re-declare.
 * Programs using this header need `-lpthread` at link time (see the
 * BuildDirective / Makefile fix that now auto-adds it whenever `use
 * Mutex`, `use Thread`, or `use Semaphore` is detected).
 */
#include "dictum_core.h"
#include <stdlib.h>
#include <pthread.h>

/* dictum_mutex_handle_t — was referenced by emit_c's stdlib type table
 * (extend_emitter maps mutex_handle -> dictum_mutex_handle_t) but never
 * actually defined anywhere, so any `let m be Mutex.create` failed to
 * compile with "unknown type name" even before Mutex.create itself was
 * filled in. Fixed here alongside the real implementation. */
typedef pthread_mutex_t *dictum_mutex_handle_t;

static inline dictum_mutex_handle_t dictum_mutex_create(void) {
    pthread_mutex_t *m = (pthread_mutex_t *)malloc(sizeof(pthread_mutex_t));
    if (!m) { dictum_error_set("Mutex.create: out of memory"); return NULL; }
    if (pthread_mutex_init(m, NULL) != 0) {
        free(m);
        dictum_error_set("Mutex.create: pthread_mutex_init failed");
        return NULL;
    }
    return m;
}

static inline void dictum_mutex_lock(dictum_mutex_handle_t m) {
    if (m) pthread_mutex_lock(m);
}

static inline void dictum_mutex_unlock(dictum_mutex_handle_t m) {
    if (m) pthread_mutex_unlock(m);
}

static inline void dictum_mutex_destroy(dictum_mutex_handle_t m) {
    if (!m) return;
    pthread_mutex_destroy(m);
    free(m);
}

#endif /* DICTUM_MUTEX_H */
