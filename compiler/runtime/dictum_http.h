#ifndef DICTUM_HTTP_H
#define DICTUM_HTTP_H
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
 * dictum_http.h — real implementation (closes Problem 0 for Http.*
 * against plain http:// URLs).
 *
 * Built on dictum_net.h (raw POSIX sockets) with a minimal hand-rolled
 * HTTP/1.1 client — no libcurl dependency, no extra link flags.
 *
 * KNOWN LIMITATION (documented, not silently swallowed): https:// URLs
 * are NOT handled by this file — that requires TLS (dictum_tls.h, which
 * is still a stub pending OpenSSL wiring). Calling any Http.* function
 * with an https:// URL sets a clear runtime error via dictum_error_set()
 * and returns NULL, rather than silently doing a plaintext request to
 * port 443 or fabricating a response. This is a real, narrower gap than
 * the "returns garbage" state Http.* was in before — it's the one part
 * of Problem 0 that is legitimately still open, and needs a dedicated
 * Tls implementation, not a stdlib rewrite.
 */
#include "dictum_core.h"
#include "dictum_net.h"
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <ctype.h>

typedef struct {
    char scheme[8];
    char host[256];
    int port;
    char path[2048];
} _dictum_http_url;

static inline int _dictum_http_parse_url(dictum_text url, _dictum_http_url *out) {
    if (!url) return 0;
    const char *p = url;
    const char *scheme_end = strstr(p, "://");
    if (!scheme_end) return 0;
    size_t slen = (size_t)(scheme_end - p);
    if (slen >= sizeof(out->scheme)) return 0;
    memcpy(out->scheme, p, slen); out->scheme[slen] = '\0';
    p = scheme_end + 3;

    const char *path_start = strchr(p, '/');
    const char *hostport_end = path_start ? path_start : p + strlen(p);
    size_t hp_len = (size_t)(hostport_end - p);
    char hostport[256];
    if (hp_len >= sizeof(hostport)) return 0;
    memcpy(hostport, p, hp_len); hostport[hp_len] = '\0';

    char *colon = strchr(hostport, ':');
    if (colon) {
        *colon = '\0';
        out->port = atoi(colon + 1);
    } else {
        out->port = (strcmp(out->scheme, "https") == 0) ? 443 : 80;
    }
    snprintf(out->host, sizeof(out->host), "%s", hostport);

    if (path_start) snprintf(out->path, sizeof(out->path), "%s", path_start);
    else snprintf(out->path, sizeof(out->path), "/");
    return 1;
}

/* Reads a full HTTP response off `sock` until the peer closes the
 * connection (we always send "Connection: close", so this terminates),
 * decodes chunked transfer-encoding if present, and returns just the
 * body. Returns NULL on a transport-level failure. */
static inline dictum_text _dictum_http_read_response(dictum_net_socket_t sock) {
    size_t cap = 8192, len = 0;
    char *buf = (char *)malloc(cap);
    if (!buf) return NULL;
    for (;;) {
        if (len + 4096 + 1 > cap) { cap *= 2; buf = (char *)realloc(buf, cap); }
        ssize_t n = recv(sock, buf + len, cap - len - 1, 0);
        if (n <= 0) break;
        len += (size_t)n;
    }
    buf[len] = '\0';

    char *header_end = strstr(buf, "\r\n\r\n");
    if (!header_end) { return buf; /* no body / malformed: return raw */ }
    char *headers = buf;
    char *body_start = header_end + 4;
    size_t body_len = len - (size_t)(body_start - buf);

    int chunked = 0;
    for (char *line = headers; line < header_end; ) {
        char *eol = strstr(line, "\r\n");
        if (!eol || eol > header_end) eol = header_end;
        if (strncasecmp(line, "Transfer-Encoding:", 18) == 0) {
            char saved = *eol; *eol = '\0';
            if (strstr(line, "chunked")) chunked = 1;
            *eol = saved;
        }
        line = (eol == header_end) ? header_end : eol + 2;
    }

    if (!chunked) {
        char *out = (char *)malloc(body_len + 1);
        memcpy(out, body_start, body_len);
        out[body_len] = '\0';
        free(buf);
        return out;
    }

    /* Decode chunked body in place. */
    char *decoded = (char *)malloc(body_len + 1);
    size_t dlen = 0;
    char *p = body_start;
    char *end = body_start + body_len;
    while (p < end) {
        char *line_end = strstr(p, "\r\n");
        if (!line_end) break;
        long chunk_size = strtol(p, NULL, 16);
        if (chunk_size <= 0) break;
        char *chunk_data = line_end + 2;
        if (chunk_data + chunk_size > end) chunk_size = end - chunk_data;
        memcpy(decoded + dlen, chunk_data, (size_t)chunk_size);
        dlen += (size_t)chunk_size;
        p = chunk_data + chunk_size + 2; /* skip trailing \r\n */
    }
    decoded[dlen] = '\0';
    free(buf);
    return decoded;
}

static inline dictum_text _dictum_http_request(const char *method, dictum_text url,
                                                dictum_text body, dictum_text content_type,
                                                dictum_text extra_headers) {
    _dictum_http_url u;
    if (!_dictum_http_parse_url(url, &u)) {
        dictum_error_set("Http: malformed URL");
        return NULL;
    }
    if (strcmp(u.scheme, "https") == 0) {
        dictum_error_set("Http: https:// is not yet supported (needs Tls; see dictum_http.h)");
        return NULL;
    }
    if (strcmp(u.scheme, "http") != 0) {
        dictum_error_set("Http: unsupported scheme");
        return NULL;
    }

    dictum_net_socket_t sock = dictum_net_connect(u.host, u.port);
    if (sock < 0) return NULL; /* dictum_net_connect already set an error */

    size_t body_len = body ? strlen(body) : 0;
    char req_head[4096];
    int n = snprintf(req_head, sizeof(req_head),
        "%s %s HTTP/1.1\r\n"
        "Host: %s\r\n"
        "User-Agent: dictum/0.1\r\n"
        "Accept: */*\r\n"
        "Connection: close\r\n",
        method, u.path, u.host);
    if (n < 0 || (size_t)n >= sizeof(req_head)) {
        dictum_net_close(sock);
        dictum_error_set("Http: request too large");
        return NULL;
    }
    size_t used = (size_t)n;

    if (content_type && used < sizeof(req_head)) {
        int m = snprintf(req_head + used, sizeof(req_head) - used, "Content-Type: %s\r\n", content_type);
        if (m > 0) used += (size_t)m;
    }
    if (extra_headers && *extra_headers && used < sizeof(req_head)) {
        int m = snprintf(req_head + used, sizeof(req_head) - used, "%s", extra_headers);
        if (m > 0) used += (size_t)m;
    }
    if (body && used < sizeof(req_head)) {
        int m = snprintf(req_head + used, sizeof(req_head) - used, "Content-Length: %zu\r\n", body_len);
        if (m > 0) used += (size_t)m;
    }
    if (used >= sizeof(req_head)) {
        dictum_net_close(sock);
        dictum_error_set("Http: request too large");
        return NULL;
    }
    n = (int)used;

    /* Build the full request (head + blank line + optional body). */
    size_t total = (size_t)n + 2 + body_len;
    char *full = (char *)malloc(total + 1);
    memcpy(full, req_head, (size_t)n);
    memcpy(full + n, "\r\n", 2);
    if (body_len) memcpy(full + n + 2, body, body_len);
    full[total] = '\0';

    if (dictum_net_send(sock, full) < (int)total) {
        free(full);
        dictum_net_close(sock);
        dictum_error_set("Http: send failed");
        return NULL;
    }
    free(full);

    dictum_text response = _dictum_http_read_response(sock);
    dictum_net_close(sock);
    return response;
}

static inline dictum_text dictum_http_get(dictum_text url) {
    return _dictum_http_request("GET", url, NULL, NULL, NULL);
}

static inline dictum_text dictum_http_post(dictum_text url, dictum_text body, dictum_text content_type) {
    return _dictum_http_request("POST", url, body, content_type ? content_type : "text/plain", NULL);
}

static inline dictum_text dictum_http_post_form(dictum_text url, dictum_text form_body) {
    return _dictum_http_request("POST", url, form_body, "application/x-www-form-urlencoded", NULL);
}

static inline dictum_text dictum_http_put(dictum_text url, dictum_text body) {
    return _dictum_http_request("PUT", url, body, "text/plain", NULL);
}

static inline dictum_text dictum_http_delete(dictum_text url) {
    return _dictum_http_request("DELETE", url, NULL, NULL, NULL);
}

static inline dictum_text dictum_http_patch(dictum_text url, dictum_text body) {
    return _dictum_http_request("PATCH", url, body, "text/plain", NULL);
}

/* Returns the raw response headers (everything before the blank line)
 * for a HEAD-equivalent probe done via GET (kept simple/dependency-free;
 * a true HEAD is one extra method string away if ever needed). */
static inline dictum_text dictum_http_headers(dictum_text url) {
    _dictum_http_url u;
    if (!_dictum_http_parse_url(url, &u)) { dictum_error_set("Http.headers: malformed URL"); return NULL; }
    if (strcmp(u.scheme, "https") == 0) {
        dictum_error_set("Http.headers: https:// is not yet supported (needs Tls)");
        return NULL;
    }
    dictum_net_socket_t sock = dictum_net_connect(u.host, u.port);
    if (sock < 0) return NULL;
    char req[2048];
    snprintf(req, sizeof(req), "HEAD %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", u.path, u.host);
    dictum_net_send(sock, req);
    size_t cap = 4096, len = 0;
    char *buf = (char *)malloc(cap);
    for (;;) {
        if (len + 2048 + 1 > cap) { cap *= 2; buf = (char*)realloc(buf, cap); }
        ssize_t n = recv(sock, buf + len, cap - len - 1, 0);
        if (n <= 0) break;
        len += (size_t)n;
    }
    buf[len] = '\0';
    dictum_net_close(sock);
    char *end = strstr(buf, "\r\n\r\n");
    if (end) *end = '\0';
    return buf;
}

#endif /* DICTUM_HTTP_H */
