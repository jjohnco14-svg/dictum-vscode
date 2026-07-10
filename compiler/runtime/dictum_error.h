#ifndef DICTUM_ERROR_H
#define DICTUM_ERROR_H
/*
 * dictum_error.h — error-propagation runtime for `attempt` / `produce failure`.
 *
 * Generated code calls:
 *   dictum_error_clear()        — reset error state before an attempt
 *   dictum_error_set(msg)       — set by `produce failure with text "..."`
 *   DICTUM_HAS_ERROR()           — checked by `attempt` after a call
 *   dictum_error_last()          — message bound to `on failure <name>`
 *
 * Single-threaded by default. Define DICTUM_THREAD_LOCAL_ERROR before
 * including this header to make the error state thread-local.
 */

#ifdef DICTUM_THREAD_LOCAL_ERROR
  #if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
    #define DICTUM_TLS _Thread_local
  #else
    #define DICTUM_TLS __thread
  #endif
#else
  #define DICTUM_TLS
#endif

static DICTUM_TLS const char *__dictum_error_msg = (const char *)0;

static inline void dictum_error_clear(void) {
    __dictum_error_msg = (const char *)0;
}

static inline void dictum_error_set(const char *msg) {
    __dictum_error_msg = msg;
}

static inline const char *dictum_error_last(void) {
    return __dictum_error_msg ? __dictum_error_msg : "";
}

#define DICTUM_HAS_ERROR() (__dictum_error_msg != (const char *)0)

#endif /* DICTUM_ERROR_H */
