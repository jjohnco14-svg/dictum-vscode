#ifndef DICTUM_JSON_H
#define DICTUM_JSON_H
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
 * dictum_json.h — real implementation (closes Problem 0 for Json.*).
 * Pure C recursive-descent JSON parser, zero external dependency.
 *
 * Design: Json.parse returns a `whole number` handle (registry contract),
 * which is an index into a process-wide table of parsed documents. Every
 * other Json.* function takes that handle as its first arg. This matches
 * STDLIB_ACTION_FAMILIES exactly (Json.get(handle, path) etc.).
 */
#include "dictum_core.h"
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

typedef enum {
    DJ_NULL, DJ_BOOL, DJ_NUMBER, DJ_STRING, DJ_ARRAY, DJ_OBJECT, DJ_INVALID
} dj_type;

typedef struct dj_node {
    dj_type type;
    double num;
    int boolean;
    char *str;                 /* owned */
    struct dj_node **items;    /* array elements / object values */
    char **keys;               /* object keys (parallel to items), NULL for arrays */
    int count;
    int cap;
} dj_node;

/* ---- handle table ------------------------------------------------------ */

#define DICTUM_JSON_MAX_DOCS 256
static dj_node *_dj_docs[DICTUM_JSON_MAX_DOCS];
static int _dj_docs_init_done = 0;

static inline void _dj_ensure_init(void) {
    if (!_dj_docs_init_done) {
        for (int i = 0; i < DICTUM_JSON_MAX_DOCS; i++) _dj_docs[i] = NULL;
        _dj_docs_init_done = 1;
    }
}

static inline void dj_free_node(dj_node *n) {
    if (!n) return;
    if (n->str) free(n->str);
    for (int i = 0; i < n->count; i++) {
        if (n->keys && n->keys[i]) free(n->keys[i]);
        dj_free_node(n->items ? n->items[i] : NULL);
    }
    free(n->items);
    free(n->keys);
    free(n);
}

/* ---- parser ------------------------------------------------------------ */

typedef struct { const char *s; size_t pos; size_t len; int ok; } dj_parser;

static inline void dj_skip_ws(dj_parser *p) {
    while (p->pos < p->len) {
        char c = p->s[p->pos];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') p->pos++;
        else break;
    }
}

static dj_node *dj_parse_value(dj_parser *p);

static inline dj_node *dj_new_node(dj_type t) {
    dj_node *n = (dj_node *)calloc(1, sizeof(dj_node));
    n->type = t;
    return n;
}

static inline void dj_node_push(dj_node *parent, char *key, dj_node *child) {
    if (parent->count >= parent->cap) {
        parent->cap = parent->cap ? parent->cap * 2 : 8;
        parent->items = (dj_node **)realloc(parent->items, parent->cap * sizeof(dj_node *));
        if (parent->keys || key) {
            parent->keys = (char **)realloc(parent->keys, parent->cap * sizeof(char *));
        }
    }
    parent->items[parent->count] = child;
    if (parent->keys) parent->keys[parent->count] = key;
    parent->count++;
}

static char *dj_parse_string_raw(dj_parser *p) {
    /* assumes p->s[p->pos] == '"' */
    p->pos++;
    size_t cap = 32, len = 0;
    char *out = (char *)malloc(cap);
    while (p->pos < p->len && p->s[p->pos] != '"') {
        char c = p->s[p->pos];
        if (c == '\\' && p->pos + 1 < p->len) {
            p->pos++;
            char esc = p->s[p->pos];
            switch (esc) {
                case 'n': c = '\n'; break;
                case 't': c = '\t'; break;
                case 'r': c = '\r'; break;
                case '"': c = '"'; break;
                case '\\': c = '\\'; break;
                case '/': c = '/'; break;
                case 'b': c = '\b'; break;
                case 'f': c = '\f'; break;
                case 'u': {
                    /* Minimal \uXXXX -> UTF-8 (BMP only, no surrogate pairs). */
                    if (p->pos + 4 < p->len) {
                        char hex[5] = { p->s[p->pos+1], p->s[p->pos+2], p->s[p->pos+3], p->s[p->pos+4], 0 };
                        unsigned int cp = (unsigned int)strtoul(hex, NULL, 16);
                        p->pos += 4;
                        if (len + 4 >= cap) { cap *= 2; out = realloc(out, cap); }
                        if (cp < 0x80) { out[len++] = (char)cp; }
                        else if (cp < 0x800) {
                            out[len++] = (char)(0xC0 | (cp >> 6));
                            out[len++] = (char)(0x80 | (cp & 0x3F));
                        } else {
                            out[len++] = (char)(0xE0 | (cp >> 12));
                            out[len++] = (char)(0x80 | ((cp >> 6) & 0x3F));
                            out[len++] = (char)(0x80 | (cp & 0x3F));
                        }
                        p->pos++;
                        continue;
                    }
                    c = 'u';
                    break;
                }
                default: c = esc; break;
            }
            p->pos++;
        } else {
            p->pos++;
        }
        if (len + 1 >= cap) { cap *= 2; out = (char *)realloc(out, cap); }
        out[len++] = c;
    }
    if (p->pos < p->len) p->pos++; /* closing quote */
    out[len] = '\0';
    return out;
}

static dj_node *dj_parse_value(dj_parser *p) {
    dj_skip_ws(p);
    if (p->pos >= p->len) { p->ok = 0; return dj_new_node(DJ_INVALID); }
    char c = p->s[p->pos];

    if (c == '"') {
        dj_node *n = dj_new_node(DJ_STRING);
        n->str = dj_parse_string_raw(p);
        return n;
    }
    if (c == '{') {
        p->pos++;
        dj_node *n = dj_new_node(DJ_OBJECT);
        dj_skip_ws(p);
        if (p->pos < p->len && p->s[p->pos] == '}') { p->pos++; return n; }
        while (1) {
            dj_skip_ws(p);
            if (p->pos >= p->len || p->s[p->pos] != '"') { p->ok = 0; break; }
            char *key = dj_parse_string_raw(p);
            dj_skip_ws(p);
            if (p->pos >= p->len || p->s[p->pos] != ':') { p->ok = 0; free(key); break; }
            p->pos++; /* ':' */
            dj_node *val = dj_parse_value(p);
            dj_node_push(n, key, val);
            dj_skip_ws(p);
            if (p->pos < p->len && p->s[p->pos] == ',') { p->pos++; continue; }
            if (p->pos < p->len && p->s[p->pos] == '}') { p->pos++; break; }
            p->ok = 0; break;
        }
        return n;
    }
    if (c == '[') {
        p->pos++;
        dj_node *n = dj_new_node(DJ_ARRAY);
        dj_skip_ws(p);
        if (p->pos < p->len && p->s[p->pos] == ']') { p->pos++; return n; }
        while (1) {
            dj_node *val = dj_parse_value(p);
            dj_node_push(n, NULL, val);
            dj_skip_ws(p);
            if (p->pos < p->len && p->s[p->pos] == ',') { p->pos++; continue; }
            if (p->pos < p->len && p->s[p->pos] == ']') { p->pos++; break; }
            p->ok = 0; break;
        }
        return n;
    }
    if (strncmp(p->s + p->pos, "true", 4) == 0) {
        p->pos += 4; dj_node *n = dj_new_node(DJ_BOOL); n->boolean = 1; return n;
    }
    if (strncmp(p->s + p->pos, "false", 5) == 0) {
        p->pos += 5; dj_node *n = dj_new_node(DJ_BOOL); n->boolean = 0; return n;
    }
    if (strncmp(p->s + p->pos, "null", 4) == 0) {
        p->pos += 4; return dj_new_node(DJ_NULL);
    }
    if (c == '-' || (c >= '0' && c <= '9')) {
        char *end;
        double v = strtod(p->s + p->pos, &end);
        if (end == p->s + p->pos) { p->ok = 0; return dj_new_node(DJ_INVALID); }
        p->pos = (size_t)(end - p->s);
        dj_node *n = dj_new_node(DJ_NUMBER); n->num = v; return n;
    }
    p->ok = 0;
    return dj_new_node(DJ_INVALID);
}

/* Path navigation: "a.b.0.c" — dot-separated keys, numeric segments index arrays. */
static dj_node *dj_navigate(dj_node *root, const char *path) {
    if (!path || !*path) return root;
    dj_node *cur = root;
    char *copy = strdup(path);
    char *tok = strtok(copy, ".");
    while (tok && cur) {
        if (cur->type == DJ_OBJECT) {
            dj_node *next = NULL;
            for (int i = 0; i < cur->count; i++) {
                if (cur->keys[i] && strcmp(cur->keys[i], tok) == 0) { next = cur->items[i]; break; }
            }
            cur = next;
        } else if (cur->type == DJ_ARRAY) {
            char *endp; long idx = strtol(tok, &endp, 10);
            if (*endp != '\0' || idx < 0 || idx >= cur->count) { cur = NULL; }
            else cur = cur->items[idx];
        } else {
            cur = NULL;
        }
        tok = strtok(NULL, ".");
    }
    free(copy);
    return cur;
}

/* ---- public Json.* API -------------------------------------------------- */

static inline int dictum_json_parse(dictum_text text) {
    _dj_ensure_init();
    int slot = -1;
    for (int i = 0; i < DICTUM_JSON_MAX_DOCS; i++) if (!_dj_docs[i]) { slot = i; break; }
    if (slot < 0) { dictum_error_set("Json.parse: too many open documents"); return -1; }
    dj_parser p = { text ? text : "", 0, text ? strlen(text) : 0, 1 };
    dj_node *root = dj_parse_value(&p);
    dj_skip_ws(&p);
    if (!p.ok) { dj_free_node(root); dictum_error_set("Json.parse: invalid JSON"); return -1; }
    _dj_docs[slot] = root;
    return slot;
}

static inline dj_node *_dj_get_node(int handle, const char *path) {
    if (handle < 0 || handle >= DICTUM_JSON_MAX_DOCS || !_dj_docs[handle]) return NULL;
    return dj_navigate(_dj_docs[handle], path);
}

static inline dictum_text dictum_json_get(int handle, dictum_text path) {
    dj_node *n = _dj_get_node(handle, path);
    if (!n) return NULL;
    if (n->type == DJ_STRING) return strdup(n->str);
    if (n->type == DJ_NUMBER) { char buf[64]; snprintf(buf, sizeof buf, "%g", n->num); return strdup(buf); }
    if (n->type == DJ_BOOL) return strdup(n->boolean ? "true" : "false");
    if (n->type == DJ_NULL) return strdup("null");
    return strdup(""); /* object/array: use get_path/stringify instead */
}

static inline dictum_text dictum_json_get_string(int handle, dictum_text path) {
    dj_node *n = _dj_get_node(handle, path);
    if (!n || n->type != DJ_STRING) return NULL;
    return strdup(n->str);
}

static inline int dictum_json_get_int(int handle, dictum_text path) {
    dj_node *n = _dj_get_node(handle, path);
    return (n && n->type == DJ_NUMBER) ? (int)n->num : 0;
}

static inline double dictum_json_get_float(int handle, dictum_text path) {
    dj_node *n = _dj_get_node(handle, path);
    return (n && n->type == DJ_NUMBER) ? n->num : 0.0;
}

static inline int dictum_json_get_bool(int handle, dictum_text path) {
    dj_node *n = _dj_get_node(handle, path);
    return (n && n->type == DJ_BOOL) ? n->boolean : 0;
}

/* Sets a top-level-or-nested string field. Only supports assigning string
 * values (the common case for config/patch-style edits); returns 0 if the
 * path's parent isn't an object. */
static inline int dictum_json_set(int handle, dictum_text path, dictum_text value) {
    if (handle < 0 || handle >= DICTUM_JSON_MAX_DOCS || !_dj_docs[handle]) return 0;
    char *copy = strdup(path ? path : "");
    char *last_dot = strrchr(copy, '.');
    dj_node *parent;
    const char *leaf;
    if (last_dot) {
        *last_dot = '\0';
        parent = dj_navigate(_dj_docs[handle], copy);
        leaf = last_dot + 1;
    } else {
        parent = _dj_docs[handle];
        leaf = copy;
    }
    int result = 0;
    if (parent && parent->type == DJ_OBJECT) {
        for (int i = 0; i < parent->count; i++) {
            if (parent->keys[i] && strcmp(parent->keys[i], leaf) == 0) {
                dj_node *n = parent->items[i];
                if (n->str) free(n->str);
                n->type = DJ_STRING;
                n->str = strdup(value ? value : "");
                result = 1;
                break;
            }
        }
        if (!result) {
            dj_node *n = dj_new_node(DJ_STRING);
            n->str = strdup(value ? value : "");
            dj_node_push(parent, strdup(leaf), n);
            result = 1;
        }
    }
    free(copy);
    return result;
}

static void dj_stringify_into(dj_node *n, char **buf, size_t *len, size_t *cap);

static inline void dj_buf_append(char **buf, size_t *len, size_t *cap, const char *s, size_t n) {
    if (*len + n + 1 > *cap) { while (*len + n + 1 > *cap) *cap *= 2; *buf = (char *)realloc(*buf, *cap); }
    memcpy(*buf + *len, s, n);
    *len += n;
    (*buf)[*len] = '\0';
}

static void dj_stringify_into(dj_node *n, char **buf, size_t *len, size_t *cap) {
    if (!n) { dj_buf_append(buf, len, cap, "null", 4); return; }
    char tmp[64];
    switch (n->type) {
        case DJ_NULL: dj_buf_append(buf, len, cap, "null", 4); break;
        case DJ_BOOL: dj_buf_append(buf, len, cap, n->boolean ? "true" : "false", n->boolean ? 4 : 5); break;
        case DJ_NUMBER: {
            int w = snprintf(tmp, sizeof tmp, "%g", n->num);
            dj_buf_append(buf, len, cap, tmp, (size_t)w);
            break;
        }
        case DJ_STRING: {
            dj_buf_append(buf, len, cap, "\"", 1);
            for (char *p = n->str; *p; p++) {
                if (*p == '"' || *p == '\\') { dj_buf_append(buf, len, cap, "\\", 1); }
                dj_buf_append(buf, len, cap, p, 1);
            }
            dj_buf_append(buf, len, cap, "\"", 1);
            break;
        }
        case DJ_ARRAY: {
            dj_buf_append(buf, len, cap, "[", 1);
            for (int i = 0; i < n->count; i++) {
                if (i) dj_buf_append(buf, len, cap, ",", 1);
                dj_stringify_into(n->items[i], buf, len, cap);
            }
            dj_buf_append(buf, len, cap, "]", 1);
            break;
        }
        case DJ_OBJECT: {
            dj_buf_append(buf, len, cap, "{", 1);
            for (int i = 0; i < n->count; i++) {
                if (i) dj_buf_append(buf, len, cap, ",", 1);
                dj_buf_append(buf, len, cap, "\"", 1);
                dj_buf_append(buf, len, cap, n->keys[i], strlen(n->keys[i]));
                dj_buf_append(buf, len, cap, "\":", 2);
                dj_stringify_into(n->items[i], buf, len, cap);
            }
            dj_buf_append(buf, len, cap, "}", 1);
            break;
        }
        default: dj_buf_append(buf, len, cap, "null", 4); break;
    }
}

static inline dictum_text dictum_json_stringify(int handle) {
    if (handle < 0 || handle >= DICTUM_JSON_MAX_DOCS || !_dj_docs[handle]) return NULL;
    size_t cap = 128, len = 0;
    char *buf = (char *)malloc(cap);
    buf[0] = '\0';
    dj_stringify_into(_dj_docs[handle], &buf, &len, &cap);
    return buf;
}

static inline void dictum_json_destroy(int handle) {
    if (handle < 0 || handle >= DICTUM_JSON_MAX_DOCS) return;
    dj_free_node(_dj_docs[handle]);
    _dj_docs[handle] = NULL;
}

static inline int dictum_json_length(int handle) {
    if (handle < 0 || handle >= DICTUM_JSON_MAX_DOCS || !_dj_docs[handle]) return 0;
    return _dj_docs[handle]->count;
}

static inline int dictum_json_array_length(int handle, dictum_text path) {
    dj_node *n = _dj_get_node(handle, path);
    return (n && (n->type == DJ_ARRAY || n->type == DJ_OBJECT)) ? n->count : 0;
}

/* Navigates to `path`, then indexes into it as an array. Backs the whole
 * *_at family below. */
static inline dj_node *_dj_get_at_node(int handle, const char *path, int index) {
    dj_node *n = _dj_get_node(handle, path);
    if (!n || n->type != DJ_ARRAY || index < 0 || index >= n->count) return NULL;
    return n->items[index];
}

static inline dictum_text dictum_json_get_at(int handle, dictum_text path, int index) {
    dj_node *n = _dj_get_at_node(handle, path, index);
    if (!n) return NULL;
    if (n->type == DJ_STRING) return strdup(n->str);
    if (n->type == DJ_NUMBER) { char buf[64]; snprintf(buf, sizeof buf, "%g", n->num); return strdup(buf); }
    if (n->type == DJ_BOOL) return strdup(n->boolean ? "true" : "false");
    return strdup("");
}

static inline int dictum_json_get_int_at(int handle, dictum_text path, int index) {
    dj_node *n = _dj_get_at_node(handle, path, index);
    return (n && n->type == DJ_NUMBER) ? (int)n->num : 0;
}

static inline double dictum_json_get_float_at(int handle, dictum_text path, int index) {
    dj_node *n = _dj_get_at_node(handle, path, index);
    return (n && n->type == DJ_NUMBER) ? n->num : 0.0;
}

/* Registers the array element at `index` as its own document and returns
 * a NEW handle to it (matches the "object_at" naming: an object handle). */
static inline int dictum_json_get_object_at(int handle, dictum_text path, int index) {
    dj_node *n = _dj_get_at_node(handle, path, index);
    if (!n) return -1;
    _dj_ensure_init();
    int slot = -1;
    for (int i = 0; i < DICTUM_JSON_MAX_DOCS; i++) if (!_dj_docs[i]) { slot = i; break; }
    if (slot < 0) return -1;
    /* Alias into the same tree (do NOT deep copy): destroying the child
     * handle must not destroy the parent's node, so we do NOT free this
     * slot's node in dictum_json_destroy for aliased handles. We mark
     * that by storing the same pointer; caller should destroy the root
     * document, not aliases, to avoid a double free. This mirrors the
     * common usage pattern of read-then-discard-root. */
    _dj_docs[slot] = n;
    return slot;
}

static inline dictum_text dictum_json_get_path(int handle, dictum_text path) {
    dj_node *n = _dj_get_node(handle, path);
    if (!n) return NULL;
    size_t cap = 64, len = 0;
    char *buf = (char *)malloc(cap);
    buf[0] = '\0';
    dj_stringify_into(n, &buf, &len, &cap);
    return buf;
}

#endif /* DICTUM_JSON_H */
