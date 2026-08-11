/*
 * local-ai-monitor-sensor — minimal always-on physics probe (macOS).
 *
 * Budget target: << Python collector RSS. libc + sysctl only.
 * Writes: ~/.local/state/local-ai-monitor/physics.json (atomic rename).
 *
 * Product law (2026-07-30): band is reclaimable *headroom*, not free pages alone.
 * free_pages remains diagnostic; primary band uses free+speculative+purgeable
 * + fraction of pageable external (file cache proxy).
 *
 * Build:
 *   cc -O2 -Wall -Wextra -o native/dist/local-ai-monitor-sensor native/sensor.c
 *
 * Law: observe only. Does not kill. Manage/reclaim stays in later C slices
 * or Python until N3.
 */

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/sysctl.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

/* Headroom floors (MB) — align with Python headroom.py defaults for 8 GB. */
#define HEADROOM_OK_MB 1500.0
#define HEADROOM_WARN_MB 600.0
#define FILE_BACKED_RECLAIM 0.75

static double clamp_double(double v, double lo, double hi) {
    if (v < lo)
        return lo;
    if (v > hi)
        return hi;
    return v;
}

static double scaled_headroom_ok_mb(unsigned long long memsize_bytes) {
    if (memsize_bytes == 0)
        return HEADROOM_OK_MB;
    double mem_mb = (double)memsize_bytes / (1024.0 * 1024.0);
    return clamp_double(mem_mb * 0.125, 1500.0, 4096.0);
}

static double scaled_headroom_warn_mb(unsigned long long memsize_bytes) {
    if (memsize_bytes == 0)
        return HEADROOM_WARN_MB;
    double mem_mb = (double)memsize_bytes / (1024.0 * 1024.0);
    double ok_mb = scaled_headroom_ok_mb(memsize_bytes);
    double warn_mb = clamp_double(mem_mb * 0.040, 600.0, 1400.0);
    if (warn_mb >= ok_mb)
        warn_mb = clamp_double(ok_mb * 0.40, 600.0, 1400.0);
    return warn_mb;
}

static int sysctl_int(const char *name, int *out) {
    size_t len = sizeof(int);
    if (sysctlbyname(name, out, &len, NULL, 0) != 0)
        return -1;
    return 0;
}

static int sysctl_ull(const char *name, unsigned long long *out) {
    size_t len = sizeof(unsigned long long);
    if (sysctlbyname(name, out, &len, NULL, 0) != 0) {
        /* try int */
        int v = 0;
        if (sysctl_int(name, &v) != 0)
            return -1;
        *out = (unsigned long long)v;
        return 0;
    }
    return 0;
}

/* Headroom band — never free-page waterlines alone. */
static const char *band_for_headroom(double headroom_mb, int physics_ok,
                                     double ok_mb, double warn_mb) {
    if (!physics_ok || headroom_mb < 0)
        return "unknown";
    if (headroom_mb < warn_mb)
        return "hard";
    if (headroom_mb < ok_mb)
        return "warn";
    return "ok";
}

static int write_atomic(const char *path, const char *body) {
    char tmp[1024];
    int n = snprintf(tmp, sizeof(tmp), "%s.tmp.%d", path, (int)getpid());
    if (n <= 0 || (size_t)n >= sizeof(tmp))
        return -1;
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0)
        return -1;
    size_t len = strlen(body);
    ssize_t w = write(fd, body, len);
    if (w < 0 || (size_t)w != len) {
        close(fd);
        unlink(tmp);
        return -1;
    }
    if (fsync(fd) != 0) {
        /* non-fatal on some FS */
    }
    close(fd);
    if (rename(tmp, path) != 0) {
        unlink(tmp);
        return -1;
    }
    return 0;
}

static void iso_local(char *buf, size_t n) {
    time_t t = time(NULL);
    struct tm tm;
    localtime_r(&t, &tm);
    strftime(buf, n, "%Y-%m-%dT%H:%M:%S%z", &tm);
}

int main(int argc, char **argv) {
    int loop = 0;
    int interval = 0;
    const char *out_path = NULL;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--loop") == 0)
            loop = 1;
        else if (strcmp(argv[i], "--interval") == 0 && i + 1 < argc)
            interval = atoi(argv[++i]);
        else if (strcmp(argv[i], "--out") == 0 && i + 1 < argc)
            out_path = argv[++i];
        else if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            fprintf(stderr,
                    "usage: local-ai-monitor-sensor [--loop] [--interval N] [--out path]\n"
                    "  default out: $HOME/.local/state/local-ai-monitor/physics.json\n"
                    "  band = headroom (not free-page waterlines)\n");
            return 0;
        }
    }
    if (interval <= 0)
        interval = 10;

    char default_path[512];
    if (!out_path) {
        const char *home = getenv("HOME");
        if (!home)
            home = ".";
        snprintf(default_path, sizeof(default_path),
                 "%s/.local/state/local-ai-monitor/physics.json", home);
        out_path = default_path;
    }

    do {
        int free_pages = -1;
        int speculative_pages = -1;
        int purgeable_pages = -1;
        int external_pages = -1; /* pageable external ≈ file-backed cache */
        int page_size = 0;
        unsigned long long memsize = 0;
        int pressure = -1;
        double load[3] = {0, 0, 0};

        if (sysctl_int("vm.page_free_count", &free_pages) != 0)
            free_pages = -1;
        if (sysctl_int("vm.page_speculative_count", &speculative_pages) != 0)
            speculative_pages = 0;
        if (sysctl_int("vm.page_purgeable_count", &purgeable_pages) != 0)
            purgeable_pages = 0;
        if (sysctl_int("vm.page_pageable_external_count", &external_pages) != 0)
            external_pages = 0;
        if (sysctl_int("hw.pagesize", &page_size) != 0)
            page_size = 16384;
        if (sysctl_ull("hw.memsize", &memsize) != 0)
            memsize = 0;
        if (sysctl_int("vm.memory_pressure", &pressure) != 0)
            pressure = -1;
        getloadavg(load, 3);

        double free_mb = -1.0;
        double headroom_mb = -1.0;
        double cheap_mb = -1.0;
        double file_mb = 0.0;
        int physics_ok = (free_pages >= 0 && page_size > 0);

        if (physics_ok) {
            double page_mb = (double)page_size / (1024.0 * 1024.0);
            free_mb = (double)free_pages * page_mb;
            double spec_mb =
                (speculative_pages > 0 ? (double)speculative_pages : 0.0) * page_mb;
            double purg_mb =
                (purgeable_pages > 0 ? (double)purgeable_pages : 0.0) * page_mb;
            file_mb = (external_pages > 0 ? (double)external_pages : 0.0) * page_mb;
            cheap_mb = free_mb + spec_mb + purg_mb;
            headroom_mb = cheap_mb + file_mb * FILE_BACKED_RECLAIM;
        }

        double ok_mb = scaled_headroom_ok_mb(memsize);
        double warn_mb = scaled_headroom_warn_mb(memsize);
        const char *band = band_for_headroom(headroom_mb, physics_ok, ok_mb, warn_mb);
        char ts[64];
        iso_local(ts, sizeof(ts));

        char body[1536];
        snprintf(body, sizeof(body),
                 "{\n"
                 "  \"version\": 2,\n"
                 "  \"source\": \"local-ai-monitor-sensor\",\n"
                 "  \"ts\": \"%s\",\n"
                 "  \"ok\": %s,\n"
                 "  \"page_size\": %d,\n"
                 "  \"free_pages\": %d,\n"
                 "  \"free_mb\": %.1f,\n"
                 "  \"speculative_pages\": %d,\n"
                 "  \"purgeable_pages\": %d,\n"
                 "  \"pageable_external_pages\": %d,\n"
                 "  \"cheap_mb\": %.1f,\n"
                 "  \"file_backed_mb\": %.1f,\n"
                 "  \"headroom_mb\": %.1f,\n"
                 "  \"memsize_bytes\": %llu,\n"
                 "  \"memory_pressure_raw\": %d,\n"
                 "  \"load_1\": %.3f,\n"
                 "  \"band\": \"%s\",\n"
                 "  \"band_source\": \"headroom\",\n"
                 "  \"headroom_ok_mb\": %.0f,\n"
                 "  \"headroom_warn_mb\": %.0f\n"
                 "}\n",
                 ts,
                 physics_ok ? "true" : "false",
                 page_size,
                 free_pages,
                 free_mb,
                 speculative_pages > 0 ? speculative_pages : 0,
                 purgeable_pages > 0 ? purgeable_pages : 0,
                 external_pages > 0 ? external_pages : 0,
                 cheap_mb,
                 file_mb,
                 headroom_mb,
                 (unsigned long long)memsize,
                 pressure,
                 load[0],
                 band,
                 ok_mb,
                 warn_mb);

        /* ensure parent dir exists (best-effort) */
        {
            char dir[512];
            snprintf(dir, sizeof(dir), "%s", out_path);
            char *slash = strrchr(dir, '/');
            if (slash) {
                *slash = '\0';
                char cmd[600];
                snprintf(cmd, sizeof(cmd), "mkdir -p '%s' 2>/dev/null", dir);
                system(cmd);
            }
        }

        if (write_atomic(out_path, body) != 0) {
            fprintf(stderr, "local-ai-monitor-sensor: write failed: %s (%s)\n", out_path,
                    strerror(errno));
            if (!loop)
                return 1;
        } else if (!loop) {
            printf("%s band=%s headroom_mb=%.1f free_mb=%.1f (free_pages diagnostic only)\n",
                   out_path, band, headroom_mb, free_mb);
        }

        if (loop)
            sleep((unsigned)interval);
    } while (loop);

    return 0;
}
