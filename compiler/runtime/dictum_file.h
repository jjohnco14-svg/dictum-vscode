#ifndef DICTUM_FILE_H
#define DICTUM_FILE_H
/* Defensive: these headers use POSIX functions (strdup, strcasecmp,
 * getaddrinfo, opendir/readdir) that glibc only declares under a
 * feature-test macro. The transpiler-generated .c file already
 * defines this ahead of every #include, but this header should not
 * silently miscompile (implicit int -> pointer truncation) if it's
 * ever compiled standalone or included first under strict -std=c11. */
#ifndef _DEFAULT_SOURCE
#define _DEFAULT_SOURCE
#endif

/* FIX: defensive feature-test macro so this header declares
 * POSIX functions (strdup, strcasecmp, getaddrinfo, opendir/readdir,
 * etc.) regardless of how strictly the includer's -std= flag is set.
 * Compiling under `-std=c11` (not gnu11) without this previously left
 * these as implicit declarations, which the compiler assumes return
 * `int` — silently truncating a real 64-bit pointer and reinterpreting
 * the garbage as a pointer on every affected call. Guarded so it's a
 * no-op if something upstream already defined it (e.g. the dictumc
 * emitter now does this itself, before its own #include block). */
#ifndef _DEFAULT_SOURCE
#define _DEFAULT_SOURCE
#endif
/*
 * dictum_file.h — real implementation (closes Problem 0 for File.*).
 * Pure libc (stdio.h + dirent.h), no external dependency, no extra
 * link flags required.
 */
#include "dictum_core.h"
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <dirent.h>
#include <sys/stat.h>

/* Reads the whole file into a heap string. Returns NULL on failure. */
static inline dictum_text dictum_file_read(dictum_text path) {
    if (!path) return NULL;
    FILE *f = fopen(path, "rb");
    if (!f) { dictum_error_set("File.read: could not open file"); return NULL; }
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); dictum_error_set("File.read: seek failed"); return NULL; }
    long size = ftell(f);
    if (size < 0) { fclose(f); dictum_error_set("File.read: tell failed"); return NULL; }
    rewind(f);
    char *buf = (char *)malloc((size_t)size + 1);
    if (!buf) { fclose(f); dictum_error_set("File.read: out of memory"); return NULL; }
    size_t got = fread(buf, 1, (size_t)size, f);
    fclose(f);
    buf[got] = '\0';
    return buf;
}

/* Overwrites (creates/truncates) `path` with `content`. */
static inline int dictum_file_write(dictum_text path, dictum_text content) {
    if (!path) return 0;
    FILE *f = fopen(path, "wb");
    if (!f) { dictum_error_set("File.write: could not open file"); return 0; }
    size_t len = content ? strlen(content) : 0;
    size_t wrote = len ? fwrite(content, 1, len, f) : 0;
    fclose(f);
    if (wrote != len) { dictum_error_set("File.write: short write"); return 0; }
    return 1;
}

static inline int dictum_file_exists(dictum_text path) {
    if (!path) return 0;
    struct stat st;
    return stat(path, &st) == 0;
}

static inline int dictum_file_delete(dictum_text path) {
    if (!path) return 0;
    if (remove(path) != 0) { dictum_error_set("File.delete: remove failed"); return 0; }
    return 1;
}

/* Returns directory entries (excluding "." and "..") newline-joined.
 * Returns NULL if the path can't be opened as a directory. */
static inline dictum_text dictum_file_list(dictum_text path) {
    if (!path) return NULL;
    DIR *d = opendir(path);
    if (!d) { dictum_error_set("File.list: could not open directory"); return NULL; }
    size_t cap = 256, len = 0;
    char *out = (char *)malloc(cap);
    if (!out) { closedir(d); return NULL; }
    out[0] = '\0';
    struct dirent *ent;
    int first = 1;
    while ((ent = readdir(d)) != NULL) {
        if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) continue;
        size_t nlen = strlen(ent->d_name);
        size_t needed = len + nlen + 2; /* + separator + NUL */
        if (needed > cap) { while (cap < needed) cap *= 2; out = (char *)realloc(out, cap); }
        if (!first) { out[len++] = '\n'; }
        memcpy(out + len, ent->d_name, nlen);
        len += nlen;
        out[len] = '\0';
        first = 0;
    }
    closedir(d);
    return out;
}

#endif /* DICTUM_FILE_H */
