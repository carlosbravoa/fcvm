/*
 * fc-init: the init of every fcvm microVM. It boots as /init from an
 * initramfs (build/initramfs.cpio), so images don't carry it and upgrading it
 * never needs an image rebuild.
 *
 * Stage 1 assembles the root filesystem from the drives named on the kernel
 * command line and switches into it:
 *   fcvm.root=DEV          image disk (read-only when fcvm.rw is given)
 *   fcvm.rw=DEV            per-VM writable layer (ext4 holding upper/, work/)
 *   fcvm.layers=DEV,...    committed image layers, topmost first (ext4, upper/)
 *   fcvm.vols=DEV:PATH[:ro],...  volumes
 *   fcvm.exec=PATH         then exec PATH as PID 1 (systemd images) ...
 *   fcvm.proxy=URL         restricted network: http(s)_proxy for the container
 *                          command and exec sessions (systemd images get it via
 *                          systemd.setenv=)
 *
 * ... otherwise stage 2 acts as PID 1 for a container image:
 * Does what a container runtime would: mounts the API filesystems, sets the
 * hostname and resolv.conf, then runs the image's entrypoint with its env,
 * working directory and user. Stays as PID 1 to reap zombies and forward
 * signals; when the main process exits the VM reboots, which makes
 * Firecracker exit (boot with reboot=k).
 *
 * Configuration written by `fcvm import` under /.fcvm/:
 *   argv      NUL-separated argument vector
 *   env       NUL-separated KEY=VALUE list
 *   workdir   working directory
 *   user      "uid:gid[:gid,gid...]"
 *   hostname  hostname
 *
 * Networking is configured by the kernel (ip= on the command line); DNS
 * servers from ip= show up in /proc/net/pnp in resolv.conf format.
 *
 * `fc-init --agent` runs the exec agent standalone (systemd images), and
 * `fc-init --idle` just waits (containers created with --idle, for exec).
 *
 * On exit the main process status (exit code, or 128+signal) is written to
 * /.fcvm/exit-status, which the host reads back from the writable disk.
 *
 * Build: lib/build-init.sh (static binary + initramfs)
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <linux/vm_sockets.h>
#include <net/if.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

#define CONF "/.fcvm/"

static void msg(const char *fmt, ...)
{
    char buf[512] = "[fc-init] ";
    va_list ap;
    va_start(ap, fmt);
    int n = 10 + vsnprintf(buf + 10, sizeof(buf) - 11, fmt, ap);
    va_end(ap);
    if (n > (int)sizeof(buf) - 1)
        n = sizeof(buf) - 1;
    buf[n++] = '\n';
    write(2, buf, n); /* one write, so parent and child lines don't interleave */
}

static void mnt(const char *src, const char *dst, const char *type,
                unsigned long flags, const char *data)
{
    mkdir(dst, 0755);
    if (mount(src, dst, type, flags, data) < 0 && errno != EBUSY)
        msg("mount %s: %s", dst, strerror(errno));
}

/* Read a whole file; returns NULL if missing. */
static char *slurp(const char *path, size_t *len)
{
    FILE *f = fopen(path, "r");
    if (!f)
        return NULL;
    size_t cap = 4096, n = 0, r;
    char *buf = malloc(cap + 1);
    while ((r = fread(buf + n, 1, cap - n, f)) > 0) {
        n += r;
        if (n == cap)
            buf = realloc(buf, (cap *= 2) + 1);
    }
    fclose(f);
    buf[n] = '\0';
    if (len)
        *len = n;
    return buf;
}

static void spit(const char *path, const char *data)
{
    unlink(path); /* images often ship these as dangling symlinks */
    FILE *f = fopen(path, "w");
    if (!f) {
        msg("write %s: %s", path, strerror(errno));
        return;
    }
    fputs(data, f);
    fclose(f);
}

/* Split a NUL-separated buffer into a NULL-terminated vector. */
static char **split0(char *buf, size_t len)
{
    size_t n = 0, i;
    for (i = 0; i < len; i++)
        if (buf[i] == '\0')
            n++;
    char **v = calloc(n + 2, sizeof(*v));
    size_t k = 0;
    for (i = 0; i < len; i += strlen(buf + i) + 1)
        v[k++] = buf + i;
    v[k] = NULL;
    return v;
}

static char *trim(char *s)
{
    if (!s)
        return s;
    s[strcspn(s, "\n")] = '\0';
    return s;
}

/* Value of "key=value" on the kernel command line, or NULL. */
static char *karg(const char *key)
{
    static char *cmdline;
    if (!cmdline && !(cmdline = trim(slurp("/proc/cmdline", NULL))))
        return NULL;
    size_t klen = strlen(key);
    for (char *p = cmdline; (p = strstr(p, key)); p += klen) {
        if ((p == cmdline || p[-1] == ' ') && p[klen] == '=') {
            char *v = strndup(p + klen + 1, strcspn(p + klen + 1, " "));
            return v;
        }
    }
    return NULL;
}

static int write_all(int fd, const void *p, size_t n);

static void mkdir_p(const char *path, mode_t mode)
{
    char tmp[4096];
    snprintf(tmp, sizeof(tmp), "%s", path);
    for (char *p = tmp + 1; *p; p++)
        if (*p == '/') {
            *p = '\0';
            mkdir(tmp, mode);
            *p = '/';
        }
    mkdir(tmp, mode);
}

/* Mount an ext4 block device, waiting briefly for its node to appear. */
static int mount_dev(const char *dev, const char *dir, unsigned long flags)
{
    for (int i = 0; i < 100 && access(dev, F_OK) < 0; i++)
        usleep(20000);
    mkdir_p(dir, 0755);
    if (mount(dev, dir, "ext4", flags, NULL) < 0) {
        msg("mount %s on %s: %s", dev, dir, strerror(errno));
        return -1;
    }
    return 0;
}

static int copy_file(const char *src, const char *dst, mode_t mode)
{
    size_t len;
    char *data = slurp(src, &len);
    int fd = data ? open(dst, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, mode) : -1;
    int ok = fd >= 0 && write_all(fd, data, len) == 0;
    if (fd >= 0)
        close(fd);
    free(data);
    return ok ? 0 : -1;
}

static int is_empty_dir(const char *path)
{
    DIR *d = opendir(path);
    struct dirent *e;
    int empty = 1;
    while (d && (e = readdir(d)))
        if (strcmp(e->d_name, ".") && strcmp(e->d_name, ".."))
            empty = 0;
    if (d)
        closedir(d);
    return d && empty;
}

/*
 * Stage 1, running from the initramfs: assemble the root filesystem from the
 * drives named on the kernel command line, then switch into it (the moves
 * util-linux switch_root does). Every mount but the final root lives under
 * the initramfs and becomes unreachable, but stays alive as overlay layers.
 */
static int assemble_root(void)
{
    char *root = karg("fcvm.root"), *rw = karg("fcvm.rw");
    char *layers = karg("fcvm.layers"), *vols = karg("fcvm.vols");
    const char *nr = "/mnt/root";
    char *save;
    if (!root) {
        msg("no fcvm.root= on the kernel command line");
        return -1;
    }
    if (!rw) { /* private disk (--copy): mount it read-write as is */
        if (mount_dev(root, nr, MS_NOATIME) < 0)
            return -1;
    } else {
        char lower[2048] = "", opts[2400];
        if (mount_dev(root, "/mnt/base", MS_RDONLY) < 0 ||
            mount_dev(rw, "/mnt/rw", MS_NOATIME) < 0)
            return -1;
        mkdir("/mnt/rw/upper", 0755);
        mkdir("/mnt/rw/work", 0755);
        int i = 0;
        for (char *d = layers ? strtok_r(layers, ",", &save) : NULL; d; d = strtok_r(NULL, ",", &save)) {
            char dir[32];
            snprintf(dir, sizeof(dir), "/mnt/l%d", i++);
            if (mount_dev(d, dir, MS_RDONLY) < 0)
                return -1;
            size_t n = strlen(lower);
            snprintf(lower + n, sizeof(lower) - n, "%s/upper:", dir);
        }
        size_t n = strlen(lower);
        snprintf(lower + n, sizeof(lower) - n, "/mnt/base");
        snprintf(opts, sizeof(opts), "lowerdir=%s,upperdir=/mnt/rw/upper,workdir=/mnt/rw/work", lower);
        mkdir_p(nr, 0755);
        if (mount("overlay", nr, "overlay", 0, opts) < 0) {
            msg("mount overlay: %s", strerror(errno));
            return -1;
        }
    }

    /* Volumes: DEV:PATH[:ro]. A new (empty) volume takes the owner and mode of
     * the directory it covers, like a docker named volume, so images running
     * as non-root can write to it. */
    for (char *v = vols ? strtok_r(vols, ",", &save) : NULL; v; v = strtok_r(NULL, ",", &save)) {
        char *dev = v, *path = strchr(v, ':'), dst[4096];
        if (!path)
            continue;
        *path++ = '\0';
        char *opt = strchr(path, ':');
        if (opt)
            *opt++ = '\0';
        int ro = opt && strcmp(opt, "ro") == 0;
        snprintf(dst, sizeof(dst), "%s%s", nr, path);
        struct stat under;
        int had = stat(dst, &under) == 0 && S_ISDIR(under.st_mode);
        if (mount_dev(dev, dst, ro ? MS_RDONLY : MS_NOATIME) < 0)
            return -1;
        if (had && !ro && is_empty_dir(dst)) {
            chown(dst, under.st_uid, under.st_gid);
            chmod(dst, under.st_mode & 07777);
        }
    }

    /* Keep a copy of this binary in the new root for systemd's agent service
     * (fcvm-agent.service runs /.fcvm/bin/fc-init --agent). */
    char bin[64];
    snprintf(bin, sizeof(bin), "%s/.fcvm/bin", nr);
    mkdir_p(bin, 0755);
    mount("tmpfs", bin, "tmpfs", MS_NOSUID | MS_NODEV, "mode=0755,size=8m");
    strcat(bin, "/fc-init");
    copy_file("/init", bin, 0755);

    static const char *carry[] = {"/dev", "/proc"};
    for (size_t i = 0; i < sizeof(carry) / sizeof(*carry); i++) {
        char dst[64];
        snprintf(dst, sizeof(dst), "%s%s", nr, carry[i]);
        mkdir(dst, 0755);
        mount(carry[i], dst, NULL, MS_MOVE, NULL);
    }
    if (chdir(nr) < 0 || mount(".", "/", NULL, MS_MOVE, NULL) < 0 ||
        chroot(".") < 0 || chdir("/") < 0) {
        msg("switch root: %s", strerror(errno));
        return -1;
    }
    return 0;
}

static void setup_fs(void)
{
    mnt("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL);
    mnt("sysfs", "/sys", "sysfs", MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL);
    mnt("devtmpfs", "/dev", "devtmpfs", MS_NOSUID, "mode=0755");
    mnt("devpts", "/dev/pts", "devpts", MS_NOSUID | MS_NOEXEC,
        "newinstance,ptmxmode=0666,mode=0620,gid=5");
    mnt("shm", "/dev/shm", "tmpfs", MS_NOSUID | MS_NODEV, "mode=1777");
    mnt("mqueue", "/dev/mqueue", "mqueue", MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL);
    mnt("cgroup2", "/sys/fs/cgroup", "cgroup2", MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL);

    symlink("/proc/self/fd", "/dev/fd");
    symlink("/proc/self/fd/0", "/dev/stdin");
    symlink("/proc/self/fd/1", "/dev/stdout");
    symlink("/proc/self/fd/2", "/dev/stderr");
    unlink("/dev/ptmx");
    symlink("pts/ptmx", "/dev/ptmx");
}

static void setup_net(const char *hostname)
{
    int s = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    struct ifreq ifr = {0};
    strcpy(ifr.ifr_name, "lo");
    if (s >= 0 && ioctl(s, SIOCGIFFLAGS, &ifr) == 0) {
        ifr.ifr_flags |= IFF_UP;
        ioctl(s, SIOCSIFFLAGS, &ifr);
    }
    if (s >= 0)
        close(s);

    if (hostname && *hostname)
        sethostname(hostname, strlen(hostname));

    char hosts[512];
    snprintf(hosts, sizeof(hosts),
             "127.0.0.1\tlocalhost\n::1\tlocalhost ip6-localhost ip6-loopback\n"
             "127.0.1.1\t%s\n", hostname ? hostname : "");
    spit("/etc/hosts", hosts);
    if (hostname) {
        char line[300];
        snprintf(line, sizeof(line), "%s\n", hostname);
        spit("/etc/hostname", line);
    }

    char *pnp = slurp("/proc/net/pnp", NULL); /* "nameserver x.x.x.x" lines */
    if (pnp && strstr(pnp, "nameserver"))
        spit("/etc/resolv.conf", pnp);
    free(pnp);
}

static int isnum(const char *s)
{
    return *s && strspn(s, "0123456789") == strlen(s);
}

/*
 * Proxy variables for restricted VMs (fcvm.proxy=URL on the command line):
 * appended to `env` (a NULL-terminated, malloc'd vector) and returned.
 */
static char **with_proxy_env(char **env)
{
    static const char *vars[] = {"http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"};
    char *proxy = karg("fcvm.proxy");
    int n = 0;
    while (env && env[n])
        n++;
    if (!proxy)
        return env;
    char **out = calloc(n + 7, sizeof(*out));
    for (int i = 0; i < n; i++)
        out[i] = env[i];
    for (size_t i = 0; i < 4; i++)
        asprintf(&out[n++], "%s=%s", vars[i], proxy);
    out[n++] = "no_proxy=localhost,127.0.0.1,::1";
    out[n++] = "NO_PROXY=localhost,127.0.0.1,::1";
    return out;
}

/*
 * Find the /etc/passwd or /etc/group entry whose name is `name` (or, with
 * name NULL, whose id is `id`) and split it into f[]. Returns the number of
 * fields, 0 if not found. The strings stay allocated.
 */
static int db_find(const char *path, const char *name, long id, char *f[7])
{
    char *db = slurp(path, NULL), *save;
    for (char *line = db ? strtok_r(db, "\n", &save) : NULL; line; line = strtok_r(NULL, "\n", &save)) {
        char *p = line;
        int n = 0;
        while (n < 7 && (f[n] = strsep(&p, ":")))
            n++;
        if (n >= 4 && (name ? strcmp(f[0], name) == 0 : strtol(f[2], NULL, 10) == id))
            return n;
    }
    free(db);
    return 0;
}

/*
 * Docker-style USER spec (name, uid, name:group, uid:gid) -> "uid:gid[:g,g...]"
 * with supplementary groups from /etc/group, resolved inside the guest.
 * Returns NULL and fills err if a name is unknown.
 */
static char *resolve_user(const char *spec, char *err, size_t errlen)
{
    char *u = strdupa(spec), *g = strchr(u, ':'), *pw[7], *gr[7], *name = NULL;
    long uid, gid = 0;
    if (g)
        *g++ = '\0';
    if (db_find("/etc/passwd", isnum(u) ? NULL : u, atol(u), pw)) {
        name = pw[0], uid = atol(pw[2]), gid = atol(pw[3]);
    } else if (isnum(u)) {
        uid = atol(u); /* like docker: unknown numeric uid is fine, gid 0 */
    } else {
        snprintf(err, errlen, "unable to find user %s: no matching entries in passwd file", u);
        return NULL;
    }
    if (g && *g) {
        if (isnum(g))
            gid = atol(g);
        else if (db_find("/etc/group", g, 0, gr))
            gid = atol(gr[2]);
        else {
            snprintf(err, errlen, "unable to find group %s: no matching entries in group file", g);
            return NULL;
        }
    }

    size_t cap = 1024;
    char *out = malloc(cap), sep = ':';
    int n = snprintf(out, cap, "%ld:%ld", uid, gid);
    char *db = name ? slurp("/etc/group", NULL) : NULL, *save, *msave;
    for (char *line = db ? strtok_r(db, "\n", &save) : NULL; line; line = strtok_r(NULL, "\n", &save)) {
        char *f[4], *p = line;
        int k = 0;
        while (k < 4 && (f[k] = strsep(&p, ":")))
            k++;
        if (k < 4 || atol(f[2]) == gid)
            continue;
        for (char *m = strtok_r(f[3], ",", &msave); m; m = strtok_r(NULL, ",", &msave))
            if (strcmp(m, name) == 0 && n < (int)cap - 24) {
                n += snprintf(out + n, cap - n, "%c%s", sep, f[2]);
                sep = ',';
                break;
            }
    }
    free(db);
    return out;
}

/*
 * Become the image's user and exec argv with the image's env and workdir.
 * Shared by the main process and by `fcvm exec` sessions. No workdir means
 * $HOME (exec sessions on systemd images). term, if set, overrides TERM.
 */
static void exec_as(char **argv, char **env, const char *workdir, char *user,
                    const char *term)
{
    if (user && *user) {
        uid_t uid = strtoul(strtok(user, ":"), NULL, 10);
        char *g = strtok(NULL, ":");
        gid_t gid = g ? strtoul(g, NULL, 10) : 0;
        gid_t groups[64];
        int ng = 0;
        char *extra = strtok(NULL, ":");
        for (char *t = extra ? strtok(extra, ",") : NULL; t && ng < 64; t = strtok(NULL, ","))
            groups[ng++] = strtoul(t, NULL, 10);
        if (setgroups(ng, groups) < 0 || setgid(gid) < 0 || setuid(uid) < 0) {
            msg("cannot switch to %u:%u: %s", uid, gid, strerror(errno));
            _exit(126);
        }
    }

    /* execvp searches the caller's PATH, so install the image env first. */
    clearenv();
    for (char **e = env; e && *e; e++)
        putenv(*e);
    if (!getenv("PATH"))
        setenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", 1);
    if (!getenv("HOME")) { /* from passwd, as docker does */
        char *pw[7];
        int n = db_find("/etc/passwd", NULL, getuid(), pw);
        setenv("HOME", n >= 6 && *pw[5] ? pw[5] : getuid() == 0 ? "/root" : "/", 1);
    }
    if (term && *term)
        setenv("TERM", term, 1);

    if (workdir && *workdir) { /* image WORKDIR: created if missing, like docker */
        mkdir(workdir, 0755);
        if (chdir(workdir) < 0)
            msg("chdir %s: %s", workdir, strerror(errno));
    } else if (chdir(getenv("HOME")) < 0) { /* no WORKDIR: home, if it exists */
        chdir("/");
    }

    execvp(argv[0], argv);
    msg("exec %s: %s", argv[0], strerror(errno));
    _exit(127);
}

/* Make the terminal on fd owned by the "uid:gid..." user, as login/sshd do. */
static void give_tty(int fd, const char *user)
{
    if (!user || !*user)
        return;
    char *end;
    uid_t uid = strtoul(user, &end, 10);
    fchown(fd, uid, *end == ':' ? strtoul(end + 1, NULL, 10) : 0);
    fchmod(fd, 0620);
}

static pid_t start_main(char **argv, char **env, const char *workdir,
                        char *user, sigset_t *oldmask)
{
    pid_t pid = fork();
    if (pid != 0)
        return pid;

    sigprocmask(SIG_SETMASK, oldmask, NULL);
    setsid();
    ioctl(0, TIOCSCTTY, 1); /* so Ctrl-C on the serial console reaches it */
    /* Apps that reopen the console via /dev/stdout -> /proc/self/fd/1
     * (nginx logs, etc.) need the image user to own it. */
    give_tty(0, user);
    exec_as(argv, env, workdir ? workdir : "/", user, NULL);
    return -1;
}

static void shutdown_vm(void)
{
    kill(-1, SIGTERM);
    for (int i = 0; i < 20 && waitpid(-1, NULL, WNOHANG) >= 0; i++)
        usleep(100000);
    kill(-1, SIGKILL);
    sync();
    reboot(RB_AUTOBOOT); /* reboot=k: Firecracker exits */
}

/*
 * Exec agent (`fcvm exec` / `fcvm shell`): listens on vsock port AGENT_PORT
 * and runs one command per connection, like `docker exec`.
 *
 * Frames in both directions: 1 type byte, 4-byte big-endian length, payload.
 *   host -> guest  R request, NUL-separated fields: "fcvm2", tty ("0"/"1"),
 *                    rows, cols, TERM, user ("" = image default;
 *                    name|uid[:group|gid]), workdir ("" = default), N,
 *                    N extra KEY=VALUE env entries, argv...
 *                  D stdin data    C stdin closed    W window size (u16 rows, u16 cols)
 *   guest -> host  D stdout data   E stderr data     X exit status (be32)
 */
#define AGENT_PORT 1024

struct buf {
    char *d;
    size_t len, cap;
};

static int write_all(int fd, const void *p, size_t n)
{
    const char *c = p;
    while (n) {
        ssize_t w = write(fd, c, n);
        if (w < 0 && errno == EINTR)
            continue;
        if (w <= 0)
            return -1;
        c += w;
        n -= w;
    }
    return 0;
}

static int send_frame(int fd, char type, const void *p, uint32_t n)
{
    unsigned char h[5] = {type, n >> 24, n >> 16, n >> 8, n};
    return write_all(fd, h, 5) < 0 || write_all(fd, p, n) < 0 ? -1 : 0;
}

static uint32_t be32(const char *p)
{
    const unsigned char *u = (const unsigned char *)p;
    return (uint32_t)u[0] << 24 | u[1] << 16 | u[2] << 8 | u[3];
}

/* Append whatever is readable on fd; returns bytes read (0 = EOF, <0 = error). */
static ssize_t fill(struct buf *b, int fd)
{
    if (b->cap - b->len < 65536)
        b->d = realloc(b->d, b->cap += 65536);
    ssize_t n = read(fd, b->d + b->len, b->cap - b->len);
    if (n > 0)
        b->len += n;
    return n;
}

/* Length of the first complete frame in b (header included), or 0. */
static size_t frame_ready(struct buf *b)
{
    if (b->len < 5)
        return 0;
    size_t n = 5 + be32(b->d + 1);
    return b->len >= n ? n : 0;
}

static void consume(struct buf *b, size_t n)
{
    memmove(b->d, b->d + n, b->len - n);
    b->len -= n;
}

/* Forward everything readable on fd as frames of type t; returns -1 at EOF. */
static int pump(int fd, int conn, char t)
{
    char data[16384];
    ssize_t n = read(fd, data, sizeof(data));
    if (n > 0)
        return send_frame(conn, t, data, n);
    return n < 0 && (errno == EAGAIN || errno == EINTR) ? 0 : -1;
}

static void agent_session(int conn)
{
    struct buf b = {0};
    size_t flen;
    while (!(flen = frame_ready(&b)))
        if (fill(&b, conn) <= 0)
            _exit(0);
    if (b.d[0] != 'R')
        _exit(0);

    /* Request: "fcvm2", tty, rows, cols, TERM, user, workdir, N, N env, argv... */
    char *fields[300];
    int nf = 0;
    for (size_t i = 5; i < flen && nf < 299; i += strlen(b.d + i) + 1)
        fields[nf++] = strndup(b.d + i, flen - i);
    consume(&b, flen);
    fields[nf] = NULL;
    int nenv = nf >= 8 ? atoi(fields[7]) : -1;
    if (nf < 8 || strcmp(fields[0], "fcvm2") != 0 || nenv < 0 || 8 + nenv > nf) {
        static const char why[] = "fcvm: exec protocol mismatch between host and VM (restart the VM)\n";
        unsigned char x[4] = {0, 0, 0, 126};
        send_frame(conn, 'E', why, sizeof(why) - 1);
        send_frame(conn, 'X', x, 4);
        _exit(0);
    }
    int tty = fields[1][0] == '1';
    struct winsize ws = {.ws_row = atoi(fields[2]), .ws_col = atoi(fields[3])};
    char *term = fields[4];
    char **argv = &fields[8 + nenv];
    static char *bash[] = {"/bin/bash", NULL}, *sh[] = {"/bin/sh", NULL};
    if (!*argv)
        argv = access("/bin/bash", X_OK) == 0 ? bash : sh;

    /* Image env, then exec -e entries (later putenv wins). */
    size_t elen = 0;
    char *ebuf = slurp(CONF "env", &elen);
    char **image_env = ebuf && elen ? split0(ebuf, elen) : NULL;
    int ni = 0;
    while (image_env && image_env[ni])
        ni++;
    image_env = with_proxy_env(image_env);
    ni = 0;
    while (image_env && image_env[ni])
        ni++;
    char **env = calloc(ni + nenv + 1, sizeof(*env));
    for (int i = 0; i < ni; i++)
        env[i] = image_env[i];
    for (int i = 0; i < nenv; i++)
        env[ni + i] = fields[8 + i];

    char *workdir = *fields[6] ? fields[6] : trim(slurp(CONF "workdir", NULL));
    char *user = trim(slurp(CONF "user", NULL));
    if (*fields[5]) { /* exec -u */
        char why[300];
        if (!(user = resolve_user(fields[5], why, sizeof(why)))) {
            unsigned char x[4] = {0, 0, 0, 126};
            strcat(why, "\n");
            send_frame(conn, 'E', why, strlen(why));
            send_frame(conn, 'X', x, 4);
            _exit(0);
        }
    }

    int in = -1, out = -1, err = -1;
    pid_t pid;
    if (tty) {
        int m = posix_openpt(O_RDWR | O_NOCTTY | O_CLOEXEC);
        if (m < 0 || grantpt(m) < 0 || unlockpt(m) < 0)
            _exit(1);
        ioctl(m, TIOCSWINSZ, &ws);
        char *slave = ptsname(m);
        if ((pid = fork()) == 0) {
            setsid();
            int s = open(slave, O_RDWR);
            ioctl(s, TIOCSCTTY, 0);
            give_tty(s, user);
            dup2(s, 0), dup2(s, 1), dup2(s, 2);
            if (s > 2)
                close(s);
            exec_as(argv, env, workdir, user, term);
        }
        in = out = m;
    } else {
        int pi[2], po[2], pe[2];
        if (pipe2(pi, O_CLOEXEC) < 0 || pipe2(po, O_CLOEXEC) < 0 || pipe2(pe, O_CLOEXEC) < 0)
            _exit(1);
        if ((pid = fork()) == 0) {
            setsid();
            dup2(pi[0], 0), dup2(po[1], 1), dup2(pe[1], 2);
            exec_as(argv, env, workdir, user, NULL);
        }
        close(pi[0]), close(po[1]), close(pe[1]);
        in = pi[1], out = po[0], err = pe[0];
    }

    int status = 0, out_open = 1;
    for (;;) {
        struct pollfd p[3] = {{conn, POLLIN, 0}, {out_open ? out : -1, POLLIN, 0}, {err, POLLIN, 0}};
        poll(p, 3, 100);
        if (p[0].revents && fill(&b, conn) <= 0) { /* host went away (or --timeout) */
            kill(-pid, SIGHUP);
            usleep(200000);
            kill(-pid, SIGKILL);
            _exit(0);
        }
        { /* frames may already be buffered along with the request */
            while ((flen = frame_ready(&b))) {
                uint32_t n = flen - 5;
                char *d = b.d + 5;
                if (b.d[0] == 'D' && in >= 0)
                    write_all(in, d, n);
                else if (b.d[0] == 'C' && !tty && in >= 0)
                    close(in), in = -1;
                else if (b.d[0] == 'W' && tty && n == 4) {
                    struct winsize w = {.ws_row = (unsigned char)d[0] << 8 | (unsigned char)d[1],
                                        .ws_col = (unsigned char)d[2] << 8 | (unsigned char)d[3]};
                    ioctl(out, TIOCSWINSZ, &w);
                }
                consume(&b, flen);
            }
        }
        if (p[1].revents && pump(out, conn, 'D') < 0)
            out_open = 0; /* pty: EIO once the session's last process closes it */
        if (p[2].revents && pump(err, conn, 'E') < 0)
            close(err), err = -1;
        if (waitpid(pid, &status, WNOHANG) == pid)
            break;
    }
    /* Drain output the command wrote just before exiting. */
    if (out_open) {
        fcntl(out, F_SETFL, O_NONBLOCK);
        while (pump(out, conn, 'D') == 0 && poll(&(struct pollfd){out, POLLIN, 0}, 1, 0) > 0)
            ;
    }
    if (err >= 0) {
        fcntl(err, F_SETFL, O_NONBLOCK);
        while (pump(err, conn, 'E') == 0 && poll(&(struct pollfd){err, POLLIN, 0}, 1, 0) > 0)
            ;
    }
    int code = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
    unsigned char x[4] = {code >> 24, code >> 16, code >> 8, code};
    send_frame(conn, 'X', x, 4);
    _exit(0);
}

static int agent_main(void)
{
    int s = socket(AF_VSOCK, SOCK_STREAM | SOCK_CLOEXEC, 0);
    struct sockaddr_vm addr = {.svm_family = AF_VSOCK, .svm_cid = VMADDR_CID_ANY,
                               .svm_port = AGENT_PORT};
    if (s < 0 || bind(s, (struct sockaddr *)&addr, sizeof(addr)) < 0 || listen(s, 16) < 0) {
        msg("exec agent: vsock port %d: %s", AGENT_PORT, strerror(errno));
        return 1;
    }
    signal(SIGCHLD, SIG_IGN); /* sessions are reaped automatically */
    for (;;) {
        int c = accept4(s, NULL, NULL, SOCK_CLOEXEC);
        if (c < 0)
            continue;
        if (fork() == 0) {
            close(s);
            signal(SIGCHLD, SIG_DFL);
            agent_session(c);
        }
        close(c);
    }
}

static void idle_stop(int sig)
{
    (void)sig;
    _exit(0);
}

int main(int argc, char **argv_)
{
    if (argc > 1 && strcmp(argv_[1], "--agent") == 0)
        return agent_main(); /* systemd images run the agent as a service */
    if (argc > 1 && strcmp(argv_[1], "--idle") == 0) {
        /* `fcvm create --idle`: keep a container VM up for exec, stop cleanly */
        signal(SIGTERM, idle_stop);
        signal(SIGINT, idle_stop);
        for (;;)
            pause();
    }
    if (getpid() != 1) {
        fprintf(stderr, "usage: fc-init (as PID 1) | fc-init --agent\n");
        return 1;
    }
    mnt("devtmpfs", "/dev", "devtmpfs", MS_NOSUID, "mode=0755");
    mnt("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL);
    char *exec = karg("fcvm.exec");
    if (assemble_root() < 0) {
        msg("cannot assemble the root filesystem; halting");
        shutdown_vm();
    }
    if (exec) {
        char *init_argv[] = {exec, NULL};
        execv(exec, init_argv);
        msg("exec %s: %s", exec, strerror(errno));
        shutdown_vm();
    }

    setup_fs();
    reboot(RB_DISABLE_CAD); /* Ctrl-Alt-Del arrives as SIGINT: graceful stop */
    unlink(CONF "exit-status");

    size_t alen = 0, elen = 0;
    char *abuf = slurp(CONF "argv", &alen);
    char *ebuf = slurp(CONF "env", &elen);
    char *workdir = trim(slurp(CONF "workdir", NULL));
    char *user = trim(slurp(CONF "user", NULL));
    char *hostname = trim(slurp(CONF "hostname", NULL));

    setup_net(hostname);

    static char *fallback[] = {"/bin/sh", NULL};
    char **argv = abuf && alen ? split0(abuf, alen) : fallback;
    char **env = with_proxy_env(ebuf && elen ? split0(ebuf, elen) : NULL);

    sigset_t all, old;
    sigfillset(&all);
    sigprocmask(SIG_BLOCK, &all, &old);

    if (fork() == 0) { /* exec agent for `fcvm exec` / `fcvm shell` */
        sigprocmask(SIG_SETMASK, &old, NULL);
        _exit(agent_main());
    }

    pid_t main_pid = start_main(argv, env, workdir, user, &old);
    if (main_pid < 0) {
        msg("fork: %s", strerror(errno));
        shutdown_vm();
    }

    int status = 0;
    for (;;) {
        siginfo_t si;
        int sig = sigwaitinfo(&all, &si);
        if (sig == SIGCHLD) {
            pid_t p;
            int st;
            while ((p = waitpid(-1, &st, WNOHANG)) > 0)
                if (p == main_pid) {
                    status = st;
                    goto done;
                }
        } else if (sig == SIGINT || sig == SIGTERM || sig == SIGPWR) {
            kill(main_pid, SIGTERM); /* Ctrl-Alt-Del / stop request */
        } else if (sig > 0) {
            kill(main_pid, sig);
        }
    }
done:;
    int code = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
    if (WIFEXITED(status))
        msg("%s exited with status %d", argv[0], code);
    else
        msg("%s killed by signal %d", argv[0], WTERMSIG(status));
    char buf[16];
    snprintf(buf, sizeof(buf), "%d\n", code);
    spit(CONF "exit-status", buf);
    shutdown_vm();
    return 0;
}
