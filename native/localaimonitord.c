/*
 * local-ai-monitord — N2 observe + N3 optional idle soft-stop (one-shot or --loop).
 *
 * Lab default state: $LOCAL_AI_MONITOR_STATE or ~/.local/state/local-ai-monitor-native/
 *   live.min.json  — LiveSnap-compatible sessions + resource + totals
 *   physics.json   — headroom band (not free-page waterlines)
 *   soft-stop-audit.jsonl — N3 actions (append)
 *
 * Never writes production ~/.local/state/local-ai-monitor/live.json unless --state says so.
 *
 * N3 laws (L4 /  / ):
 *   - Default observe-only. Act only with --act.
 *   - Default --act requires band=hard (override: --act-on-warn).
 *   - Idle catalog AI sessions only — never active/open mid-stream.
 *   - OpenClaw: launchctl bootout first, then SIGTERM pids.
 *   - N3 C path: catalog AI only (OpenClaw bootout + idle AI SIGTERM).
 *   - Apple-app relief (Safari/Chrome/…) is BY DESIGN in Python local-ai-rm under hard
 *     freeze-risk / thin headroom when no idle AI candidate — GB floor, graceful quit.
 *     C does not reimplement apple_relief yet; never Finder/WindowServer either way.
 *   - Self-exclude; no secrets in audit (app, session_id, pids, signals only).
 *   - --dry-run logs would_act without signals.
 *   - Reclaim floors are GB-scale (0.5 GiB+), not tens of MB.
 */

#include <errno.h>
#include <fcntl.h>
#include <libproc.h>
#include <mach/mach.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/sysctl.h>
#include <sys/time.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

/*
 * Memory budget (always-on target ≤5 MB RSS total for the control plane):
 *   1024 procs × ~2 KB cmd ≈ 2 MB peak for scan buffers (reused across loops).
 *   Never 8192×4 KB (that alone was ~32 MB and made the “lite” daemon heavy).
 */
#define MAX_PROCS 1024
#define CMD_CAP 1536
#define MAX_SESS 128
#define MAX_PIDS_PER_SESS 64
#define JSON_CAP (96 * 1024)
#define ACTIVE_CPU 2.5
#define IDLE_CPU 0.8
/* Legacy free-page knobs kept only for env override / audit fields — NOT band. */
#define WARN_PAGES_DEFAULT 8000
#define HARD_PAGES_DEFAULT 4000
/* Headroom floors (MB) — product band (align with Python headroom.py). */
#define HEADROOM_OK_MB 1500.0
#define HEADROOM_WARN_MB 600.0
#define FILE_BACKED_RECLAIM 0.75
/* Swap/pageout activity is the beachball signal; static swap-used is context. */
#define THRASH_SWAP_WEIGHT 2.0
#define THRASH_PAGEOUT_WEIGHT 0.05
#define THRASH_WARN 8.0
#define THRASH_HARD 25.0
#define MIN_THRASH_RATE_DT_S 15.0
/* GB-scale reclaim floor: ignore sub-0.5 GiB sessions (product law 2026-07-30). */
#define MIN_RECLAIM_RSS_KB (512 * 1024)
#define HEAVY_RSS_FLOOR_KB (1024 * 1024)
#define HEAVY_RSS_CEILING_KB (4096 * 1024)
#define HEAVY_RSS_MEM_FRACTION 0.08
#define SOFT_STOP_GRACE_S 2
#define ACT_COOLDOWN_S 60
#define NOTIFY_COOLDOWN_S 120

static int g_warn_pages = WARN_PAGES_DEFAULT;
static int g_hard_pages = HARD_PAGES_DEFAULT;
static double g_headroom_ok_mb = HEADROOM_OK_MB;
static double g_headroom_warn_mb = HEADROOM_WARN_MB;

static double clamp_double(double v, double lo, double hi) {
    if (v < lo)
        return lo;
    if (v > hi)
        return hi;
    return v;
}

typedef struct {
    int enable;       /* --act */
    int dry_run;      /* --dry-run */
    int on_warn;      /* --act-on-warn (default hard only) */
    int notify;       /* --notify macOS banner on warn/hard */
    time_t last_act;  /* cooldown */
    time_t last_notify;
    char last_notify_band[16];
} ActOpts;

static void ensure_parent_dir(const char *path);
static void iso_local(char *buf, size_t n);
static const char *activity_for(double cpu);
static int str_eq(const char *a, const char *b);
static int contains(const char *hay, const char *needle);

typedef struct {
    int pid;
    int ppid;
    double pcpu;
    long rss_kb;
    char cmd[CMD_CAP];
    char base[128];
    char exe[512];
    int app_idx;
    int sess_idx;
    int direct;
    int is_root;
} Proc;

typedef struct {
    char app[32];
    char session_id[96];
    char label[128];
    char detail[256];
    char kind[16];
    long rss_kb;
    double pcpu;
    int nproc;
    int pids[MAX_PIDS_PER_SESS];
    int npids;
} Sess;

typedef struct {
    int ok;
    unsigned long long total_bytes;
    unsigned long long used_bytes;
    unsigned long long avail_bytes;
} SwapUsage;

typedef struct {
    int ok;
    unsigned long long pageouts;
    unsigned long long swapins;
    unsigned long long swapouts;
    unsigned long long compressor_pages;
} VmCounters;

/* Reused across --loop iterations (avoids multi‑MB calloc every sample). */
static Proc *g_procs;
static Sess *g_sess;
static char *g_json;
static int g_pool_ready;
static VmCounters g_prev_vm;
static double g_prev_vm_ms;
static int g_prev_vm_ready;
static double g_profile_first_ms;
static double g_learn_ai_rss_mb;
static double g_learn_ai_mem_pct;
static double g_learn_cpu_capacity_pct;
static double g_learn_thrash_score;

static int pool_init(void) {
    if (g_pool_ready)
        return 0;
    g_procs = calloc(MAX_PROCS, sizeof(Proc));
    g_sess = calloc(MAX_SESS, sizeof(Sess));
    g_json = malloc(JSON_CAP);
    if (!g_procs || !g_sess || !g_json)
        return -1;
    g_pool_ready = 1;
    return 0;
}

static const char *APP_NAMES[] = {
    "Claude Desktop", "ChatGPT", "Buzz", "OpenClaw", "Codex",
    "OpenAI CLI",     "Cursor",  "Grok", "Claude CLI", NULL};

static int app_name_index(const char *id) {
    for (int i = 0; APP_NAMES[i]; i++)
        if (strcmp(APP_NAMES[i], id) == 0)
            return i;
    return -1;
}

static int contains(const char *hay, const char *needle) {
    return hay && needle && needle[0] && strstr(hay, needle) != NULL;
}

static int str_eq(const char *a, const char *b) {
    return a && b && strcmp(a, b) == 0;
}

/* Argv only — stop at first KEY=value (ps eww env tail). /self_filter. */
static void argv_portion(const char *cmd, char *out, size_t outn) {
    size_t o = 0;
    const char *s = cmd;
    while (*s && o + 1 < outn) {
        /* token */
        while (*s == ' ' || *s == '\t')
            s++;
        if (!*s)
            break;
        const char *t = s;
        while (*s && *s != ' ' && *s != '\t')
            s++;
        size_t tl = (size_t)(s - t);
        /* env token KEY= */
        int is_env = 0;
        if (tl > 1) {
            int eq = -1;
            for (size_t i = 0; i < tl; i++) {
                if (t[i] == '=') {
                    eq = (int)i;
                    break;
                }
                if (!((t[i] >= 'A' && t[i] <= 'Z') || (t[i] >= 'a' && t[i] <= 'z') ||
                      (t[i] >= '0' && t[i] <= '9') || t[i] == '_')) {
                    eq = -2;
                    break;
                }
            }
            if (eq > 0)
                is_env = 1;
        }
        if (is_env)
            break;
        if (o && o + 1 < outn)
            out[o++] = ' ';
        for (size_t i = 0; i < tl && o + 1 < outn; i++)
            out[o++] = t[i];
    }
    out[o] = '\0';
}

static int is_self_pattern(const Proc *p) {
    char argv[CMD_CAP];
    argv_portion(p->cmd, argv, sizeof(argv));
    const char *b = p->base;
    if (str_eq(b, "local-ai-monitor-sensor") || str_eq(b, "local-ai-monitor-appscan") || str_eq(b, "local-ai-monitord") ||
        str_eq(b, "local-ai-monitor-menubar") || str_eq(b, "local-ai-monitor"))
        return 1;
    if (contains(argv, "local-ai-monitor-sensor") || contains(argv, "local-ai-monitor-appscan") ||
        contains(argv, "local-ai-monitord") || contains(argv, "local-ai-monitor-menubar"))
        return 1;
    if (contains(argv, "local_ai_monitor") &&
        (contains(argv, "python") || contains(argv, "Python") || contains(argv, "-m")))
        return 1;
    if (contains(argv, "local-ai-monitor") &&
        (contains(argv, "python") || contains(argv, "Python") || contains(argv, "-m")))
        return 1;
    return 0;
}

/* Drop monitor stack: own pid, self patterns, their descendants, immediate parent. */
static int filter_self_inplace(Proc *procs, int n) {
    char *drop = calloc((size_t)n, 1);
    if (!drop)
        return n;
    int me = (int)getpid();
    int parent = (int)getppid();
    for (int i = 0; i < n; i++) {
        if (procs[i].pid == me || is_self_pattern(&procs[i]))
            drop[i] = 1;
    }
    if (parent > 1) {
        for (int i = 0; i < n; i++)
            if (procs[i].pid == parent)
                drop[i] = 1;
    }
    /* expand descendants of seeds */
    int changed = 1;
    while (changed) {
        changed = 0;
        for (int i = 0; i < n; i++) {
            if (drop[i])
                continue;
            for (int j = 0; j < n; j++) {
                if (!drop[j])
                    continue;
                if (procs[i].ppid == procs[j].pid) {
                    drop[i] = 1;
                    changed = 1;
                    break;
                }
            }
        }
    }
    int w = 0;
    for (int i = 0; i < n; i++) {
        if (drop[i])
            continue;
        if (w != i)
            procs[w] = procs[i];
        w++;
    }
    free(drop);
    return w;
}

static int self_exclude(const Proc *p) { return is_self_pattern(p); }

static int buzz_hit(const Proc *p) {
    static const char *const bins[] = {"buzz", "buzz-desktop", "buzz-agent",
                                       "buzz-acp", "buzz-dev-mcp", NULL};
    if (str_eq(p->base, "ngrok") && contains(p->cmd, "/.buzz/"))
        return 1;
    for (int i = 0; bins[i]; i++)
        if (str_eq(p->base, bins[i]))
            return 1;
    if (contains(p->cmd, "/Applications/Buzz.app/") ||
        contains(p->cmd, "Application Support/Buzz/") || contains(p->cmd, "/.buzz/") ||
        contains(p->cmd, "xyz.block.buzz") || contains(p->cmd, "XPC_SERVICE_NAME=com.buzz") ||
        contains(p->cmd, "BUZZ_ACP_") || contains(p->cmd, "BUZZ_MANAGED_AGENT=") ||
        contains(p->cmd, "BUZZ_RELAY_URL="))
        return 1;
    return 0;
}

static const char *classify_direct(const Proc *p) {
    if (self_exclude(p))
        return NULL;
    if (buzz_hit(p))
        return "Buzz";
    if (contains(p->cmd, "/Applications/Claude.app/") || contains(p->cmd, "Claude Helper"))
        return "Claude Desktop";
    if (str_eq(p->base, "ChatGPT") || contains(p->cmd, "/Applications/ChatGPT.app/") ||
        contains(p->cmd, "Application Support/com.openai.chat") ||
        contains(p->cmd, "Application Support/OpenAI/"))
        return "ChatGPT";
    if (str_eq(p->base, "openclaw") || contains(p->cmd, "/node_modules/openclaw/") ||
        contains(p->cmd, "/.openclaw/") || contains(p->cmd, "ai.openclaw.") ||
        contains(p->cmd, "OPENCLAW_SERVICE_MARKER=") || contains(p->cmd, "OPENCLAW_GATEWAY") ||
        contains(p->cmd, "XPC_SERVICE_NAME=ai.openclaw"))
        return "OpenClaw";
    if (str_eq(p->base, "codex") || contains(p->exe, "/codex") ||
        contains(p->cmd, "/.codex/packages/") || contains(p->cmd, "/.codex/bin/") ||
        contains(p->cmd, "node_modules/@openai/codex") || contains(p->cmd, "openai-codex") ||
        contains(p->cmd, "OPENAI_CODEX="))
        return "Codex";
    if (str_eq(p->base, "cursor") || str_eq(p->base, "Cursor") ||
        contains(p->cmd, "/Applications/Cursor.app/") || contains(p->cmd, "Cursor Helper") ||
        contains(p->cmd, "/.cursor/"))
        return "Cursor";
    if (str_eq(p->base, "grok") || contains(p->exe, "/bin/grok") ||
        (strlen(p->exe) >= 5 && strcmp(p->exe + strlen(p->exe) - 5, "/grok") == 0) ||
        contains(p->cmd, "/.grok/sessions/") || contains(p->cmd, "/.grok/bin/") ||
        contains(p->cmd, "GROK_AGENT="))
        return "Grok";
    if (str_eq(p->base, "claude") || contains(p->cmd, "@anthropic-ai/claude") ||
        contains(p->cmd, "CLAUDE_CODE"))
        return "Claude CLI";
    if (str_eq(p->base, "openai") || contains(p->cmd, "/.openai/") ||
        contains(p->cmd, "node_modules/openai/bin"))
        return "OpenAI CLI";
    return NULL;
}

static void fill_exe_base(Proc *p) {
    p->exe[0] = '\0';
    p->base[0] = '\0';
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
    snprintf(p->base, sizeof(p->base), "%s", slash ? slash + 1 : p->exe);
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
        pr->sess_idx = -1;
        snprintf(pr->cmd, sizeof(pr->cmd), "%s", p);
        fill_exe_base(pr);
        n++;
    }
    pclose(fp);
    return n;
}

static int is_session_root(const Proc *p) {
    if (p->app_idx < 0 || !p->direct)
        return 0;
    const char *app = APP_NAMES[p->app_idx];
    if (str_eq(app, "Grok"))
        return str_eq(p->base, "grok") || contains(p->exe, "/grok");
    if (str_eq(app, "Claude CLI"))
        return (str_eq(p->base, "claude") || contains(p->exe, "/claude")) &&
               !contains(p->cmd, "/Applications/Claude.app/");
    if (str_eq(app, "Buzz")) {
        if (str_eq(p->base, "zsh") || str_eq(p->base, "bash") || str_eq(p->base, "sh")) {
            return contains(p->cmd, "BUZZ_ACP_SESSION_TITLE=") ||
                   contains(p->cmd, "buzz_channel_trace") ||
                   contains(p->cmd, "XPC_SERVICE_NAME=com.buzz");
        }
        if (contains(p->cmd, "BUZZ_ACP_SESSION_TITLE=") &&
            !str_eq(p->base, "buzz-acp") &&
            !contains(p->cmd, "XPC_SERVICE_NAME=com.buzz.") &&
            !contains(p->cmd, "/.buzz/PROJECTS/") &&
            !contains(p->cmd, "/.buzz/REPOS/")) {
            return 0;
        }
        return 1;
    }
    if (str_eq(app, "OpenClaw") || str_eq(app, "Claude Desktop") || str_eq(app, "ChatGPT"))
        return 1;
    return p->direct;
}

static int env_get(const char *cmd, const char *key, char *out, size_t outn) {
    char pat[96];
    snprintf(pat, sizeof(pat), "%s=", key);
    const char *found = NULL;
    const char *s = cmd;
    while ((s = strstr(s, pat)) != NULL) {
        if (s == cmd || s[-1] == ' ' || s[-1] == '\t')
            found = s + strlen(pat);
        s += strlen(pat);
    }
    if (!found) {
        out[0] = '\0';
        return 0;
    }
    size_t i = 0;
    while (found[i] && found[i] != ' ' && found[i] != '\t' && i + 1 < outn) {
        out[i] = found[i];
        i++;
    }
    out[i] = '\0';
    return 1;
}

static int extract_resume_uuid(const char *cmd, char *out, size_t outn) {
    const char *p = strstr(cmd, "--resume");
    if (!p)
        return 0;
    p += 8;
    while (*p == ' ' || *p == '\t' || *p == '=')
        p++;
    size_t i = 0;
    while (p[i] && p[i] != ' ' && p[i] != '\t' && i + 1 < outn) {
        char c = p[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F') ||
              c == '-'))
            break;
        out[i] = c;
        i++;
    }
    out[i] = '\0';
    return i >= 20;
}

static int extract_grok_path_uuid(const char *cmd, char *out, size_t outn) {
    const char *p = strstr(cmd, "/.grok/sessions/");
    if (!p)
        return 0;
    p += strlen("/.grok/sessions/");
    while (*p && *p != '/')
        p++;
    if (*p != '/')
        return 0;
    p++;
    size_t i = 0;
    while (p[i] && p[i] != '/' && p[i] != ' ' && i + 1 < outn) {
        out[i] = p[i];
        i++;
    }
    out[i] = '\0';
    return i >= 20;
}

static void basename_path(const char *path, char *out, size_t outn) {
    if (!path || !path[0]) {
        snprintf(out, outn, "unknown");
        return;
    }
    const char *slash = strrchr(path, '/');
    const char *b = slash ? slash + 1 : path;
    if (!b[0])
        snprintf(out, outn, "unknown");
    else
        snprintf(out, outn, "%s", b);
}

/* Reject truncated $HOME/Grok or $HOME/Claude (Python is_trustworthy_pwd). */
static int trustworthy_pwd(const char *pwd) {
    if (!pwd || pwd[0] != '/')
        return 0;
    const char *home = getenv("HOME");
    if (!home || !home[0])
        return 1;
    size_t hl = strlen(home);
    if (strncmp(pwd, home, hl) != 0 || (pwd[hl] != '/' && pwd[hl] != '\0'))
        return 1; /* outside home — accept */
    const char *rel = pwd + hl;
    if (*rel == '/')
        rel++;
    if (!*rel)
        return 0;
    /* single segment under home is untrustworthy for labels */
    if (!strchr(rel, '/'))
        return 0;
    return 1;
}

/* Real cwd via libproc (no lsof). */
static int proc_cwd(int pid, char *out, size_t outn) {
    struct proc_vnodepathinfo vpi;
    memset(&vpi, 0, sizeof(vpi));
    int nb = proc_pidinfo(pid, PROC_PIDVNODEPATHINFO, 0, &vpi, (int)sizeof(vpi));
    if (nb <= 0)
        return 0;
    const char *path = vpi.pvi_cdir.vip_path;
    if (!path || path[0] != '/')
        return 0;
    snprintf(out, outn, "%s", path);
    return 1;
}

/* Prefer libproc cwd; fall back to trusted PWD= from env. */
static void resolve_cwd(int pid, const char *cmd, char *out, size_t outn) {
    out[0] = '\0';
    if (proc_cwd(pid, out, outn) && trustworthy_pwd(out))
        return;
    char pwd[512] = "";
    if (env_get(cmd, "PWD", pwd, sizeof(pwd)) && trustworthy_pwd(pwd)) {
        snprintf(out, outn, "%s", pwd);
        return;
    }
    out[0] = '\0';
}

static void norm_slug(char *s) {
    for (char *p = s; *p; p++) {
        if (*p >= 'A' && *p <= 'Z')
            *p = (char)(*p - 'A' + 'a');
    }
}

static void buzz_identity(const Proc *p, char *sid, size_t sidn, char *label, size_t labn,
                          char *detail, size_t detn) {
    char channel[64] = "unknown";
    char agent[64] = "worker";
    char xpc[128], pwd[512], title[128];

    if (env_get(p->cmd, "XPC_SERVICE_NAME", xpc, sizeof(xpc)) &&
        strncmp(xpc, "com.buzz.", 9) == 0) {
        snprintf(channel, sizeof(channel), "%s", xpc + 9);
        size_t cl = strlen(channel);
        if (cl > 7 && strcmp(channel + cl - 7, "-tunnel") == 0) {
            channel[cl - 7] = '\0';
            snprintf(agent, sizeof(agent), "tunnel");
        }
    }
    const char *markers[] = {"/.buzz/PROJECTS/", "/.buzz/REPOS/", NULL};
    for (int mi = 0; markers[mi]; mi++) {
        const char *bz = strstr(p->cmd, markers[mi]);
        if (!bz)
            continue;
        const char *name = bz + strlen(markers[mi]);
        size_t i = 0;
        while (name[i] && name[i] != '/' && name[i] != ' ' && i + 1 < sizeof(channel)) {
            channel[i] = name[i];
            i++;
        }
        channel[i] = '\0';
        if (strncmp(channel, "buzz-", 5) == 0)
            memmove(channel, channel + 5, strlen(channel + 5) + 1);
        break;
    }
    if (env_get(p->cmd, "PWD", pwd, sizeof(pwd))) {
        for (int mi = 0; markers[mi]; mi++) {
            const char *m = strstr(pwd, markers[mi]);
            if (!m)
                continue;
            const char *name = m + strlen(markers[mi]);
            size_t i = 0;
            while (name[i] && name[i] != '/' && name[i] != ' ' && i + 1 < sizeof(channel)) {
                channel[i] = name[i];
                i++;
            }
            channel[i] = '\0';
            if (strncmp(channel, "buzz-", 5) == 0)
                memmove(channel, channel + 5, strlen(channel + 5) + 1);
            break;
        }
    }
    if (contains(p->cmd, "/Applications/Buzz.app/") && str_eq(channel, "unknown"))
        snprintf(channel, sizeof(channel), "desktop");
    if (str_eq(p->base, "ngrok") || contains(p->exe, "/ngrok"))
        snprintf(agent, sizeof(agent), "tunnel");
    if (env_get(p->cmd, "BUZZ_ACP_SESSION_TITLE", title, sizeof(title)) && title[0])
        snprintf(agent, sizeof(agent), "%s", title);

    norm_slug(channel);
    norm_slug(agent);
    snprintf(sid, sidn, "buzz:%s|%s", channel, agent);
    snprintf(label, labn, "%s · %s", channel, agent);
    snprintf(detail, detn, "channel=%s agent=%s", channel, agent);
}

static int find_or_add_sess(Sess *sess, int *ns, const char *app, const char *sid,
                            const char *label, const char *detail, const char *kind) {
    for (int i = 0; i < *ns; i++) {
        if (strcmp(sess[i].app, app) == 0 && strcmp(sess[i].session_id, sid) == 0)
            return i;
    }
    if (*ns >= MAX_SESS)
        return -1;
    int i = (*ns)++;
    memset(&sess[i], 0, sizeof(sess[i]));
    snprintf(sess[i].app, sizeof(sess[i].app), "%s", app);
    snprintf(sess[i].session_id, sizeof(sess[i].session_id), "%s", sid);
    snprintf(sess[i].label, sizeof(sess[i].label), "%s", label);
    snprintf(sess[i].detail, sizeof(sess[i].detail), "%s", detail);
    snprintf(sess[i].kind, sizeof(sess[i].kind), "%s", kind);
    return i;
}

static void add_pid(Sess *s, int pid) {
    for (int i = 0; i < s->npids; i++)
        if (s->pids[i] == pid)
            return;
    if (s->npids < MAX_PIDS_PER_SESS)
        s->pids[s->npids++] = pid;
    s->nproc++;
}

static void sessionize(Proc *procs, int n, Sess *sess, int *ns) {
    for (int i = 0; i < n; i++) {
        const char *id = classify_direct(&procs[i]);
        if (!id)
            continue;
        int ai = app_name_index(id);
        if (ai < 0)
            continue;
        procs[i].app_idx = ai;
        procs[i].direct = 1;
    }

    int changed = 1;
    while (changed) {
        changed = 0;
        for (int i = 0; i < n; i++) {
            if (procs[i].app_idx >= 0)
                continue;
            for (int j = 0; j < n; j++) {
                if (procs[j].pid != procs[i].ppid)
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
        Proc *p = &procs[i];
        if (!is_session_root(p))
            continue;
        p->is_root = 1;
        const char *app = APP_NAMES[p->app_idx];
        char sid[96], label[128], detail[256], kind[16];
        snprintf(kind, sizeof(kind), "cli");
        detail[0] = '\0';

        if (str_eq(app, "Grok")) {
            char uuid[80] = "";
            if (!extract_resume_uuid(p->cmd, uuid, sizeof(uuid)))
                extract_grok_path_uuid(p->cmd, uuid, sizeof(uuid));
            char cwd[512];
            resolve_cwd(p->pid, p->cmd, cwd, sizeof(cwd));
            char base[64];
            if (cwd[0])
                basename_path(cwd, base, sizeof(base));
            else
                snprintf(base, sizeof(base), "pid%d", p->pid);
            if (uuid[0]) {
                snprintf(sid, sizeof(sid), "%s", uuid);
                snprintf(label, sizeof(label), "%s · %.8s", base, uuid);
                snprintf(detail, sizeof(detail), "%s", cwd);
            } else {
                snprintf(sid, sizeof(sid), "pid:%d", p->pid);
                snprintf(label, sizeof(label), "%s", base);
                snprintf(detail, sizeof(detail), "%s", cwd);
            }
        } else if (str_eq(app, "Buzz")) {
            buzz_identity(p, sid, sizeof(sid), label, sizeof(label), detail, sizeof(detail));
            snprintf(kind, sizeof(kind), "agent");
        } else if (str_eq(app, "OpenClaw")) {
            snprintf(sid, sizeof(sid), "svc:gateway");
            snprintf(label, sizeof(label), "gateway");
            snprintf(kind, sizeof(kind), "service");
        } else if (str_eq(app, "Claude Desktop")) {
            snprintf(sid, sizeof(sid), "app:claude-desktop");
            snprintf(label, sizeof(label), "Claude Desktop");
            snprintf(kind, sizeof(kind), "desktop");
        } else if (str_eq(app, "ChatGPT")) {
            snprintf(sid, sizeof(sid), "app:chatgpt");
            snprintf(label, sizeof(label), "ChatGPT");
            snprintf(kind, sizeof(kind), "desktop");
        } else if (str_eq(app, "Claude CLI")) {
            char cwd[512];
            resolve_cwd(p->pid, p->cmd, cwd, sizeof(cwd));
            char base[64];
            if (cwd[0])
                basename_path(cwd, base, sizeof(base));
            else
                snprintf(base, sizeof(base), "pid%d", p->pid);
            snprintf(sid, sizeof(sid), "pid:%d", p->pid);
            snprintf(label, sizeof(label), "%s", base);
            snprintf(detail, sizeof(detail), "%s", cwd);
        } else {
            char cwd[512];
            resolve_cwd(p->pid, p->cmd, cwd, sizeof(cwd));
            char base[64];
            if (cwd[0])
                basename_path(cwd, base, sizeof(base));
            else
                snprintf(base, sizeof(base), "pid%d", p->pid);
            snprintf(sid, sizeof(sid), "pid:%d", p->pid);
            snprintf(label, sizeof(label), "%s", base);
            snprintf(detail, sizeof(detail), "%s", cwd);
        }

        int si = find_or_add_sess(sess, ns, app, sid, label, detail, kind);
        if (si < 0)
            continue;
        p->sess_idx = si;
        add_pid(&sess[si], p->pid);
        sess[si].rss_kb += p->rss_kb;
        sess[si].pcpu += p->pcpu;
    }

    changed = 1;
    while (changed) {
        changed = 0;
        for (int i = 0; i < n; i++) {
            if (procs[i].app_idx < 0 || procs[i].sess_idx >= 0)
                continue;
            for (int j = 0; j < n; j++) {
                if (procs[j].pid != procs[i].ppid)
                    continue;
                if (procs[j].sess_idx >= 0 && procs[j].app_idx == procs[i].app_idx) {
                    int si = procs[j].sess_idx;
                    procs[i].sess_idx = si;
                    add_pid(&sess[si], procs[i].pid);
                    sess[si].rss_kb += procs[i].rss_kb;
                    sess[si].pcpu += procs[i].pcpu;
                    changed = 1;
                }
                break;
            }
        }
    }

    for (int i = 0; i < n; i++) {
        if (procs[i].app_idx < 0 || procs[i].sess_idx >= 0)
            continue;
        const char *app = APP_NAMES[procs[i].app_idx];
        char sid[96], label[128];
        snprintf(sid, sizeof(sid), "app:%s", app);
        snprintf(label, sizeof(label), "%s", app);
        int si = find_or_add_sess(sess, ns, app, sid, label, "", "unknown");
        if (si < 0)
            continue;
        procs[i].sess_idx = si;
        add_pid(&sess[si], procs[i].pid);
        sess[si].rss_kb += procs[i].rss_kb;
        sess[si].pcpu += procs[i].pcpu;
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

static void append_host_sample(const char *state_dir, const char *host_id, double headroom_mb,
                               double thrash_score, int ai_mb, double ai_mem_pct,
                               double cpu_capacity_pct) {
    char path[512];
    snprintf(path, sizeof(path), "%s/host-profile-samples.jsonl", state_dir);
    ensure_parent_dir(path);
    FILE *f = fopen(path, "a");
    if (!f)
        return;
    char ts[64];
    iso_local(ts, sizeof(ts));
    fprintf(f,
            "{\"ts\":\"%s\",\"host_id\":\"%s\",\"headroom_mb\":%.1f,"
            "\"thrash_score\":%.2f,\"ai_rss_mb\":%d,\"ai_mem_pct\":%.1f,"
            "\"cpu_capacity_pct\":%.1f}\n",
            ts, host_id, headroom_mb, thrash_score, ai_mb, ai_mem_pct, cpu_capacity_pct);
    fclose(f);
}

static void update_learned_capacity(int ai_mb, double ai_mem_pct, double cpu_capacity_pct,
                                    double thrash_score) {
    const double alpha = 0.08;
    if (g_learn_ai_rss_mb <= 0.0) {
        g_learn_ai_rss_mb = (double)ai_mb;
        g_learn_ai_mem_pct = ai_mem_pct;
        g_learn_cpu_capacity_pct = cpu_capacity_pct;
        g_learn_thrash_score = thrash_score;
        return;
    }
    g_learn_ai_rss_mb = g_learn_ai_rss_mb * (1.0 - alpha) + (double)ai_mb * alpha;
    g_learn_ai_mem_pct = g_learn_ai_mem_pct * (1.0 - alpha) + ai_mem_pct * alpha;
    g_learn_cpu_capacity_pct =
        g_learn_cpu_capacity_pct * (1.0 - alpha) + cpu_capacity_pct * alpha;
    g_learn_thrash_score = g_learn_thrash_score * (1.0 - alpha) + thrash_score * alpha;
}

static long host_heavy_rss_kb(unsigned long long memsize) {
    long threshold = HEAVY_RSS_FLOOR_KB;
    if (memsize > 0) {
        double mem_mb = (double)memsize / (1024.0 * 1024.0);
        long scaled = (long)(mem_mb * 1024.0 * HEAVY_RSS_MEM_FRACTION);
        if (scaled > threshold)
            threshold = scaled;
    }
    if (threshold > HEAVY_RSS_CEILING_KB)
        threshold = HEAVY_RSS_CEILING_KB;
    return threshold;
}

static int existing_ready_profile_matches(const char *path, unsigned long long memsize,
                                          int page_size) {
    FILE *f = fopen(path, "r");
    if (!f)
        return 0;
    char buf[2048];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = '\0';
    if (!contains(buf, "\"status\": \"ready\""))
        return 0;
    char needle[96];
    snprintf(needle, sizeof(needle), "\"memsize_bytes\": %llu", memsize);
    if (!contains(buf, needle))
        return 0;
    snprintf(needle, sizeof(needle), "\"page_size\": %d", page_size);
    return contains(buf, needle);
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

/* ISO-8601 local with colon in offset (-05:00). Glass menubar / ISO8601DateFormatter
 * parse this reliably; bare %z (-0500) is flaky and can mark a live feed "stale". */
static void iso_local(char *buf, size_t n) {
    time_t t = time(NULL);
    struct tm tm;
    localtime_r(&t, &tm);
    char base[32];
    strftime(base, sizeof(base), "%Y-%m-%dT%H:%M:%S", &tm);
#if defined(__APPLE__)
    long off = tm.tm_gmtoff;
#else
    long off = 0;
#endif
    int sign = off < 0 ? -1 : 1;
    long a = off < 0 ? -off : off;
    int hh = (int)(a / 3600);
    int mm = (int)((a % 3600) / 60);
    snprintf(buf, n, "%s%c%02d:%02d", base, sign < 0 ? '-' : '+', hh, mm);
}

static double now_ms(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (double)tv.tv_sec * 1000.0 + (double)tv.tv_usec / 1000.0;
}

static int sysctl_int(const char *name, int *out) {
    size_t len = sizeof(int);
    if (sysctlbyname(name, out, &len, NULL, 0) != 0)
        return -1;
    return 0;
}

static int sysctl_ull(const char *name, unsigned long long *out) {
    size_t len = sizeof(unsigned long long);
    if (sysctlbyname(name, out, &len, NULL, 0) != 0)
        return -1;
    return 0;
}

static void scale_headroom_for_memsize(unsigned long long memsize_bytes) {
    if (memsize_bytes == 0) {
        g_headroom_ok_mb = HEADROOM_OK_MB;
        g_headroom_warn_mb = HEADROOM_WARN_MB;
        return;
    }
    double mem_mb = (double)memsize_bytes / (1024.0 * 1024.0);
    g_headroom_ok_mb = clamp_double(mem_mb * 0.125, 1500.0, 4096.0);
    g_headroom_warn_mb = clamp_double(mem_mb * 0.040, 600.0, 1400.0);
    if (g_headroom_warn_mb >= g_headroom_ok_mb)
        g_headroom_warn_mb = clamp_double(g_headroom_ok_mb * 0.40, 600.0, 1400.0);
}

static int sysctl_swapusage(SwapUsage *out) {
    memset(out, 0, sizeof(*out));
    struct xsw_usage xsu;
    size_t len = sizeof(xsu);
    if (sysctlbyname("vm.swapusage", &xsu, &len, NULL, 0) != 0)
        return -1;
    out->ok = 1;
    out->total_bytes = (unsigned long long)xsu.xsu_total;
    out->used_bytes = (unsigned long long)xsu.xsu_used;
    out->avail_bytes = (unsigned long long)xsu.xsu_avail;
    return 0;
}

static int sample_vm_counters(VmCounters *out) {
    memset(out, 0, sizeof(*out));
    vm_statistics64_data_t vm;
    mach_msg_type_number_t count = HOST_VM_INFO64_COUNT;
    kern_return_t kr =
        host_statistics64(mach_host_self(), HOST_VM_INFO64, (host_info64_t)&vm, &count);
    if (kr != KERN_SUCCESS)
        return -1;
    out->ok = 1;
    out->pageouts = (unsigned long long)vm.pageouts;
    out->swapins = (unsigned long long)vm.swapins;
    out->swapouts = (unsigned long long)vm.swapouts;
    out->compressor_pages = (unsigned long long)vm.compressor_page_count;
    return 0;
}

static int jesc(char *dest, size_t destn, const char *src) {
    size_t o = 0;
    if (!src)
        src = "";
    for (const unsigned char *p = (const unsigned char *)src; *p; p++) {
        if (o + 6 >= destn)
            return -1;
        if (*p == '"' || *p == '\\') {
            dest[o++] = '\\';
            dest[o++] = (char)*p;
        } else if (*p == '\n') {
            dest[o++] = '\\';
            dest[o++] = 'n';
        } else if (*p >= 0x20) {
            dest[o++] = (char)*p;
        }
    }
    dest[o] = '\0';
    return (int)o;
}

static int cmp_sess_rss(const void *a, const void *b) {
    const Sess *x = a, *y = b;
    if (y->rss_kb > x->rss_kb)
        return 1;
    if (y->rss_kb < x->rss_kb)
        return -1;
    return 0;
}

static const char *activity_for(double cpu) {
    if (cpu >= ACTIVE_CPU)
        return "active";
    if (cpu < IDLE_CPU)
        return "idle";
    return "open";
}

static double thrash_score_from_deltas(double dt_s, unsigned long long d_swapins,
                                       unsigned long long d_swapouts,
                                       unsigned long long d_pageouts) {
    if (dt_s <= 0)
        return 0.0;
    if (d_pageouts > 20000 || (d_swapins + d_swapouts) > 5000)
        return 0.0;
    double effective_dt = dt_s < MIN_THRASH_RATE_DT_S ? MIN_THRASH_RATE_DT_S : dt_s;
    double scale = 60.0 / effective_dt;
    double swap_r = (double)(d_swapins + d_swapouts) * scale;
    double page_r = (double)d_pageouts * scale;
    if (swap_r > 500.0)
        swap_r = 500.0;
    if (page_r > 5000.0)
        page_r = 5000.0;
    double swap_term = THRASH_SWAP_WEIGHT * swap_r;
    if (swap_term <= 0.0)
        return 0.0;
    return swap_term + THRASH_PAGEOUT_WEIGHT * page_r;
}

/* Product band = reclaimable headroom + swap/pageout activity, never free pages alone. */
static const char *band_for_headroom(double headroom_mb, double thrash_score, int physics_ok) {
    if (!physics_ok || headroom_mb < 0)
        return "unknown";
    if (thrash_score >= THRASH_HARD)
        return "hard";
    if (thrash_score >= THRASH_WARN)
        return headroom_mb < g_headroom_warn_mb ? "hard" : "warn";
    if (headroom_mb < g_headroom_warn_mb)
        return "hard";
    if (headroom_mb < g_headroom_ok_mb)
        return "warn";
    return "ok";
}

static int apf(char *buf, size_t cap, size_t *off, const char *fmt, ...) {
    if (*off >= cap)
        return -1;
    va_list ap;
    va_start(ap, fmt);
    int m = vsnprintf(buf + *off, cap - *off, fmt, ap);
    va_end(ap);
    if (m < 0 || (size_t)m >= cap - *off)
        return -1;
    *off += (size_t)m;
    return 0;
}

static void fmt_mb(char *out, size_t outn, int mb) {
    if (mb < 0) {
        snprintf(out, outn, "unknown");
    } else if (mb >= 1000) {
        snprintf(out, outn, "%.1f GB", (double)mb / 1024.0);
    } else {
        snprintf(out, outn, "%d MB", mb);
    }
}

/* --- N3 soft-stop (catalog AI only; never Safari/system) --- */

static int pid_alive(int pid) {
    if (pid <= 1)
        return 0;
    return kill(pid, 0) == 0 || errno == EPERM;
}

static int is_protected_pid(int pid) {
    if (pid <= 1)
        return 1;
    if (pid == (int)getpid() || pid == (int)getppid())
        return 1;
    return 0;
}

static void audit_append(const char *state_dir, const char *line) {
    char path[512];
    snprintf(path, sizeof(path), "%s/soft-stop-audit.jsonl", state_dir);
    ensure_parent_dir(path);
    FILE *fp = fopen(path, "a");
    if (!fp)
        return;
    fputs(line, fp);
    if (line[0] && line[strlen(line) - 1] != '\n')
        fputc('\n', fp);
    fclose(fp);
}

/* OpenClaw KeepAlive: bootout gui/$UID/ai.openclaw.gateway. */
static int bootout_openclaw(int dry_run) {
    char cmd[256];
    snprintf(cmd, sizeof(cmd),
             "launchctl bootout gui/%d/ai.openclaw.gateway 2>/dev/null; "
             "launchctl bootout gui/%d/ai.openclaw.gateway 2>/dev/null; true",
             (int)getuid(), (int)getuid());
    if (dry_run)
        return 0;
    return system(cmd);
}

static int sigterm_pids(const int *pids, int n, int dry_run) {
    int nsig = 0;
    for (int i = 0; i < n; i++) {
        int pid = pids[i];
        if (is_protected_pid(pid))
            continue;
        if (!pid_alive(pid))
            continue;
        if (!dry_run) {
            if (kill(pid, SIGTERM) == 0)
                nsig++;
        } else {
            nsig++;
        }
    }
    return nsig;
}

static int sigkill_still_alive(const int *pids, int n, int dry_run) {
    int nsig = 0;
    for (int i = 0; i < n; i++) {
        int pid = pids[i];
        if (is_protected_pid(pid))
            continue;
        if (!pid_alive(pid))
            continue;
        if (!dry_run) {
            if (kill(pid, SIGKILL) == 0)
                nsig++;
        } else {
            nsig++;
        }
    }
    return nsig;
}

/* Prefer idle OpenClaw/service, else highest-RSS idle AI session. Never active/open. */
static int pick_idle_reclaim(const Sess *sess, int ns, long min_rss_kb) {
    int best_svc = -1;
    int best_idle = -1;
    for (int i = 0; i < ns; i++) {
        if (!str_eq(activity_for(sess[i].pcpu), "idle"))
            continue;
        if (sess[i].rss_kb < min_rss_kb && !str_eq(sess[i].app, "OpenClaw"))
            continue;
        /* never treat desktop heavy as auto if openable mid-use — already idle CPU */
        if (str_eq(sess[i].kind, "service") || str_eq(sess[i].app, "OpenClaw")) {
            if (best_svc < 0 || sess[i].rss_kb > sess[best_svc].rss_kb)
                best_svc = i;
        } else {
            if (best_idle < 0 || sess[i].rss_kb > sess[best_idle].rss_kb)
                best_idle = i;
        }
    }
    if (best_svc >= 0)
        return best_svc;
    return best_idle;
}

/* Notification Center banner — no permanent Cocoa menubar tax. */
static void maybe_notify(const char *band, double headroom_mb, long ai_rss_kb, const Sess *cand,
                         ActOpts *act, int quiet) {
    if (!act || !act->notify)
        return;
    if (strcmp(band, "warn") != 0 && strcmp(band, "hard") != 0)
        return;
    time_t now = time(NULL);
    if (act->last_notify > 0 && (now - act->last_notify) < NOTIFY_COOLDOWN_S &&
        strcmp(act->last_notify_band, band) == 0)
        return;

    const char *title = strcmp(band, "hard") == 0 ? "Little room left for more heavy apps"
                                                  : "Headroom is getting thinner";
    char body[400];
    int hr_i = headroom_mb >= 0 ? (int)(headroom_mb + 0.5) : 0;
    double ai_gb = ai_rss_kb / (1024.0 * 1024.0);
    if (cand) {
        snprintf(body, sizeof(body),
                 "About %d MB reclaimable headroom. AI ~%.2f GB. Idle reclaim candidate: %s "
                 "(%.2f GB). Active work is never auto-killed.",
                 hr_i, ai_gb, cand->app, cand->rss_kb / (1024.0 * 1024.0));
    } else {
        snprintf(body, sizeof(body),
                 "About %d MB reclaimable headroom. AI ~%.2f GB. No idle AI ≥0.5 GB — Apple "
                 "apps ≥1 GB may quit under freeze-risk (by design).",
                 hr_i, ai_gb);
    }
    /* escape for AppleScript */
    char etitle[160], ebody[500];
    size_t ti = 0, bi = 0;
    for (const char *p = title; *p && ti + 2 < sizeof(etitle); p++) {
        if (*p == '"' || *p == '\\')
            etitle[ti++] = '\\';
        etitle[ti++] = *p;
    }
    etitle[ti] = '\0';
    for (const char *p = body; *p && bi + 2 < sizeof(ebody); p++) {
        if (*p == '"' || *p == '\\')
            ebody[bi++] = '\\';
        if (*p == '\n')
            continue;
        ebody[bi++] = *p;
    }
    ebody[bi] = '\0';
    char cmd[800];
    snprintf(cmd, sizeof(cmd),
             "osascript -e 'display notification \"%s\" with title \"%s\" subtitle \"Local AI Monitor\"' "
             "2>/dev/null",
             ebody, etitle);
    system(cmd);
    act->last_notify = now;
    snprintf(act->last_notify_band, sizeof(act->last_notify_band), "%s", band);
    if (!quiet)
        printf("n3: notified band=%s\n", band);
}

static void maybe_soft_stop(const char *state_dir, const Sess *sess, int ns, const char *band,
                            int free_pages, ActOpts *act, int quiet) {
    if (!act || !act->enable)
        return;

    char ts[64];
    iso_local(ts, sizeof(ts));

    int band_ok = 0;
    if (strcmp(band, "hard") == 0)
        band_ok = 1;
    else if (act->on_warn && strcmp(band, "warn") == 0)
        band_ok = 1;
    if (!band_ok) {
        char line[512];
        snprintf(line, sizeof(line),
                 "{\"ts\":\"%s\",\"event\":\"soft_stop\",\"band\":\"%s\",\"free_pages\":%d,"
                 "\"acted\":false,\"skip\":\"band_ok\",\"dry_run\":%s}",
                 ts, band, free_pages, act->dry_run ? "true" : "false");
        audit_append(state_dir, line);
        if (!quiet)
            printf("n3: skip band=%s (need hard%s)\n", band,
                   act->on_warn ? " or warn" : "");
        return;
    }

    time_t now = time(NULL);
    if (act->last_act > 0 && (now - act->last_act) < ACT_COOLDOWN_S) {
        char line[512];
        snprintf(line, sizeof(line),
                 "{\"ts\":\"%s\",\"event\":\"soft_stop\",\"band\":\"%s\",\"free_pages\":%d,"
                 "\"acted\":false,\"skip\":\"cooldown\",\"dry_run\":%s}",
                 ts, band, free_pages, act->dry_run ? "true" : "false");
        audit_append(state_dir, line);
        if (!quiet)
            printf("n3: skip cooldown %lds remaining\n",
                   (long)(ACT_COOLDOWN_S - (now - act->last_act)));
        return;
    }

    int ci = pick_idle_reclaim(sess, ns, MIN_RECLAIM_RSS_KB);

    if (ci < 0) {
        char line[512];
        snprintf(line, sizeof(line),
                 "{\"ts\":\"%s\",\"event\":\"soft_stop\",\"band\":\"%s\",\"free_pages\":%d,"
                 "\"acted\":false,\"skip\":\"no_idle_candidate\",\"dry_run\":%s}",
                 ts, band, free_pages, act->dry_run ? "true" : "false");
        audit_append(state_dir, line);
        if (!quiet)
            printf("n3: no idle reclaim candidate\n");
        return;
    }

    const Sess *s = &sess[ci];
    /* N3 C path is catalog-AI only. Apple relief is Python local-ai-rm (by design). */
    if (str_eq(s->app, "Finder") || str_eq(s->app, "WindowServer") ||
        str_eq(s->app, "loginwindow") || str_eq(s->app, "kernel_task")) {
        if (!quiet)
            printf("n3: REFUSE system app %s\n", s->app);
        return;
    }

    int is_oc = str_eq(s->app, "OpenClaw");
    char pids_json[512];
    size_t po = 0;
    pids_json[0] = '[';
    po = 1;
    for (int i = 0; i < s->npids && po + 16 < sizeof(pids_json); i++) {
        int m = snprintf(pids_json + po, sizeof(pids_json) - po, "%s%d", i ? "," : "", s->pids[i]);
        if (m > 0)
            po += (size_t)m;
    }
    if (po + 2 < sizeof(pids_json)) {
        pids_json[po++] = ']';
        pids_json[po] = '\0';
    }

    if (!quiet) {
        printf("n3: %s soft-stop %s sid=%s rss_mb=%.1f pids=%s%s\n",
               act->dry_run ? "DRY-RUN" : "ACT", s->app, s->session_id, s->rss_kb / 1024.0,
               pids_json, is_oc ? " (bootout+term)" : " (SIGTERM)");
    }

    int n_term = 0, n_kill = 0, boot = 0;
    if (is_oc) {
        bootout_openclaw(act->dry_run);
        boot = 1;
    }
    n_term = sigterm_pids(s->pids, s->npids, act->dry_run);
    if (!act->dry_run && n_term > 0) {
        sleep(SOFT_STOP_GRACE_S);
        /* re-check activity law: only escalate if still idle-class was true at pick;
         * we do not re-sample CPU — escalate only leftover zombies */
        n_kill = sigkill_still_alive(s->pids, s->npids, act->dry_run);
    }

    if (!act->dry_run)
        act->last_act = now;

    char line[1024];
    snprintf(line, sizeof(line),
             "{\"ts\":\"%s\",\"event\":\"soft_stop\",\"band\":\"%s\",\"free_pages\":%d,"
             "\"acted\":true,\"dry_run\":%s,\"app\":\"%s\",\"session_id\":\"%s\","
             "\"rss_kb\":%ld,\"pids\":%s,\"bootout\":%s,\"sigterm\":%d,\"sigkill\":%d,"
             "\"skip\":\"%s\"}",
             ts, band, free_pages, act->dry_run ? "true" : "false", s->app, s->session_id,
             s->rss_kb, pids_json, boot ? "true" : "false", n_term, n_kill,
             act->dry_run ? "dry_run" : "soft_stopped_idle");
    audit_append(state_dir, line);
}

static int sample_once(const char *state_dir, int quiet, ActOpts *act, int sample_interval_s) {
    char live_path[512], phys_path[512], host_profile_path[512];
    snprintf(live_path, sizeof(live_path), "%s/live.min.json", state_dir);
    snprintf(phys_path, sizeof(phys_path), "%s/physics.json", state_dir);
    snprintf(host_profile_path, sizeof(host_profile_path), "%s/host-profile.json", state_dir);

    double t0 = now_ms();
    if (g_profile_first_ms <= 0.0)
        g_profile_first_ms = t0;
    if (pool_init() != 0) {
        fprintf(stderr, "local-ai-monitord: pool alloc failed\n");
        return 1;
    }
    Proc *procs = g_procs;
    Sess *sess = g_sess;
    char *json = g_json;
    memset(procs, 0, MAX_PROCS * sizeof(Proc));
    memset(sess, 0, MAX_SESS * sizeof(Sess));

    int free_pages = -1, page_size = 16384, cpu_count = 1;
    int speculative_pages = 0, purgeable_pages = 0, external_pages = 0;
    unsigned long long memsize = 0;
    SwapUsage swap;
    VmCounters vmc;
    sysctl_swapusage(&swap);
    sample_vm_counters(&vmc);
    if (sysctl_ull("hw.memsize", &memsize) != 0)
        memsize = 0;
    scale_headroom_for_memsize(memsize);
    sysctl_int("vm.page_free_count", &free_pages);
    sysctl_int("hw.pagesize", &page_size);
    if (sysctl_int("hw.ncpu", &cpu_count) != 0 || cpu_count <= 0)
        cpu_count = 1;
    if (existing_ready_profile_matches(host_profile_path, memsize, page_size))
        g_profile_first_ms = t0 - 60000.0;
    double profile_age_s = (t0 - g_profile_first_ms) / 1000.0;
    const char *profile_status = profile_age_s >= 60.0 ? "ready" : "provisional";
    double profile_confidence = clamp_double((profile_age_s / 60.0) * 0.5, 0.0, 0.5);
    if (strcmp(profile_status, "ready") == 0)
        profile_confidence = 1.0;
    char host_id[96];
    snprintf(host_id, sizeof(host_id), "native-v1:%llu:%d", memsize, page_size);
    long dynamic_heavy_rss_kb = host_heavy_rss_kb(memsize);
    if (sysctl_int("vm.page_speculative_count", &speculative_pages) != 0)
        speculative_pages = 0;
    if (sysctl_int("vm.page_purgeable_count", &purgeable_pages) != 0)
        purgeable_pages = 0;
    if (sysctl_int("vm.page_pageable_external_count", &external_pages) != 0)
        external_pages = 0;
    int physics_ok = (free_pages >= 0 && page_size > 0);
    double page_mb =
        physics_ok ? ((double)page_size / (1024.0 * 1024.0)) : 0.0;
    double free_mb = physics_ok ? ((double)free_pages * page_mb) : -1.0;
    double cheap_mb = physics_ok
                          ? (free_mb + (double)speculative_pages * page_mb +
                             (double)purgeable_pages * page_mb)
                          : -1.0;
    double file_mb = physics_ok ? ((double)external_pages * page_mb) : 0.0;
    double headroom_mb =
        physics_ok ? (cheap_mb + file_mb * FILE_BACKED_RECLAIM) : -1.0;
    double thrash_score = 0.0;
    if (vmc.ok && g_prev_vm_ready) {
        double dt_s = (t0 - g_prev_vm_ms) / 1000.0;
        if (dt_s > 0 && dt_s <= 3600.0 && vmc.swapins >= g_prev_vm.swapins &&
            vmc.swapouts >= g_prev_vm.swapouts && vmc.pageouts >= g_prev_vm.pageouts) {
            thrash_score = thrash_score_from_deltas(
                dt_s, vmc.swapins - g_prev_vm.swapins,
                vmc.swapouts - g_prev_vm.swapouts, vmc.pageouts - g_prev_vm.pageouts);
        }
    }
    if (vmc.ok) {
        g_prev_vm = vmc;
        g_prev_vm_ms = t0;
        g_prev_vm_ready = 1;
    }
    const char *band = band_for_headroom(headroom_mb, thrash_score, physics_ok);
    const char *pressure_state = "unknown";
    const char *recommendation = "refuse";
    int can_start_heavy = 0;

    int n = run_ps(procs, MAX_PROCS);
    if (n < 0) {
        fprintf(stderr, "local-ai-monitord: ps failed\n");
        return 1;
    }
    n = filter_self_inplace(procs, n);

    int ns = 0;
    sessionize(procs, n, sess, &ns);
    qsort(sess, (size_t)ns, sizeof(Sess), cmp_sess_rss);

    long total_rss = 0;
    double total_cpu = 0;
    int total_nproc = 0;
    for (int i = 0; i < ns; i++) {
        total_rss += sess[i].rss_kb;
        total_cpu += sess[i].pcpu;
        total_nproc += sess[i].nproc;
    }

    int top_i = -1;
    for (int i = 0; i < ns; i++) {
        if (top_i < 0 || sess[i].pcpu > sess[top_i].pcpu ||
            (sess[i].pcpu == sess[top_i].pcpu && sess[i].rss_kb > sess[top_i].rss_kb))
            top_i = i;
    }

    /* reclaim candidate: idle only (never active/open mid-stream — L4) */
    int cand = -1;
    for (int i = 0; i < ns; i++) {
        if (!str_eq(activity_for(sess[i].pcpu), "idle"))
            continue;
        if (sess[i].rss_kb < MIN_RECLAIM_RSS_KB)
            continue;
        if (cand < 0 || sess[i].rss_kb > sess[cand].rss_kb)
            cand = i;
    }

    double wall_ms = now_ms() - t0;
    char ts[64];
    iso_local(ts, sizeof(ts));

    /* physics.json — headroom primary band */
    {
        char body[2048];
        snprintf(body, sizeof(body),
                 "{\n"
                 "  \"version\": 2,\n"
                 "  \"source\": \"local-ai-monitord\",\n"
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
                 "  \"swap_total_mb\": %.1f,\n"
                 "  \"swap_used_mb\": %.1f,\n"
                 "  \"swap_avail_mb\": %.1f,\n"
                 "  \"swapins\": %llu,\n"
                 "  \"swapouts\": %llu,\n"
                 "  \"pageouts\": %llu,\n"
                 "  \"compressor_pages\": %llu,\n"
                 "  \"thrash_score\": %.2f,\n"
                 "  \"band\": \"%s\",\n"
                 "  \"band_source\": \"headroom+swap\",\n"
                 "  \"headroom_ok_mb\": %.0f,\n"
                 "  \"headroom_warn_mb\": %.0f,\n"
                 "  \"profile_status\": \"%s\",\n"
                 "  \"profile_age_s\": %.1f,\n"
                 "  \"profile_confidence\": %.3f,\n"
                 "  \"waterline_warn\": %d,\n"
                 "  \"waterline_hard\": %d\n"
                 "}\n",
                 ts, physics_ok ? "true" : "false", page_size, free_pages, free_mb,
                 speculative_pages, purgeable_pages, external_pages, cheap_mb, file_mb,
                 headroom_mb, memsize,
                 swap.ok ? swap.total_bytes / (1024.0 * 1024.0) : -1.0,
                 swap.ok ? swap.used_bytes / (1024.0 * 1024.0) : -1.0,
                 swap.ok ? swap.avail_bytes / (1024.0 * 1024.0) : -1.0,
                 vmc.ok ? vmc.swapins : 0ULL, vmc.ok ? vmc.swapouts : 0ULL,
                 vmc.ok ? vmc.pageouts : 0ULL, vmc.ok ? vmc.compressor_pages : 0ULL,
                 thrash_score, band, g_headroom_ok_mb, g_headroom_warn_mb,
                 profile_status, profile_age_s, profile_confidence, g_warn_pages,
                 g_hard_pages);
        ensure_parent_dir(phys_path);
        if (write_atomic(phys_path, body) != 0)
            fprintf(stderr, "local-ai-monitord: physics write failed\n");
    }

    {
        char body[1024];
        snprintf(body, sizeof(body),
                 "{\n"
                 "  \"version\": 1,\n"
                 "  \"source\": \"local-ai-monitord\",\n"
                 "  \"host_id\": \"%s\",\n"
                 "  \"memsize_bytes\": %llu,\n"
                 "  \"page_size\": %d,\n"
                 "  \"status\": \"%s\",\n"
                 "  \"confidence\": %.3f,\n"
                 "  \"profile_age_s\": %.1f,\n"
                 "  \"headroom_ok_mb\": %.0f,\n"
                 "  \"headroom_warn_mb\": %.0f,\n"
                 "  \"threshold_source\": \"host-bounded:v1\"\n"
                 "}\n",
                 host_id, memsize, page_size, profile_status,
                 profile_confidence, profile_age_s, g_headroom_ok_mb, g_headroom_warn_mb);
        ensure_parent_dir(host_profile_path);
        if (write_atomic(host_profile_path, body) != 0)
            fprintf(stderr, "local-ai-monitord: host profile write failed\n");
    }

    size_t off = 0;
    if (apf(json, JSON_CAP, &off,
            "{\n"
            "  \"version\": 2,\n"
            "  \"source\": \"local-ai-monitord\",\n"
            "  \"schema\": \"live.min\",\n"
            "  \"ts\": \"%s\",\n"
            "  \"collector_pid\": %d,\n"
            "  \"sample_interval_s\": %d,\n"
            "  \"wall_ms\": %.2f,\n"
            "  \"scanned_procs\": %d,\n"
            "  \"totals\": {\"cpu_pct\": %.2f, \"rss_kb\": %ld, \"nproc\": %d, "
            "\"nsessions\": %d},\n",
            ts, (int)getpid(), sample_interval_s > 0 ? sample_interval_s : 10, wall_ms, n,
            total_cpu, total_rss, total_nproc, ns) != 0)
        goto oom;

    if (top_i >= 0) {
        char elab[256], esid[192], eapp[64];
        jesc(elab, sizeof(elab), sess[top_i].label);
        jesc(esid, sizeof(esid), sess[top_i].session_id);
        jesc(eapp, sizeof(eapp), sess[top_i].app);
        if (apf(json, JSON_CAP, &off,
                "  \"top\": {\"app\": \"%s\", \"session_id\": \"%s\", \"label\": \"%s\", "
                "\"cpu_pct\": %.2f, \"rss_kb\": %ld},\n",
                eapp, esid, elab, sess[top_i].pcpu, sess[top_i].rss_kb) != 0)
            goto oom;
    } else if (apf(json, JSON_CAP, &off, "  \"top\": null,\n") != 0)
        goto oom;

    if (apf(json, JSON_CAP, &off, "  \"tools\": {\"running\": [") != 0)
        goto oom;
    {
        int first = 1;
        int seen[16];
        memset(seen, 0, sizeof(seen));
        for (int i = 0; i < ns; i++) {
            int ai = app_name_index(sess[i].app);
            if (ai < 0 || ai >= 16 || seen[ai])
                continue;
            seen[ai] = 1;
            char eapp[64];
            jesc(eapp, sizeof(eapp), sess[i].app);
            if (apf(json, JSON_CAP, &off, "%s\"%s\"", first ? "" : ", ", eapp) != 0)
                goto oom;
            first = 0;
        }
    }
    if (apf(json, JSON_CAP, &off,
            "], \"installed\": [], \"visible\": [], \"show_idle_installed\": false},\n"
            "  \"attention\": [],\n  \"sparks\": {},\n") != 0)
        goto oom;

    /* resource — headroom / freeze-risk copy (never free-page panic titles) */
    {
        int show = (strcmp(profile_status, "provisional") == 0 ||
                    strcmp(band, "warn") == 0 || strcmp(band, "hard") == 0 ||
                    !can_start_heavy);
        int free_i = free_mb >= 0 ? (int)(free_mb + 0.5) : -1;
        int hr_i = headroom_mb >= 0 ? (int)(headroom_mb + 0.5) : -1;
        int swap_used_i = swap.ok ? (int)(swap.used_bytes / (1024ULL * 1024ULL)) : -1;
        int swap_total_i = swap.ok ? (int)(swap.total_bytes / (1024ULL * 1024ULL)) : -1;
        int ai_mb = (int)(total_rss / 1024);
        int mem_mb_i = memsize > 0 ? (int)(memsize / (1024ULL * 1024ULL)) : -1;
        double ai_mem_pct = mem_mb_i > 0 ? ((double)ai_mb * 100.0 / (double)mem_mb_i) : 0.0;
        double cpu_capacity_pct = cpu_count > 0 ? (total_cpu / (double)cpu_count) : total_cpu;
        if (cpu_capacity_pct < 0.0)
            cpu_capacity_pct = 0.0;
        if (cpu_capacity_pct > 100.0)
            cpu_capacity_pct = 100.0;
        update_learned_capacity(ai_mb, ai_mem_pct, cpu_capacity_pct, thrash_score);
        append_host_sample(state_dir, host_id, headroom_mb, thrash_score, ai_mb, ai_mem_pct,
                           cpu_capacity_pct);
        {
            char body[1280];
            snprintf(body, sizeof(body),
                     "{\n"
                     "  \"version\": 1,\n"
                     "  \"source\": \"local-ai-monitord\",\n"
                     "  \"host_id\": \"%s\",\n"
                     "  \"memsize_bytes\": %llu,\n"
                     "  \"page_size\": %d,\n"
                     "  \"status\": \"%s\",\n"
                     "  \"confidence\": %.3f,\n"
                     "  \"profile_age_s\": %.1f,\n"
                     "  \"headroom_ok_mb\": %.0f,\n"
                     "  \"headroom_warn_mb\": %.0f,\n"
                     "  \"threshold_source\": \"host-bounded:v1\",\n"
                     "  \"learned\": {\n"
                     "    \"ai_rss_ewma_mb\": %.1f,\n"
                     "    \"ai_mem_ewma_pct\": %.1f,\n"
                     "    \"cpu_capacity_ewma_pct\": %.1f,\n"
                     "    \"thrash_ewma\": %.2f\n"
                     "  }\n"
                     "}\n",
                     host_id, memsize, page_size, profile_status, profile_confidence,
                     profile_age_s, g_headroom_ok_mb, g_headroom_warn_mb, g_learn_ai_rss_mb,
                     g_learn_ai_mem_pct, g_learn_cpu_capacity_pct, g_learn_thrash_score);
            if (write_atomic(host_profile_path, body) != 0)
                fprintf(stderr, "local-ai-monitord: learned host profile write failed\n");
        }
        int low_room = headroom_mb >= 0.0 && headroom_mb < g_headroom_warn_mb;
        int loaded_host = ai_mem_pct >= 25.0 || cpu_capacity_pct >= 70.0;
        if (!physics_ok) {
            pressure_state = "unknown";
            recommendation = "refuse";
        } else if (low_room && thrash_score >= THRASH_HARD) {
            pressure_state = "freeze_risk";
            recommendation = "avoid_new_heavy_work";
        } else if (low_room || (thrash_score >= THRASH_HARD && loaded_host)) {
            pressure_state = "stop_start_gate";
            recommendation = "avoid_new_heavy_work";
        } else if (strcmp(profile_status, "provisional") == 0) {
            pressure_state = "calibrating";
            recommendation = "do_nothing";
            can_start_heavy = 1;
        } else if (strcmp(band, "warn") == 0 || strcmp(band, "hard") == 0 ||
                   thrash_score >= THRASH_WARN) {
            pressure_state = "caution";
            recommendation = "watch_capacity";
            can_start_heavy = 1;
        } else {
            pressure_state = "ok";
            recommendation = "do_nothing";
            can_start_heavy = 1;
        }
        const char *chip =
            !show ? ""
                  : (strcmp(profile_status, "provisional") == 0
                         ? "Monitor · Calibrating"
                         : (str_eq(pressure_state, "freeze_risk") ? "Monitor · Protect work"
                                                                   : "Monitor · Watch"));
        const char *title =
            !show ? ""
                  : (strcmp(profile_status, "provisional") == 0
                         ? "Calibrating this Mac"
                         : (str_eq(pressure_state, "freeze_risk") ? "Swap / headroom risk is high"
                                                                  : "Keep an eye on capacity"));
        char detail[512] = "";
        char clab[160] = "";
        char capp[64] = "";
        char csid[120] = "";
        char hr_s[32], swap_s[32], ai_s[32], cand_s[32];
        fmt_mb(hr_s, sizeof(hr_s), hr_i);
        fmt_mb(swap_s, sizeof(swap_s), swap_used_i);
        fmt_mb(ai_s, sizeof(ai_s), ai_mb);
        if (show && cand >= 0 && !can_start_heavy) {
            recommendation = "reclaim_idle";
            jesc(capp, sizeof(capp), sess[cand].app);
            jesc(csid, sizeof(csid), sess[cand].session_id);
            int cmb = (int)(sess[cand].rss_kb / 1024);
            if (cmb < 1 && sess[cand].rss_kb > 0)
                cmb = 1;
            fmt_mb(cand_s, sizeof(cand_s), cmb);
            snprintf(clab, sizeof(clab), "%s · %s", sess[cand].app, cand_s);
            snprintf(detail, sizeof(detail),
                     "About %s reclaimable headroom. Swap is using %s. "
                     "Thrash score %.1f. AI tools are using about %s. "
                     "Idle reclaim may stop “%s” (%s). "
                     "Active work is never auto-killed.",
                     hr_s, swap_s, thrash_score, ai_s, sess[cand].app, cand_s);
        } else if (show) {
            if (strcmp(profile_status, "provisional") == 0) {
                snprintf(detail, sizeof(detail),
                         "About %s reclaimable headroom. This Mac is learning its first local baseline. "
                         "Green starts around %.0f MB on this host. Swap is using %s. Thrash score %.1f.",
                         hr_s, g_headroom_ok_mb, swap_s, thrash_score);
            } else {
                snprintf(detail, sizeof(detail),
                         "About %s reclaimable headroom. This Mac is calibrated around %.0f MB green / %.0f MB hold. "
                         "Swap is using %s. Thrash score %.1f. AI tools are using about %s.",
                         hr_s, g_headroom_ok_mb, g_headroom_warn_mb, swap_s, thrash_score, ai_s);
            }
        }
        char echip[80], etitle[160], edetail[640], eclab[200];
        jesc(echip, sizeof(echip), chip);
        jesc(etitle, sizeof(etitle), title);
        jesc(edetail, sizeof(edetail), detail);
        jesc(eclab, sizeof(eclab), clab);

        if (apf(json, JSON_CAP, &off,
                "  \"resource\": {\n    \"band\": \"%s\",\n    \"show\": %s,\n", band,
                show ? "true" : "false") != 0)
            goto oom;
        if (show) {
            if (apf(json, JSON_CAP, &off,
                    "    \"chip\": \"%s\",\n    \"title\": \"%s\",\n    \"detail\": \"%s\",\n",
                    echip, etitle, edetail) != 0)
                goto oom;
            if (capp[0]) {
                if (apf(json, JSON_CAP, &off,
                        "    \"candidate_label\": \"%s\",\n"
                        "    \"candidate_app\": \"%s\",\n"
                        "    \"candidate_session_id\": \"%s\",\n",
                        eclab, capp, csid) != 0)
                    goto oom;
            } else if (apf(json, JSON_CAP, &off,
                           "    \"candidate_label\": null,\n"
                           "    \"candidate_app\": null,\n"
                           "    \"candidate_session_id\": null,\n") != 0)
                goto oom;
        } else if (apf(json, JSON_CAP, &off,
                       "    \"chip\": null,\n    \"title\": null,\n    \"detail\": null,\n"
                       "    \"candidate_label\": null,\n    \"candidate_app\": null,\n"
                       "    \"candidate_session_id\": null,\n") != 0)
            goto oom;
        if (free_i >= 0) {
            if (apf(json, JSON_CAP, &off, "    \"free_mb\": %d,\n", free_i) != 0)
                goto oom;
        } else if (apf(json, JSON_CAP, &off, "    \"free_mb\": null,\n") != 0)
            goto oom;
        if (hr_i >= 0) {
            if (apf(json, JSON_CAP, &off, "    \"headroom_mb\": %d,\n", hr_i) != 0)
                goto oom;
        } else if (apf(json, JSON_CAP, &off, "    \"headroom_mb\": null,\n") != 0)
            goto oom;
        if (swap_used_i >= 0) {
            if (apf(json, JSON_CAP, &off, "    \"swap_used_mb\": %d,\n", swap_used_i) != 0)
                goto oom;
        } else if (apf(json, JSON_CAP, &off, "    \"swap_used_mb\": null,\n") != 0)
            goto oom;
        if (swap_total_i >= 0) {
            if (apf(json, JSON_CAP, &off, "    \"swap_total_mb\": %d,\n", swap_total_i) != 0)
                goto oom;
        } else if (apf(json, JSON_CAP, &off, "    \"swap_total_mb\": null,\n") != 0)
            goto oom;
        if (apf(json, JSON_CAP, &off,
                "    \"headroom_ok_mb\": %d,\n"
                "    \"headroom_warn_mb\": %d,\n"
                "    \"memsize_mb\": %d,\n"
                "    \"ai_mem_pct\": %.1f,\n"
                "    \"cpu_count\": %d,\n"
                "    \"cpu_capacity_pct\": %.1f,\n"
                "    \"heavy_rss_kb\": %ld,\n"
                "    \"learned_ai_rss_mb\": %.1f,\n"
                "    \"learned_ai_mem_pct\": %.1f,\n"
                "    \"learned_cpu_capacity_pct\": %.1f,\n"
                "    \"learned_thrash_score\": %.2f,\n"
                "    \"profile_status\": \"%s\",\n"
                "    \"profile_age_s\": %.1f,\n"
                "    \"profile_confidence\": %.3f,\n"
                "    \"action_label\": \"Reclaim idle\",\n"
                "    \"ai_rss_mb\": %d,\n"
                "    \"thrash_score\": %.2f,\n"
                "    \"pressure_state\": \"%s\",\n"
                "    \"recommendation\": \"%s\",\n"
                "    \"can_start_heavy\": %s,\n"
                "    \"checkpoint_hint\": \"%s\",\n"
                "    \"browser_hint\": \"%s\",\n"
                "    \"swapins\": %llu,\n"
                "    \"swapouts\": %llu,\n"
                "    \"pageouts\": %llu,\n"
                "    \"urgency\": \"%s\",\n"
                "    \"auto_end\": false\n"
                "  },\n",
                (int)(g_headroom_ok_mb + 0.5), (int)(g_headroom_warn_mb + 0.5),
                mem_mb_i, ai_mem_pct, cpu_count, cpu_capacity_pct,
                dynamic_heavy_rss_kb, g_learn_ai_rss_mb, g_learn_ai_mem_pct,
                g_learn_cpu_capacity_pct, g_learn_thrash_score,
                profile_status, profile_age_s, profile_confidence,
                ai_mb, thrash_score, pressure_state, recommendation,
                can_start_heavy ? "true" : "false",
                can_start_heavy ? "" : "Checkpoint current AI work before continuing.",
                can_start_heavy ? "" : "Review inactive apps before adding load.",
                vmc.ok ? vmc.swapins : 0ULL,
                vmc.ok ? vmc.swapouts : 0ULL, vmc.ok ? vmc.pageouts : 0ULL,
                show ? band : "none") != 0)
            goto oom;
    }

    if (apf(json, JSON_CAP, &off, "  \"sessions\": [\n") != 0)
        goto oom;
    for (int i = 0; i < ns; i++) {
        Sess *s = &sess[i];
        char eapp[64], esid[192], elab[256], edet[512], ekind[32];
        jesc(eapp, sizeof(eapp), s->app);
        jesc(esid, sizeof(esid), s->session_id);
        jesc(elab, sizeof(elab), s->label);
        jesc(edet, sizeof(edet), s->detail);
        jesc(ekind, sizeof(ekind), s->kind);
        const char *act = activity_for(s->pcpu);
        int heavy = s->rss_kb >= dynamic_heavy_rss_kb;
        int auto_reclaim = 0;
        if (str_eq(act, "idle") && s->rss_kb >= MIN_RECLAIM_RSS_KB)
            auto_reclaim = 1;
        if (str_eq(act, "active"))
            auto_reclaim = 0;

        if (apf(json, JSON_CAP, &off,
                "    {\"app\": \"%s\", \"session_id\": \"%s\", \"label\": \"%s\", "
                "\"detail\": \"%s\", \"cpu_pct\": %.2f, \"rss_kb\": %ld, \"nproc\": %d, "
                "\"alive\": true, \"kind\": \"%s\", \"openable\": false, "
                "\"activity\": \"%s\", \"activity_state\": \"%s\", \"auto_reclaim\": %s, "
                "\"heavy\": %s, \"pids\": [",
                eapp, esid, elab, edet, s->pcpu, s->rss_kb, s->nproc, ekind, act, act,
                auto_reclaim ? "true" : "false", heavy ? "true" : "false") != 0)
            goto oom;
        for (int p = 0; p < s->npids; p++) {
            if (apf(json, JSON_CAP, &off, "%s%d", p ? ", " : "", s->pids[p]) != 0)
                goto oom;
        }
        if (apf(json, JSON_CAP, &off, "]}%s\n", i + 1 < ns ? "," : "") != 0)
            goto oom;
    }
    if (apf(json, JSON_CAP, &off, "  ]\n}\n") != 0)
        goto oom;

    ensure_parent_dir(live_path);
    if (write_atomic(live_path, json) != 0) {
        fprintf(stderr, "local-ai-monitord: live write failed: %s (%s)\n", live_path, strerror(errno));
        return 1;
    }
    /* Dual-write live.json so glass menubar default path stays fresh when
     * LOCAL_AI_MONITOR_LIVE is missing; same payload as live.min (lean feed). */
    {
        char live_full[768];
        snprintf(live_full, sizeof(live_full), "%s/live.json", state_dir);
        if (write_atomic(live_full, json) != 0)
            fprintf(stderr, "local-ai-monitord: live.json write failed (non-fatal)\n");
    }

    if (!quiet) {
        printf("%s wall_ms=%.2f sessions=%d total_rss_mb=%.1f band=%s headroom_mb=%.1f "
               "free_mb=%.1f\n",
               live_path, wall_ms, ns, total_rss / 1024.0, band, headroom_mb, free_mb);
        for (int i = 0; i < ns; i++) {
            printf("  %-14s %-40s n=%d rss_mb=%6.1f cpu=%.1f act=%s\n", sess[i].app,
                   sess[i].session_id, sess[i].nproc, sess[i].rss_kb / 1024.0, sess[i].pcpu,
                   activity_for(sess[i].pcpu));
        }
    }

    /* Notify (no menubar) then optional soft-stop */
    {
        int ci = pick_idle_reclaim(sess, ns, MIN_RECLAIM_RSS_KB);
        const Sess *cand = ci >= 0 ? &sess[ci] : NULL;
        maybe_notify(band, headroom_mb, total_rss, cand, act, quiet);
    }
    maybe_soft_stop(state_dir, sess, ns, band, free_pages, act, quiet);

    return 0;

oom:
    fprintf(stderr, "local-ai-monitord: buffer overflow building JSON\n");
    return 1;
}

int main(int argc, char **argv) {
    int loop = 0;
    int interval = 10;
    int quiet = 0;
    const char *state_dir = NULL;
    ActOpts act;
    memset(&act, 0, sizeof(act));

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--loop") == 0)
            loop = 1;
        else if (strcmp(argv[i], "--interval") == 0 && i + 1 < argc)
            interval = atoi(argv[++i]);
        else if (strcmp(argv[i], "--state") == 0 && i + 1 < argc)
            state_dir = argv[++i];
        else if (strcmp(argv[i], "-q") == 0 || strcmp(argv[i], "--quiet") == 0)
            quiet = 1;
        else if (strcmp(argv[i], "--act") == 0)
            act.enable = 1;
        else if (strcmp(argv[i], "--dry-run") == 0)
            act.dry_run = 1;
        else if (strcmp(argv[i], "--act-on-warn") == 0)
            act.on_warn = 1;
        else if (strcmp(argv[i], "--notify") == 0)
            act.notify = 1;
        else if (strcmp(argv[i], "--bare-metal") == 0) {
            /* Always-on control plane: act on hard + notify; no Cocoa menubar. */
            act.enable = 1;
            act.notify = 1;
        }
        else if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            fprintf(stderr,
                    "usage: local-ai-monitord [--loop] [--interval N] [--state dir] [-q]\n"
                    "               [--act] [--dry-run] [--act-on-warn] [--notify]\n"
                    "               [--bare-metal]\n"
                    "  budget: always-on target ≤5 MB RSS (no SwiftUI/Python)\n"
                    "  --bare-metal = --act + --notify (hard band idle AI reclaim)\n"
                    "  Apple apps ≥1 GB: Python resource tick or future C osascript\n"
                    "  writes: live.min.json, physics.json\n");
            return 0;
        }
    }
    if (interval <= 0)
        interval = 10;
    /* dry-run alone is useless without act */
    if (act.dry_run && !act.enable)
        act.enable = 1;
    /* Lab waterline override (test only): LOCAL_AI_MONITORD_WATERLINE_HARD / _WARN */
    {
        const char *wh = getenv("LOCAL_AI_MONITORD_WATERLINE_HARD");
        const char *ww = getenv("LOCAL_AI_MONITORD_WATERLINE_WARN");
        if (wh && wh[0])
            g_hard_pages = atoi(wh);
        if (ww && ww[0])
            g_warn_pages = atoi(ww);
        if (g_hard_pages < 0)
            g_hard_pages = HARD_PAGES_DEFAULT;
        if (g_warn_pages < g_hard_pages)
            g_warn_pages = g_hard_pages + 1;
    }

    char default_state[512];
    if (!state_dir) {
        const char *st = getenv("LOCAL_AI_MONITOR_STATE");
        const char *home = getenv("HOME");
        if (!home)
            home = ".";
        if (st && st[0])
            snprintf(default_state, sizeof(default_state), "%s", st);
        else
            snprintf(default_state, sizeof(default_state), "%s/.local/state/local-ai-monitor-native",
                     home);
        state_dir = default_state;
    }

    do {
        int rc = sample_once(state_dir, quiet, &act, interval);
        if (rc != 0 && !loop)
            return rc;
        if (loop)
            sleep((unsigned)interval);
    } while (loop);

    return 0;
}
