#ifndef DICTUM_CONSOLE_H
#define DICTUM_CONSOLE_H
/*
 * dictum_console.h — backs `use Console` / Console.* stdlib calls.
 *
 * Console.write       -> dictum_console_write
 * Console.write_line  -> dictum_console_write_line
 * Console.read_line   -> dictum_console_read_line
 */

#include "dictum_core.h"
#include <stdio.h>
#include <string.h>

static inline void dictum_console_write(dictum_text s) {
    fputs(s ? s : "", stdout);
}

static inline void dictum_console_write_line(dictum_text s) {
    fputs(s ? s : "", stdout);
    fputc('\n', stdout);
}

/* Reads one line from stdin (trailing newline stripped). The returned
 * pointer is valid until the next call (static buffer) — copy it if it
 * needs to outlive that. */
static inline dictum_text dictum_console_read_line(void) {
    static char buf[4096];
    if (!fgets(buf, sizeof buf, stdin)) {
        buf[0] = '\0';
        return buf;
    }
    size_t len = strlen(buf);
    if (len > 0 && buf[len - 1] == '\n') {
        buf[len - 1] = '\0';
    }
    return buf;
}

#endif /* DICTUM_CONSOLE_H */
