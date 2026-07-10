#ifndef DICTUM_NET_H
#define DICTUM_NET_H
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
 * dictum_net.h — real implementation (closes Problem 0 for Net.*).
 * POSIX sockets (<sys/socket.h>, <netdb.h>). No extra link flags needed
 * on Linux/glibc (socket syscalls live in libc itself).
 */
#include "dictum_core.h"
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <netinet/in.h>
#include <netdb.h>
#include <arpa/inet.h>

/* Was referenced by emit_c's stdlib type table but never defined. A file
 * descriptor is fine to pass by value, unlike Mutex/Thread's structs. */
typedef int dictum_net_socket_t;
#define DICTUM_NET_INVALID_SOCKET (-1)

static inline dictum_net_socket_t dictum_net_connect(dictum_text host, int port) {
    if (!host) { dictum_error_set("Net.connect: null host"); return DICTUM_NET_INVALID_SOCKET; }
    char portbuf[16];
    snprintf(portbuf, sizeof portbuf, "%d", port);

    struct addrinfo hints, *res;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host, portbuf, &hints, &res) != 0) {
        dictum_error_set("Net.connect: DNS resolution failed");
        return DICTUM_NET_INVALID_SOCKET;
    }
    int fd = DICTUM_NET_INVALID_SOCKET;
    for (struct addrinfo *rp = res; rp != NULL; rp = rp->ai_next) {
        fd = socket(rp->ai_family, rp->ai_socktype, rp->ai_protocol);
        if (fd < 0) continue;
        if (connect(fd, rp->ai_addr, rp->ai_addrlen) == 0) break;
        close(fd);
        fd = DICTUM_NET_INVALID_SOCKET;
    }
    freeaddrinfo(res);
    if (fd == DICTUM_NET_INVALID_SOCKET) dictum_error_set("Net.connect: connection failed");
    return fd;
}

static inline int dictum_net_send(dictum_net_socket_t sock, dictum_text data) {
    if (sock < 0 || !data) return -1;
    size_t len = strlen(data);
    size_t sent_total = 0;
    while (sent_total < len) {
        ssize_t n = send(sock, data + sent_total, len - sent_total, 0);
        if (n <= 0) { dictum_error_set("Net.send: send failed"); return (int)sent_total; }
        sent_total += (size_t)n;
    }
    return (int)sent_total;
}

/* Reads up to 65535 bytes available right now. Returns a heap string
 * (NUL-terminated; safe even for text protocols with embedded content). */
static inline dictum_text dictum_net_receive(dictum_net_socket_t sock) {
    if (sock < 0) return NULL;
    char buf[65536];
    ssize_t n = recv(sock, buf, sizeof(buf) - 1, 0);
    if (n < 0) { dictum_error_set("Net.receive: recv failed"); return NULL; }
    buf[n] = '\0';
    return strdup(buf); /* n==0 => peer closed => returns "" */
}

static inline void dictum_net_close(dictum_net_socket_t sock) {
    if (sock >= 0) close(sock);
}

static inline dictum_net_socket_t dictum_net_listen(int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) { dictum_error_set("Net.listen: socket failed"); return DICTUM_NET_INVALID_SOCKET; }
    int opt = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof opt);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons((uint16_t)port);
    if (bind(fd, (struct sockaddr *)&addr, sizeof addr) != 0) {
        close(fd); dictum_error_set("Net.listen: bind failed"); return DICTUM_NET_INVALID_SOCKET;
    }
    if (listen(fd, 16) != 0) {
        close(fd); dictum_error_set("Net.listen: listen failed"); return DICTUM_NET_INVALID_SOCKET;
    }
    return fd;
}

static inline dictum_net_socket_t dictum_net_accept(dictum_net_socket_t server_sock) {
    if (server_sock < 0) return DICTUM_NET_INVALID_SOCKET;
    int fd = accept(server_sock, NULL, NULL);
    if (fd < 0) dictum_error_set("Net.accept: accept failed");
    return fd;
}

#endif /* DICTUM_NET_H */
