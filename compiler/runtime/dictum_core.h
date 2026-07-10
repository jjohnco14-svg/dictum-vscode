#ifndef DICTUM_CORE_H
#define DICTUM_CORE_H
/*
 * dictum_core.h — common runtime support for transpiled Dictum programs.
 *
 * Currently this is a thin umbrella header: it pulls in the error-state
 * runtime (dictum_error.h) that `attempt` / `produce failure` rely on, and
 * reserves a place for shared helpers (string/list utilities, etc.) as the
 * stdlib bridge grows. Stdlib module headers (dictum_console.h,
 * dictum_json.h, ...) include this header themselves.
 */

#include "dictum_error.h"

/* dictum_text — matches the `typedef const char* dictum_text;` emitted at
 * the top of every generated translation unit. Guarded so a program that
 * both emits its own typedef and includes this header doesn't conflict. */
#ifndef DICTUM_TEXT_DEFINED
#define DICTUM_TEXT_DEFINED
typedef const char *dictum_text;
#endif

#endif /* DICTUM_CORE_H */
