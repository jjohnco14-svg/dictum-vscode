#ifndef DICTUM_THREAD_H
#define DICTUM_THREAD_H
/*
 * dictum_thread.h — real implementation (closes Problem 0 for Thread.*).
 * Real system header needed (<pthread.h>), same reasoning as
 * dictum_mutex.h. Requires -lpthread at link time.
 */
#include "dictum_core.h"
#include <stdlib.h>
#include <pthread.h>

/* Was referenced by emit_c's stdlib type table but never defined —
 * same gap as dictum_mutex_handle_t, fixed here. */
typedef pthread_t *dictum_thread_handle_t;

/* Dictum `action taking nothing produces nothing` lowers to a plain
 * `void (*)(void)` in C — pthreads wants `void *(*)(void*)`, so we wrap. */
typedef void (*dictum_thread_fn)(void);

typedef struct { dictum_thread_fn fn; } _dictum_thread_trampoline_arg;

static inline void *_dictum_thread_trampoline(void *arg) {
    _dictum_thread_trampoline_arg *a = (_dictum_thread_trampoline_arg *)arg;
    dictum_thread_fn fn = a->fn;
    free(a);
    fn();
    return NULL;
}

static inline dictum_thread_handle_t dictum_thread_start(dictum_thread_fn fn) {
    pthread_t *t = (pthread_t *)malloc(sizeof(pthread_t));
    if (!t) { dictum_error_set("Thread.start: out of memory"); return NULL; }
    _dictum_thread_trampoline_arg *arg =
        (_dictum_thread_trampoline_arg *)malloc(sizeof(_dictum_thread_trampoline_arg));
    arg->fn = fn;
    if (pthread_create(t, NULL, _dictum_thread_trampoline, arg) != 0) {
        free(t); free(arg);
        dictum_error_set("Thread.start: pthread_create failed");
        return NULL;
    }
    return t;
}

static inline void dictum_thread_join(dictum_thread_handle_t t) {
    if (!t) return;
    pthread_join(*t, NULL);
    free(t);
}

#endif /* DICTUM_THREAD_H */
