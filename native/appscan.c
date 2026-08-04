/*
 * local-ai-monitor-appscan — N1: proc scan + hard app tags (macOS).
 *
 * Goal: app-level RSS totals within ±10% of Python local_ai_monitor.collect_basic.
 * Uses `ps eww` for argv+env markers (same surface as Python). Tree BFS
 * attributes children under classified roots.
 *
 * Law: observe only. No kill. Self-exclude local-ai-monitor stack.
 * Budget: RSS ≪ Python collector; wall time target ≤20 ms ideal, measure first.
 *
 * Build: make -C native
 */

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#define MAX_PROCS 8192
#define CMD_CAP 4096
#define MAX_APPS 16
#define MAX_CHILDREN 64

typedef struct {
    int pid;
    int ppid;
    double pcpu;
    long rss_kb;
    char cmd[CMD_CAP];
    char base[128];
    char exe[512];
    int app_idx; /* -1 = none */
    int is_root; /* direct classify hit */
} Proc;

typedef struct {
    const char *id;
    long rss_kb;
    double pcpu;
    int nproc;
    int sample_pids[8];
    int nsamples;
} AppAgg;

/* Catalog subset — markers aligned with local_ai_monitor/catalog.py. */
static int str_in_list(const char *s, const char *const *list) {
    for (int i = 0; list[i]; i++) {
        if (strcmp(s, list[i]) == 0)
            return 1;
    }
    return 0;
}

static int contains(const char *hay, const char *needle) {
    return hay && needle && needle[0] && strstr(hay, needle) != NULL;
}

static int self_exclude(const Proc *p) {
    const char *c = p->cmd;
    const char *b = p->base;
    if (strcmp(b, "local-ai-monitor-sensor") == 0 || strcmp(b, "local-ai-monitor-appscan") == 0 ||
        strcmp(b, "local-ai-monitord") == 0 || strcmp(b, "local-ai-monitor-menubar") == 0)
        return 1;
    if (contains(c, "local-ai-monitor-sensor") || contains(c, "local-ai-monitor-appscan") ||
        contains(c, "local-ai-monitord") || contains(c, "local-ai-monitor-menubar"))
        return 1;
    /* Python package */
    if (contains(c, "local_ai_monitor") &&
        (contains(c, "python") || contains(c, "Python") || contains(c, "-m ")))
        return 1;
    if (contains(c, "local-ai-monitor") &&
        (contains(c, "python") || contains(c, "Python") || contains(c, "-m ")))
        return 1;
    return 0;
}

static int buzz_hit(const Proc *p) {
    static const char *const bins[] = {"buzz", "buzz-desktop", "buzz-agent",
                                       "buzz-acp", "buzz-dev-mcp", NULL};
    static const char *const paths[] = {
        "/Applications/Buzz.app/", "Application Support/Buzz/", "/.buzz/",
        "xyz.block.buzz", NULL};
    static const char *const envs[] = {
        "XPC_SERVICE_NAME=com.buzz", "BUZZ_ACP_", "BUZZ_MANAGED_AGENT=",
        "BUZZ_RELAY_URL=", NULL};
    if (str_in_list(p->base, bins))
        return 1;
    for (int i = 0; paths[i]; i++)
        if (contains(p->cmd, paths[i]))
            return 1;
    for (int i = 0; envs[i]; i++)
        if (contains(p->cmd, envs[i]))
            return 1;
    return 0;
}

/* Returns stable tool id or NULL. Buzz checked first. */
static const char *classify_direct(const Proc *p) {
    if (self_exclude(p))
        return NULL;

    if (buzz_hit(p))
        return "Buzz";

    /* Claude Desktop before Claude CLI */
    if (contains(p->cmd, "/Applications/Claude.app/") ||
        contains(p->cmd, "Claude Helper"))
        return "Claude Desktop";

    /* ChatGPT */
    if (strcmp(p->base, "ChatGPT") == 0 ||
        contains(p->cmd, "/Applications/ChatGPT.app/") ||
        contains(p->cmd, "Application Support/com.openai.chat") ||
        contains(p->cmd, "Application Support/OpenAI/"))
        return "ChatGPT";

    /* OpenClaw */
    if (strcmp(p->base, "openclaw") == 0 ||
        contains(p->cmd, "/node_modules/openclaw/") ||
        contains(p->cmd, "/.openclaw/") || contains(p->cmd, "ai.openclaw.") ||
        contains(p->cmd, "OPENCLAW_SERVICE_MARKER=") ||
        contains(p->cmd, "OPENCLAW_GATEWAY") ||
        contains(p->cmd, "XPC_SERVICE_NAME=ai.openclaw"))
        return "OpenClaw";

    /* Codex */
    if (strcmp(p->base, "codex") == 0 || contains(p->exe, "/codex") ||
        contains(p->cmd, "/.codex/packages/") ||
        contains(p->cmd, "/.codex/bin/") ||
        contains(p->cmd, "node_modules/@openai/codex") ||
        contains(p->cmd, "openai-codex") || contains(p->cmd, "OPENAI_CODEX="))
        return "Codex";

    /* Cursor */
    if (strcmp(p->base, "cursor") == 0 || strcmp(p->base, "Cursor") == 0 ||
        contains(p->cmd, "/Applications/Cursor.app/") ||
        contains(p->cmd, "Cursor Helper") || contains(p->cmd, "/.cursor/"))
        return "Cursor";

    /* Grok: binary / env / strict paths only; never match generic project folders. */
    if (strcmp(p->base, "grok") == 0 || contains(p->exe, "/bin/grok") ||
        (strlen(p->exe) >= 5 && strcmp(p->exe + strlen(p->exe) - 5, "/grok") == 0) ||
        contains(p->cmd, "/.grok/sessions/") || contains(p->cmd, "/.grok/bin/") ||
        contains(p->cmd, "GROK_AGENT="))
        return "Grok";

    /* Claude CLI (Anthropic) — not Desktop */
    if (strcmp(p->base, "claude") == 0 ||
        contains(p->cmd, "@anthropic-ai/claude") ||
        contains(p->cmd, "CLAUDE_CODE"))
        return "Claude CLI";

    /* OpenAI CLI */
    if (strcmp(p->base, "openai") == 0 || contains(p->cmd, "/.openai/") ||
        contains(p->cmd, "node_modules/openai/bin"))
        return "OpenAI CLI";

    return NULL;
}

static void fill_exe_base(Proc *p) {
    p->exe[0] = '\0';
    p->base[0] = '\0';
    /* first token of cmd is executable path */
    const char *s = p->cmd;
    while (*s == ' ' || *s == '\t')
        s++;
    size_t i = 0;
    while (s[i] && s[i] != ' ' && s[i] != '\t' && i + 1 < sizeof(p->exe)) {
        p->exe[i] = s[i];
        i++;
    }
    p->exe[i] = '\0';
    const char *slash = strrchr(p->exe, '/');
    const char *b = slash ? slash + 1 : p->exe;
    snprintf(p->base, sizeof(p->base), "%s", b);
}

static int run_ps(Proc *procs, int cap) {
    FILE *fp = popen("ps eww -axo pid=,ppid=,pcpu=,rss=,command=", "r");
    if (!fp)
        return -1;
    char line[CMD_CAP + 128];
    int n = 0;
    while (fgets(line, sizeof(line), fp) && n < cap) {
        char *p = line;
        while (*p == ' ' || *p == '\t')
            p++;
        if (!*p)
            continue;
        char *end = NULL;
        long pid = strtol(p, &end, 10);
        if (end == p)
            continue;
        p = end;
        while (*p == ' ' || *p == '\t')
            p++;
        long ppid = strtol(p, &end, 10);
        if (end == p)
            continue;
        p = end;
        while (*p == ' ' || *p == '\t')
            p++;
        double pcpu = strtod(p, &end);
        if (end == p)
            continue;
        p = end;
        while (*p == ' ' || *p == '\t')
            p++;
        long rss = strtol(p, &end, 10);
        if (end == p)
            continue;
        p = end;
        while (*p == ' ' || *p == '\t')
            p++;
        /* strip trailing newline */
        size_t len = strlen(p);
        while (len > 0 && (p[len - 1] == '\n' || p[len - 1] == '\r'))
            p[--len] = '\0';

        Proc *pr = &procs[n];
        memset(pr, 0, sizeof(*pr));
        pr->pid = (int)pid;
        pr->ppid = (int)ppid;
        pr->pcpu = pcpu;
        pr->rss_kb = rss;
        pr->app_idx = -1;
        snprintf(pr->cmd, sizeof(pr->cmd), "%s", p);
        fill_exe_base(pr);
        n++;
    }
    pclose(fp);
    return n;
}

static int app_index(AppAgg *apps, int *napp, const char *id) {
    for (int i = 0; i < *napp; i++) {
        if (strcmp(apps[i].id, id) == 0)
            return i;
    }
    if (*napp >= MAX_APPS)
        return -1;
    int i = (*napp)++;
    apps[i].id = id;
    apps[i].rss_kb = 0;
    apps[i].pcpu = 0;
    apps[i].nproc = 0;
    apps[i].nsamples = 0;
    return i;
}

static void attribute_trees(Proc *procs, int n, AppAgg *apps, int *napp) {
    /* map pid -> index */
    /* linear scan is fine at 8k */
    for (int i = 0; i < n; i++) {
        const char *id = classify_direct(&procs[i]);
        if (!id)
            continue;
        int ai = app_index(apps, napp, id);
        if (ai < 0)
            continue;
        procs[i].app_idx = ai;
        procs[i].is_root = 1;
    }

    /* BFS from roots: children inherit parent app if unclassified */
    int changed = 1;
    while (changed) {
        changed = 0;
        for (int i = 0; i < n; i++) {
            if (procs[i].app_idx >= 0)
                continue;
            int parent = procs[i].ppid;
            for (int j = 0; j < n; j++) {
                if (procs[j].pid != parent)
                    continue;
                if (procs[j].app_idx >= 0) {
                    procs[i].app_idx = procs[j].app_idx;
                    changed = 1;
                }
                break;
            }
        }
    }

    for (int i = 0; i < n; i++) {
        int ai = procs[i].app_idx;
        if (ai < 0)
            continue;
        apps[ai].rss_kb += procs[i].rss_kb;
        apps[ai].pcpu += procs[i].pcpu;
        apps[ai].nproc += 1;
        if (apps[ai].nsamples < 8) {
            apps[ai].sample_pids[apps[ai].nsamples++] = procs[i].pid;
        }
    }
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
    fsync(fd);
    close(fd);
    if (rename(tmp, path) != 0) {
        unlink(tmp);
        return -1;
    }
    return 0;
}

static void ensure_parent_dir(const char *path) {
    char dir[512];
    snprintf(dir, sizeof(dir), "%s", path);
    char *slash = strrchr(dir, '/');
    if (!slash)
        return;
    *slash = '\0';
    char cmd[600];
    snprintf(cmd, sizeof(cmd), "mkdir -p '%s' 2>/dev/null", dir);
    system(cmd);
}

static void iso_local(char *buf, size_t n) {
    time_t t = time(NULL);
    struct tm tm;
    localtime_r(&t, &tm);
    strftime(buf, n, "%Y-%m-%dT%H:%M:%S%z", &tm);
}

static double now_ms(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (double)tv.tv_sec * 1000.0 + (double)tv.tv_usec / 1000.0;
}

static int cmp_rss_desc(const void *a, const void *b) {
    const AppAgg *x = a, *y = b;
    if (y->rss_kb > x->rss_kb)
        return 1;
    if (y->rss_kb < x->rss_kb)
        return -1;
    return strcmp(x->id, y->id);
}

int main(int argc, char **argv) {
    const char *out_path = NULL;
    int quiet = 0;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--out") == 0 && i + 1 < argc)
            out_path = argv[++i];
        else if (strcmp(argv[i], "-q") == 0 || strcmp(argv[i], "--quiet") == 0)
            quiet = 1;
        else if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            fprintf(stderr,
                    "usage: local-ai-monitor-appscan [--out path] [-q]\n"
                    "  default out: $LOCAL_AI_MONITOR_STATE/apps.json or\n"
                    "               ~/.local/state/local-ai-monitor-native/apps.json\n");
            return 0;
        }
    }

    char default_path[512];
    if (!out_path) {
        const char *st = getenv("LOCAL_AI_MONITOR_STATE");
        const char *home = getenv("HOME");
        if (!home)
            home = ".";
        if (st && st[0]) {
            snprintf(default_path, sizeof(default_path), "%s/apps.json", st);
        } else {
            snprintf(default_path, sizeof(default_path),
                     "%s/.local/state/local-ai-monitor-native/apps.json", home);
        }
        out_path = default_path;
    }

    double t0 = now_ms();
    Proc *procs = calloc(MAX_PROCS, sizeof(Proc));
    if (!procs) {
        fprintf(stderr, "local-ai-monitor-appscan: oom\n");
        return 1;
    }
    int n = run_ps(procs, MAX_PROCS);
    if (n < 0) {
        fprintf(stderr, "local-ai-monitor-appscan: ps failed: %s\n", strerror(errno));
        free(procs);
        return 1;
    }

    AppAgg apps[MAX_APPS];
    int napp = 0;
    memset(apps, 0, sizeof(apps));
    attribute_trees(procs, n, apps, &napp);
    qsort(apps, (size_t)napp, sizeof(AppAgg), cmp_rss_desc);

    long total_rss = 0;
    double total_cpu = 0;
    int total_nproc = 0;
    for (int i = 0; i < napp; i++) {
        total_rss += apps[i].rss_kb;
        total_cpu += apps[i].pcpu;
        total_nproc += apps[i].nproc;
    }
    double wall_ms = now_ms() - t0;

    char ts[64];
    iso_local(ts, sizeof(ts));

    /* JSON body — no full cmd */
    char *body = malloc(16384);
    if (!body) {
        free(procs);
        return 1;
    }
    size_t off = 0;
    int m = snprintf(body + off, 16384 - off,
                     "{\n"
                     "  \"version\": 1,\n"
                     "  \"source\": \"local-ai-monitor-appscan\",\n"
                     "  \"slice\": \"N1\",\n"
                     "  \"ts\": \"%s\",\n"
                     "  \"ok\": true,\n"
                     "  \"wall_ms\": %.2f,\n"
                     "  \"scanned_procs\": %d,\n"
                     "  \"totals\": {\"rss_kb\": %ld, \"cpu_pct\": %.2f, "
                     "\"nproc\": %d, \"napps\": %d},\n"
                     "  \"apps\": [\n",
                     ts, wall_ms, n, total_rss, total_cpu, total_nproc, napp);
    if (m < 0) {
        free(body);
        free(procs);
        return 1;
    }
    off += (size_t)m;

    for (int i = 0; i < napp; i++) {
        m = snprintf(body + off, 16384 - off,
                     "    {\"id\": \"%s\", \"rss_kb\": %ld, \"rss_mb\": %.1f, "
                     "\"cpu_pct\": %.2f, \"nproc\": %d}%s\n",
                     apps[i].id, apps[i].rss_kb, apps[i].rss_kb / 1024.0,
                     apps[i].pcpu, apps[i].nproc, i + 1 < napp ? "," : "");
        if (m < 0 || off + (size_t)m >= 16384)
            break;
        off += (size_t)m;
    }
    m = snprintf(body + off, 16384 - off, "  ]\n}\n");
    if (m > 0)
        off += (size_t)m;

    ensure_parent_dir(out_path);
    if (write_atomic(out_path, body) != 0) {
        fprintf(stderr, "local-ai-monitor-appscan: write failed: %s (%s)\n", out_path,
                strerror(errno));
        free(body);
        free(procs);
        return 1;
    }

    if (!quiet) {
        printf("%s wall_ms=%.2f scanned=%d apps=%d total_rss_mb=%.1f\n", out_path,
               wall_ms, n, napp, total_rss / 1024.0);
        for (int i = 0; i < napp; i++) {
            printf("  %-16s nproc=%3d rss_mb=%7.1f cpu=%.1f\n", apps[i].id,
                   apps[i].nproc, apps[i].rss_kb / 1024.0, apps[i].pcpu);
        }
    }

    free(body);
    free(procs);
    return 0;
}
