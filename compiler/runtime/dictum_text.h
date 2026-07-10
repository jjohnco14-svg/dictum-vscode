#ifndef DICTUM_TEXT_H
#define DICTUM_TEXT_H
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
 * dictum_text.h — real implementation (closes Problem 0 for Text.*).
 *
 * dictum_text is `const char*`. Any function that returns a *new* string
 * heap-allocates it with malloc/strdup so it composes with the existing
 * `release` intrinsic (emit_c.py emits `free(x)` for `release x`).
 * Functions that return a *view* into an existing string return a pointer
 * that must NOT be released independently (documented per-function).
 */
#include "dictum_core.h"
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <stdio.h>

/* ---- length / comparison ------------------------------------------- */

static inline int dictum_text_length(dictum_text s) {
    return s ? (int)strlen(s) : 0;
}

/* Counts Unicode codepoints (UTF-8 decoded), not bytes. */
static inline int dictum_text_utf8_length(dictum_text s) {
    if (!s) return 0;
    int count = 0;
    const unsigned char *p = (const unsigned char *)s;
    while (*p) {
        if ((*p & 0xC0) != 0x80) count++;  /* not a continuation byte */
        p++;
    }
    return count;
}

static inline int dictum_text_compare(dictum_text a, dictum_text b) {
    return strcmp(a ? a : "", b ? b : "");
}

static inline int dictum_text_starts_with(dictum_text s, dictum_text prefix) {
    if (!s || !prefix) return 0;
    size_t pl = strlen(prefix);
    return strncmp(s, prefix, pl) == 0;
}

static inline int dictum_text_ends_with(dictum_text s, dictum_text suffix) {
    if (!s || !suffix) return 0;
    size_t sl = strlen(s), fl = strlen(suffix);
    if (fl > sl) return 0;
    return strcmp(s + (sl - fl), suffix) == 0;
}

static inline int dictum_text_contains(dictum_text s, dictum_text needle) {
    if (!s || !needle) return 0;
    return strstr(s, needle) != NULL;
}

/* Returns -1 if not found (matches the pre-existing dictum_text_find_from
 * contract used elsewhere in the emitter). */
static inline int dictum_text_find(dictum_text s, dictum_text needle) {
    if (!s || !needle) return -1;
    const char *hit = strstr(s, needle);
    return hit ? (int)(hit - s) : -1;
}

static inline int dictum_text_find_from(dictum_text s, dictum_text needle, int from) {
    if (!s || !needle) return -1;
    int len = (int)strlen(s);
    if (from < 0) from = 0;
    if (from > len) return -1;
    const char *hit = strstr(s + from, needle);
    return hit ? (int)(hit - s) : -1;
}

/* ---- allocation-returning transforms --------------------------------- */

static inline dictum_text dictum_text_copy(dictum_text s, dictum_text unused_dst) {
    (void)unused_dst; /* legacy alias kept strcpy-shaped; real copy allocates */
    return s ? strdup(s) : strdup("");
}

static inline dictum_text dictum_text_concat(dictum_text a, dictum_text b) {
    size_t la = a ? strlen(a) : 0, lb = b ? strlen(b) : 0;
    char *out = (char *)malloc(la + lb + 1);
    if (!out) return strdup("");
    memcpy(out, a ? a : "", la);
    memcpy(out + la, b ? b : "", lb);
    out[la + lb] = '\0';
    return out;
}

/* start inclusive, end exclusive; clamps to valid range. */
static inline dictum_text dictum_text_slice(dictum_text s, int start, int end) {
    if (!s) return strdup("");
    int len = (int)strlen(s);
    if (start < 0) start = 0;
    if (end > len) end = len;
    if (start >= end) return strdup("");
    int n = end - start;
    char *out = (char *)malloc((size_t)n + 1);
    if (!out) return strdup("");
    memcpy(out, s + start, (size_t)n);
    out[n] = '\0';
    return out;
}

static inline dictum_text dictum_text_trim(dictum_text s) {
    if (!s) return strdup("");
    const char *start = s;
    while (*start && isspace((unsigned char)*start)) start++;
    const char *end = s + strlen(s);
    while (end > start && isspace((unsigned char)*(end - 1))) end--;
    size_t n = (size_t)(end - start);
    char *out = (char *)malloc(n + 1);
    if (!out) return strdup("");
    memcpy(out, start, n);
    out[n] = '\0';
    return out;
}

static inline dictum_text dictum_text_to_upper(dictum_text s) {
    if (!s) return strdup("");
    char *out = strdup(s);
    if (out) for (char *p = out; *p; p++) *p = (char)toupper((unsigned char)*p);
    return out;
}

static inline dictum_text dictum_text_to_lower(dictum_text s) {
    if (!s) return strdup("");
    char *out = strdup(s);
    if (out) for (char *p = out; *p; p++) *p = (char)tolower((unsigned char)*p);
    return out;
}

/* Replaces every non-overlapping occurrence of `from` with `to`. */
static inline dictum_text dictum_text_replace(dictum_text s, dictum_text from, dictum_text to) {
    if (!s) return strdup("");
    if (!from || !*from) return strdup(s);
    size_t slen = strlen(s), flen = strlen(from), tlen = to ? strlen(to) : 0;

    size_t count = 0;
    const char *scan = s;
    const char *hit;
    while ((hit = strstr(scan, from)) != NULL) { count++; scan = hit + flen; }
    if (count == 0) return strdup(s);

    size_t out_len = slen - count * flen + count * tlen;
    char *out = (char *)malloc(out_len + 1);
    if (!out) return strdup("");

    char *w = out;
    scan = s;
    while ((hit = strstr(scan, from)) != NULL) {
        size_t chunk = (size_t)(hit - scan);
        memcpy(w, scan, chunk); w += chunk;
        memcpy(w, to ? to : "", tlen); w += tlen;
        scan = hit + flen;
    }
    strcpy(w, scan);
    return out;
}

/* NULL-terminated dictum_text* array (sentinel-terminated, not count-based —
 * matches how `text list` values from a C-side function are consumed
 * elsewhere: iterate until NULL). Caller frees the array and each element. */
static inline dictum_text *dictum_text_split(dictum_text s, dictum_text delim) {
    size_t cap = 8, n = 0;
    dictum_text *out = (dictum_text *)malloc(cap * sizeof(dictum_text));
    if (!out) return NULL;
    if (!s || !delim || !*delim) {
        out[n++] = strdup(s ? s : "");
        out[n] = NULL;
        return out;
    }
    size_t dlen = strlen(delim);
    const char *scan = s;
    const char *hit;
    while ((hit = strstr(scan, delim)) != NULL) {
        if (n + 1 >= cap) { cap *= 2; out = (dictum_text *)realloc(out, cap * sizeof(dictum_text)); }
        size_t chunk = (size_t)(hit - scan);
        char *piece = (char *)malloc(chunk + 1);
        memcpy(piece, scan, chunk); piece[chunk] = '\0';
        out[n++] = piece;
        scan = hit + dlen;
    }
    if (n + 1 >= cap) { cap += 1; out = (dictum_text *)realloc(out, cap * sizeof(dictum_text)); }
    out[n++] = strdup(scan);
    if (n >= cap) { cap += 1; out = (dictum_text *)realloc(out, cap * sizeof(dictum_text)); }
    out[n] = NULL;
    return out;
}

static inline dictum_text dictum_text_join(dictum_text list_csv, dictum_text sep) {
    /* Legacy 2-arg join kept for callers that pass an already-delimited
     * blob; real list-of-text joining is dictum_text_join_list below. */
    (void)sep;
    return strdup(list_csv ? list_csv : "");
}

static inline dictum_text dictum_text_join_list(dictum_text *parts, dictum_text sep) {
    if (!parts || !parts[0]) return strdup("");
    size_t slen = sep ? strlen(sep) : 0;
    size_t total = 0;
    int count = 0;
    for (int i = 0; parts[i]; i++) { total += strlen(parts[i]); count++; }
    total += slen * (size_t)(count > 0 ? count - 1 : 0);
    char *out = (char *)malloc(total + 1);
    if (!out) return strdup("");
    char *w = out;
    for (int i = 0; parts[i]; i++) {
        size_t l = strlen(parts[i]);
        memcpy(w, parts[i], l); w += l;
        if (parts[i + 1] && slen) { memcpy(w, sep, slen); w += slen; }
    }
    *w = '\0';
    return out;
}

/* ---- numeric conversions ---------------------------------------------- */

static inline int dictum_text_to_int(dictum_text s) {
    return s ? (int)strtol(s, NULL, 10) : 0;
}

static inline double dictum_text_to_float(dictum_text s) {
    return s ? strtod(s, NULL) : 0.0;
}

static inline dictum_text dictum_text_from_int(int n) {
    char buf[32];
    snprintf(buf, sizeof buf, "%d", n);
    return strdup(buf);
}

static inline dictum_text dictum_text_from_float(double n) {
    char buf[64];
    snprintf(buf, sizeof buf, "%g", n);
    return strdup(buf);
}

/* Minimal `{}`-style formatter: dictum_text_format("hi {}", "world") style
 * single-placeholder substitution (matches the 2-arg registry signature). */
static inline dictum_text dictum_text_format(dictum_text fmt, dictum_text arg) {
    if (!fmt) return strdup("");
    const char *ph = strstr(fmt, "{}");
    if (!ph) return strdup(fmt);
    size_t pre = (size_t)(ph - fmt);
    size_t alen = arg ? strlen(arg) : 0;
    size_t post_start = pre + 2;
    size_t postlen = strlen(fmt + post_start);
    char *out = (char *)malloc(pre + alen + postlen + 1);
    if (!out) return strdup("");
    memcpy(out, fmt, pre);
    memcpy(out + pre, arg ? arg : "", alen);
    memcpy(out + pre + alen, fmt + post_start, postlen + 1);
    return out;
}

/* ---- grapheme-aware helpers (UTF-8) ------------------------------------ */
/* Treat each codepoint as one grapheme; good enough without a full Unicode
 * grapheme-cluster table, and correct for the common non-combining case. */

static inline int dictum_text_grapheme_length(dictum_text s) {
    return dictum_text_utf8_length(s);
}

static inline size_t _dictum_utf8_char_len(unsigned char c) {
    if ((c & 0x80) == 0x00) return 1;
    if ((c & 0xE0) == 0xC0) return 2;
    if ((c & 0xF0) == 0xE0) return 3;
    if ((c & 0xF8) == 0xF0) return 4;
    return 1; /* invalid lead byte — treat as single byte, don't hang */
}

static inline dictum_text dictum_text_grapheme_slice(dictum_text s, int start, int end) {
    if (!s) return strdup("");
    const unsigned char *p = (const unsigned char *)s;
    int idx = 0;
    const char *byte_start = NULL, *byte_end = s + strlen(s);
    while (*p) {
        if (idx == start) byte_start = (const char *)p;
        if (idx == end) { byte_end = (const char *)p; break; }
        p += _dictum_utf8_char_len(*p);
        idx++;
    }
    if (!byte_start) return strdup("");
    if (byte_end < byte_start) byte_end = byte_start;
    size_t n = (size_t)(byte_end - byte_start);
    char *out = (char *)malloc(n + 1);
    if (!out) return strdup("");
    memcpy(out, byte_start, n);
    out[n] = '\0';
    return out;
}

static inline dictum_text dictum_text_grapheme_reverse(dictum_text s) {
    if (!s) return strdup("");
    size_t byte_len = strlen(s);
    /* Collect codepoint byte-spans, then emit back to front. */
    size_t cap = 16, n = 0;
    size_t *starts = (size_t *)malloc(cap * sizeof(size_t));
    size_t *lens = (size_t *)malloc(cap * sizeof(size_t));
    const unsigned char *p = (const unsigned char *)s;
    size_t off = 0;
    while (*p) {
        if (n >= cap) { cap *= 2; starts = realloc(starts, cap * sizeof(size_t)); lens = realloc(lens, cap * sizeof(size_t)); }
        size_t clen = _dictum_utf8_char_len(*p);
        if (off + clen > byte_len) clen = 1;
        starts[n] = off; lens[n] = clen;
        n++; off += clen; p += clen;
    }
    char *out = (char *)malloc(byte_len + 1);
    size_t w = 0;
    for (size_t i = n; i > 0; i--) {
        memcpy(out + w, s + starts[i - 1], lens[i - 1]);
        w += lens[i - 1];
    }
    out[w] = '\0';
    free(starts); free(lens);
    return out;
}

/* NFC-style normalization is out of scope without a Unicode data table;
 * this pass normalizes line endings and strips a UTF-8 BOM, which is the
 * practically common case, and is honest about not doing full NFC/NFD. */
static inline dictum_text dictum_text_normalize(dictum_text s) {
    if (!s) return strdup("");
    if ((unsigned char)s[0] == 0xEF && (unsigned char)s[1] == 0xBB && (unsigned char)s[2] == 0xBF) {
        s += 3;
    }
    size_t len = strlen(s);
    char *out = (char *)malloc(len + 1);
    if (!out) return strdup("");
    size_t w = 0;
    for (size_t i = 0; i < len; i++) {
        if (s[i] == '\r' && i + 1 < len && s[i + 1] == '\n') continue; /* drop \r of \r\n */
        out[w++] = s[i];
    }
    out[w] = '\0';
    return out;
}

#endif /* DICTUM_TEXT_H */
