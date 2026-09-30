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
 *   fcvm.shares=PORT:PATH[:ro],...  live host directories over vsock + 9P
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
#include <arpa/inet.h>
#include <net/if.h>
#include <net/if_arp.h>
#include <net/route.h>
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
#include <sys/xattr.h>
#include <termios.h>
#include <time.h>
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
static void shutdown_vm(void);

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
        int err = errno;
        /* A read-only disk whose journal needs replaying (its last user
         * crashed) can't be recovered in place: mount it without replaying,
         * rather than failing the boot. The newest writes may be missing. */
        if ((flags & MS_RDONLY) && mount(dev, dir, "ext4", flags, "norecovery") == 0) {
            msg("%s needs journal recovery, which a read-only disk can't do; mounted without it", dev);
            return 0;
        }
        msg("mount %s on %s: %s", dev, dir, strerror(err));
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
 * Layer merge (`fcvm squash`), run from the initramfs with no root filesystem:
 *   fcvm.merge=DEV,DEV,...  committed layers, OLDEST first (ext4, upper/)
 *   fcvm.merge_out=DEV      empty layer disk; receives the merged upper/
 * Applies each layer onto the output in order with overlayfs semantics, and
 * keeps whiteouts and opaque directories, so deletions of files in the base
 * image below survive. Writes /merge-ok on the output when there were no
 * errors, then reboots (Firecracker exits).
 */
static int merge_errors;
struct hardlink {
    dev_t dev;
    ino_t ino;
    char *path;
};
static struct hardlink *links;
static size_t nlinks;

static void merr(const char *what, const char *path)
{
    msg("merge: %s %s: %s", what, path, strerror(errno));
    merge_errors++;
}

static void remove_all(const char *path)
{
    struct stat st;
    if (lstat(path, &st) < 0)
        return;
    if (S_ISDIR(st.st_mode)) {
        DIR *d = opendir(path);
        struct dirent *e;
        while (d && (e = readdir(d))) {
            if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, ".."))
                continue;
            char p[4096];
            snprintf(p, sizeof(p), "%s/%s", path, e->d_name);
            remove_all(p);
        }
        if (d)
            closedir(d);
        rmdir(path);
    } else {
        unlink(path);
    }
}

/* Owner, then mode (chown clears setuid), then xattrs (chown clears file
 * capabilities), then times. Of overlayfs' own xattrs only "opaque" matters. */
static void copy_meta(const char *src, const char *dst, const struct stat *st)
{
    if (lchown(dst, st->st_uid, st->st_gid) < 0)
        merr("chown", dst);
    if (!S_ISLNK(st->st_mode) && chmod(dst, st->st_mode & 07777) < 0)
        merr("chmod", dst);
    static char names[65536], val[65536];
    ssize_t n = llistxattr(src, names, sizeof(names));
    for (char *k = names; n > 0 && k < names + n; k += strlen(k) + 1) {
        if (!strncmp(k, "trusted.overlay.", 16) && strcmp(k, "trusted.overlay.opaque"))
            continue;
        ssize_t vl = lgetxattr(src, k, val, sizeof(val));
        if (vl >= 0 && lsetxattr(dst, k, val, vl, 0) < 0 && errno != ENOTSUP)
            merr("setxattr", dst);
    }
    struct timespec ts[2] = {st->st_atim, st->st_mtim};
    utimensat(AT_FDCWD, dst, ts, AT_SYMLINK_NOFOLLOW);
}

static int copy_data(const char *src, const char *dst)
{
    static char buf[1 << 20];
    int in = open(src, O_RDONLY | O_CLOEXEC), out = -1, ok = 0;
    if (in >= 0)
        out = open(dst, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (out >= 0) {
        ssize_t r;
        ok = 1;
        while ((r = read(in, buf, sizeof(buf))) > 0)
            if (write_all(out, buf, r) < 0) {
                ok = 0;
                break;
            }
        if (r < 0)
            ok = 0;
    }
    if (in >= 0)
        close(in);
    if (out >= 0)
        close(out);
    return ok ? 0 : -1;
}

static void merge_dir(const char *src, const char *dst)
{
    DIR *d = opendir(src);
    struct dirent *e;
    if (!d) {
        merr("opendir", src);
        return;
    }
    while ((e = readdir(d))) {
        if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, ".."))
            continue;
        char s[4096], t[4096];
        snprintf(s, sizeof(s), "%s/%s", src, e->d_name);
        snprintf(t, sizeof(t), "%s/%s", dst, e->d_name);
        struct stat st, tst;
        if (lstat(s, &st) < 0) {
            merr("lstat", s);
            continue;
        }
        int have = lstat(t, &tst) == 0;
        if (S_ISCHR(st.st_mode) && st.st_rdev == 0) { /* whiteout: keep it */
            remove_all(t);
            if (mknod(t, S_IFCHR, 0) < 0)
                merr("whiteout", t);
            continue;
        }
        if (S_ISDIR(st.st_mode)) {
            char v[4];
            int opaque = lgetxattr(s, "trusted.overlay.opaque", v, sizeof(v)) == 1 && v[0] == 'y';
            if (have && (!S_ISDIR(tst.st_mode) || opaque)) {
                remove_all(t);
                have = 0;
            }
            if (!have && mkdir(t, 0700) < 0) {
                merr("mkdir", t);
                continue;
            }
            merge_dir(s, t);
            copy_meta(s, t, &st); /* after the children, so times stick */
            continue;
        }
        if (have)
            remove_all(t);
        if (S_ISREG(st.st_mode)) {
            size_t i;
            for (i = 0; st.st_nlink > 1 && i < nlinks; i++)
                if (links[i].dev == st.st_dev && links[i].ino == st.st_ino)
                    break;
            if (st.st_nlink > 1 && i < nlinks) {
                if (link(links[i].path, t) < 0)
                    merr("link", t);
                continue;
            }
            if (copy_data(s, t) < 0) {
                merr("copy", s);
                continue;
            }
            if (st.st_nlink > 1) {
                links = realloc(links, (nlinks + 1) * sizeof(*links));
                links[nlinks++] = (struct hardlink){st.st_dev, st.st_ino, strdup(t)};
            }
        } else if (S_ISLNK(st.st_mode)) {
            char target[4096];
            ssize_t n = readlink(s, target, sizeof(target) - 1);
            if (n < 0 || (target[n] = '\0', symlink(target, t) < 0)) {
                merr("symlink", t);
                continue;
            }
        } else if (mknod(t, st.st_mode & S_IFMT, st.st_rdev) < 0) {
            merr("mknod", t);
            continue;
        }
        copy_meta(s, t, &st);
    }
    closedir(d);
}

static void merge_main(void)
{
    char *layers = karg("fcvm.merge"), *out = karg("fcvm.merge_out"), *save;
    int n = 0;
    if (!out || mount_dev(out, "/mnt/out", MS_NOATIME) < 0) {
        msg("merge: no output disk");
        shutdown_vm();
    }
    mkdir("/mnt/out/upper", 0755);
    for (char *dev = strtok_r(layers, ",", &save); dev; dev = strtok_r(NULL, ",", &save)) {
        char dir[32], upper[48];
        snprintf(dir, sizeof(dir), "/mnt/m%d", n++);
        snprintf(upper, sizeof(upper), "%s/upper", dir);
        if (mount_dev(dev, dir, MS_RDONLY) < 0) {
            merge_errors++;
            break;
        }
        nlinks = 0; /* hardlinks are per layer */
        merge_dir(upper, "/mnt/out/upper");
    }
    if (!merge_errors)
        spit("/mnt/out/merge-ok", "ok\n");
    sync();
    umount("/mnt/out");
    msg("merged %d layer(s), %d error(s)", n, merge_errors);
    shutdown_vm();
}

/*
 * Live host directories (fcvm.shares=PORT:PATH[:ro],...): connect to the host
 * over vsock (Firecracker hands the connection to <vsock uds>_PORT, where
 * lib/share9p.py serves the directory) and give the socket to the kernel's 9P
 * client with trans=fd. `owner` ("uid:gid") is who the files appear to belong
 * to, passed to the server as the attach name.
 */
/* Mount one host directory: vsock port -> 9P mount at path. 0 or -errno. */
static int db_find(const char *path, const char *name, long id, char *f[7]);

/* "~" or "~/x" in a guest mount path: the home, from the image's own
 * /etc/passwd, of the user owning the share ("uid:gid"), so a host
 * directory can be mounted in whatever user's home the image has. */
static const char *home_path(const char *path, const char *owner, char *buf, size_t n)
{
    if (path[0] != '~' || (path[1] && path[1] != '/'))
        return path;
    long uid = strtol(owner, NULL, 10);
    char *pw[7];
    const char *home = uid == 0 ? "/root" : "/";
    if (db_find("/etc/passwd", NULL, uid, pw) >= 6 && pw[5][0] == '/')
        home = pw[5];
    snprintf(buf, n, "%s%s", strcmp(home, "/") ? home : "", path[1] ? path + 1 : (strcmp(home, "/") ? "" : "/"));
    return buf;
}

static int mount_share(unsigned port, const char *path, int ro, const char *owner)
{
    char expanded[4096];
    path = home_path(path, owner, expanded, sizeof(expanded));
    struct sockaddr_vm addr = {.svm_family = AF_VSOCK, .svm_cid = VMADDR_CID_HOST, .svm_port = port};
    int s = -1, ok = 0;
    /* The host's server may still be starting (jailed VMs get it after launch): retry ~3 s. */
    for (int i = 0; i < 30 && !ok; i++) {
        if (s >= 0)
            close(s);
        s = socket(AF_VSOCK, SOCK_STREAM, 0); /* no CLOEXEC: the kernel takes it over */
        ok = s >= 0 && connect(s, (struct sockaddr *)&addr, sizeof(addr)) == 0;
        if (!ok)
            usleep(100000);
    }
    if (!ok) {
        int e = errno;
        msg("share %s: cannot reach the host (vsock port %u): %s", path, port, strerror(e));
        if (s >= 0)
            close(s);
        return -e;
    }
    char opts[256];
    snprintf(opts, sizeof(opts),
             "trans=fd,rfdno=%d,wfdno=%d,version=9p2000.L,msize=524288,cache=mmap,access=client,aname=%s",
             s, s, owner);
    mkdir_p(path, 0755);
    int rc = mount("fcvm-share", path, "9p", ro ? MS_RDONLY : 0, opts) < 0 ? -errno : 0;
    if (rc)
        msg("share %s: mount: %s", path, strerror(-rc));
    close(s); /* the mount holds its own reference */
    return rc;
}

static void mount_shares(const char *owner)
{
    char *shares = karg("fcvm.shares"), *save;
    for (char *sh = shares ? strtok_r(shares, ",", &save) : NULL; sh; sh = strtok_r(NULL, ",", &save)) {
        char *path = strchr(sh, ':');
        if (!path)
            continue;
        *path++ = '\0';
        char *opt = strchr(path, ':');
        if (opt)
            *opt++ = '\0';
        mount_share((unsigned)atoi(sh), path, opt && strcmp(opt, "ro") == 0, owner);
    }
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
 * The image's default user from /.fcvm/user: "uid:gid[:gid,...]" as written
 * by import, or a USER spec (name, name:group, uid) as written by fcvm build,
 * resolved here against the image's own /etc/passwd.
 */
static char *config_user(void)
{
    char *u = trim(slurp(CONF "user", NULL)), why[300];
    if (!u || !*u || strspn(u, "0123456789:,") == strlen(u))
        return u;
    char *r = resolve_user(u, why, sizeof(why));
    if (!r)
        msg("%s; running as root", why);
    return r;
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

/* Unmount ext4 filesystems, newest first, so their journals close cleanly: a
 * volume left "needs recovery" couldn't be mounted read-only by the next VM.
 * What can't be unmounted (the disks under the root overlay) is remounted
 * read-only instead, which also flushes and closes the journal. */
static void unmount_disks(void)
{
    mount(NULL, "/", NULL, MS_REMOUNT | MS_RDONLY, NULL);   /* the overlay root first, */
    char *m = slurp("/proc/self/mounts", NULL);                  /* so the disks under it can close */
    if (!m)
        return;
    char *dirs[64];
    int n = 0;
    for (char *line = strtok(m, "\n"); line && n < 64; line = strtok(NULL, "\n")) {
        char dev[256], dir[1024], type[64];
        if (sscanf(line, "%255s %1023s %63s", dev, dir, type) == 3 && !strcmp(type, "ext4"))
            dirs[n++] = strdup(dir);
    }
    for (int i = n - 1; i >= 0; i--) {
        if (umount2(dirs[i], 0) < 0)
            mount(NULL, dirs[i], NULL, MS_REMOUNT | MS_RDONLY, NULL);
        free(dirs[i]);
    }
    free(m);
}

static void shutdown_vm(void)
{
    kill(-1, SIGTERM);
    for (int i = 0; i < 20 && waitpid(-1, NULL, WNOHANG) >= 0; i++)
        usleep(100000);
    kill(-1, SIGKILL);
    while (waitpid(-1, NULL, WNOHANG) > 0)
        ;
    sync();
    unmount_disks();
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

/* Point /etc/hosts' 127.0.1.1 line at HOST (adding it if missing), so the
 * VM's own name resolves (sudo complains otherwise). The rest is kept. */
static void hosts_entry(const char *host)
{
    char *h = slurp("/etc/hosts", NULL), *out = NULL;
    char *line = h ? strstr(h, "127.0.1.1") : NULL;
    int n;
    if (line && (line == h || line[-1] == '\n')) {
        *line = '\0';
        char *rest = strchr(line + 1, '\n');
        n = asprintf(&out, "%s127.0.1.1\t%s\n%s", h, host, rest ? rest + 1 : "");
    } else {
        size_t len = h ? strlen(h) : 0;
        n = asprintf(&out, "%s%s127.0.1.1\t%s\n", h ? h : "", len && h[len - 1] != '\n' ? "\n" : "", host);
    }
    if (n >= 0)
        spit("/etc/hosts", out);
    free(out);
    free(h);
}

/*
 * Re-identify a VM restored from a snapshot (fcvm fork): new address, MAC and
 * hostname, applied with plain ioctls so it works in any image. Fields:
 * "fcvm2", ip, prefix length, gateway, mac, hostname (ip "" = no network),
 * and optionally the host's time ("SECONDS.NANOSECONDS"): the guest clock
 * stopped at the snapshot, and Firecracker's own fix for that (clock_realtime)
 * needs a TSC-clocked host, which nested hosts such as cloud VMs aren't.
 */
static int netconf(char **f, char *err, size_t errlen)
{
    const char *ip = f[1], *gw = f[3], *mac = f[4], *host = f[5];
    int prefix = atoi(f[2]);
    if (f[6] && *f[6]) {
        char *end;
        struct timespec ts = {.tv_sec = strtoll(f[6], &end, 10)};
        if (*end == '.')
            ts.tv_nsec = strtol(end + 1, NULL, 10);
        clock_settime(CLOCK_REALTIME, &ts);
    }
    if (*host) {
        char old[256] = "", hosts[1024];
        gethostname(old, sizeof(old));
        sethostname(host, strlen(host));
        char *cur = slurp("/etc/hostname", NULL);
        if (cur && strcmp(trim(cur), old) == 0) { /* ours, not a distro default */
            snprintf(hosts, sizeof(hosts), "%s\n", host);
            spit("/etc/hostname", hosts);
        }
        free(cur);
        hosts_entry(host);
    }
    if (!*ip)
        return 0;

    int s = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    struct ifreq ifr = {0};
    strcpy(ifr.ifr_name, "eth0");
#define IOC(req, what) if (ioctl(s, req, &ifr) < 0) { snprintf(err, errlen, "%s: %s", what, strerror(errno)); close(s); return -1; }
    IOC(SIOCGIFFLAGS, "get flags");
    ifr.ifr_flags &= ~IFF_UP;
    IOC(SIOCSIFFLAGS, "link down");
    if (*mac) {
        unsigned int m[6];
        if (sscanf(mac, "%x:%x:%x:%x:%x:%x", &m[0], &m[1], &m[2], &m[3], &m[4], &m[5]) != 6) {
            snprintf(err, errlen, "bad mac %s", mac);
            close(s);
            return -1;
        }
        ifr.ifr_hwaddr.sa_family = ARPHRD_ETHER;
        for (int i = 0; i < 6; i++)
            ifr.ifr_hwaddr.sa_data[i] = m[i];
        IOC(SIOCSIFHWADDR, "set mac");
    }
    struct sockaddr_in *sin = (struct sockaddr_in *)&ifr.ifr_addr;
    memset(&ifr.ifr_addr, 0, sizeof(ifr.ifr_addr));
    sin->sin_family = AF_INET;
    inet_pton(AF_INET, ip, &sin->sin_addr);
    IOC(SIOCSIFADDR, "set address");
    sin->sin_addr.s_addr = htonl(prefix ? ~0u << (32 - prefix) : 0);
    IOC(SIOCSIFNETMASK, "set netmask");
    IOC(SIOCGIFFLAGS, "get flags");
    ifr.ifr_flags |= IFF_UP | IFF_RUNNING;
    IOC(SIOCSIFFLAGS, "link up");
#undef IOC
    if (*gw) {
        struct rtentry rt = {0};
        struct sockaddr_in *dst = (struct sockaddr_in *)&rt.rt_dst, *mask = (struct sockaddr_in *)&rt.rt_genmask,
                           *via = (struct sockaddr_in *)&rt.rt_gateway;
        dst->sin_family = mask->sin_family = via->sin_family = AF_INET;
        inet_pton(AF_INET, gw, &via->sin_addr);
        rt.rt_flags = RTF_UP | RTF_GATEWAY;
        rt.rt_dev = "eth0";
        if (ioctl(s, SIOCADDRT, &rt) < 0 && errno != EEXIST) {
            snprintf(err, errlen, "default route: %s", strerror(errno));
            close(s);
            return -1;
        }
    }
    close(s);
    return 0;
}

/*
 * File operations for fcvm's file browser and live mounts (request type 'F'),
 * done natively so they work in any image, even ones without sh or ls.
 * Fields: "fcvm2", op, args...  Replies: 'D' data, 'E' error text, 'X' status
 * (0 or an errno value).
 *   list DIR | stat PATH   one line per entry:
 *                          type mode uid gid size mtime name linktarget (tab-separated)
 *   read PATH              the file's bytes as 'D' frames
 *   write PATH MODE        'D' frames, then 'C': written to a temp file and
 *                          renamed into place; keeps an existing file's owner
 *                          and mode, new files take their directory's owner
 *   mkdir PATH | remove PATH (recursive) | rename FROM TO
 *   mount PORT PATH ro|rw  live host directory (see mount_share)
 *   umount PATH
 */
static void entry_line(char *out, size_t cap, const char *dir, const char *name)
{
    char full[4096], target[1024] = "", clean[512];
    struct stat st;
    snprintf(full, sizeof(full), "%s/%s", strcmp(dir, "/") ? dir : "", name);
    if (lstat(full, &st) < 0) {
        *out = '\0';
        return;
    }
    if (S_ISLNK(st.st_mode)) {
        ssize_t n = readlink(full, target, sizeof(target) - 1);
        target[n > 0 ? n : 0] = '\0';
    }
    snprintf(clean, sizeof(clean), "%s", name);
    for (char *c = clean; *c; c++)
        if (*c == '\t' || *c == '\n')
            *c = '?';
    for (char *c = target; *c; c++)
        if (*c == '\t' || *c == '\n')
            *c = '?';
    char t = S_ISDIR(st.st_mode) ? 'd' : S_ISLNK(st.st_mode) ? 'l' : S_ISREG(st.st_mode) ? 'f' :
             S_ISCHR(st.st_mode) ? 'c' : S_ISBLK(st.st_mode) ? 'b' : S_ISFIFO(st.st_mode) ? 'p' : 's';
    snprintf(out, cap, "%c\t%o\t%u\t%u\t%lld\t%lld\t%s\t%s\n", t, st.st_mode & 07777, st.st_uid, st.st_gid,
             (long long)st.st_size, (long long)st.st_mtime, clean, target);
}

static int file_op(int conn, struct buf *b, char **f, int nf)
{
    const char *op = f[1], *path = nf > 2 ? f[2] : "";
    const char *p = strcmp(op, "mount") ? path : nf > 3 ? f[3] : "";   /* mount: PORT PATH ro|rw */
    int home_ok = !strcmp(op, "mount") || !strcmp(op, "umount");       /* ~ = the image user's home */
    if (*p != '/' && !(home_ok && *p == '~'))
        return EINVAL;
    if (!strcmp(op, "list")) {
        DIR *d = opendir(path);
        struct dirent *e;
        if (!d)
            return errno;
        char out[65536], line[5000];
        size_t n = 0;
        while ((e = readdir(d))) {
            if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, ".."))
                continue;
            entry_line(line, sizeof(line), path, e->d_name);
            size_t l = strlen(line);
            if (n + l > sizeof(out)) {
                send_frame(conn, 'D', out, n);
                n = 0;
            }
            memcpy(out + n, line, l);
            n += l;
        }
        closedir(d);
        if (n)
            send_frame(conn, 'D', out, n);
        return 0;
    }
    if (!strcmp(op, "stat")) {
        char line[5000], dir[4096];
        snprintf(dir, sizeof(dir), "%s", path);
        char *slash = strrchr(dir, '/');
        const char *name = slash[1] ? slash + 1 : "";
        if (!*name) { /* "/" itself */
            struct stat st;
            if (stat("/", &st) < 0)
                return errno;
            snprintf(line, sizeof(line), "d\t%o\t%u\t%u\t%lld\t%lld\t/\t\n", st.st_mode & 07777, st.st_uid,
                     st.st_gid, (long long)st.st_size, (long long)st.st_mtime);
        } else {
            *slash = '\0';
            entry_line(line, sizeof(line), *dir ? dir : "/", name);
            if (!*line)
                return ENOENT;
        }
        send_frame(conn, 'D', line, strlen(line));
        return 0;
    }
    if (!strcmp(op, "read")) {
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        struct stat st;
        if (fd < 0)
            return errno;
        if (fstat(fd, &st) == 0 && S_ISDIR(st.st_mode)) {
            close(fd);
            return EISDIR;
        }
        char data[65536];
        ssize_t n;
        while ((n = read(fd, data, sizeof(data))) > 0)
            if (send_frame(conn, 'D', data, n) < 0)
                break;
        int e = n < 0 ? errno : 0;
        close(fd);
        return e;
    }
    if (!strcmp(op, "write")) {
        char tmp[4200], dir[4096];
        snprintf(dir, sizeof(dir), "%s", path);
        char *slash = strrchr(dir, '/');
        *slash = '\0';
        struct stat old, parent;
        int existed = stat(path, &old) == 0;
        if (existed && S_ISDIR(old.st_mode))
            return EISDIR;
        if (stat(*dir ? dir : "/", &parent) < 0)
            return errno;
        snprintf(tmp, sizeof(tmp), "%s/.fcvm-upload-%d", *dir ? dir : "", getpid());
        mode_t mode = nf > 3 ? (mode_t)strtoul(f[3], NULL, 8) : 0644;
        int fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, existed ? old.st_mode & 07777 : mode);
        if (fd < 0)
            return errno;
        int done = 0, e = 0;
        while (!done) {
            size_t flen;
            while (!done && (flen = frame_ready(b))) {
                if (b->d[0] == 'D' && write_all(fd, b->d + 5, flen - 5) < 0)
                    e = errno;
                else if (b->d[0] == 'C')
                    done = 1;
                consume(b, flen);
            }
            if (!done && fill(b, conn) <= 0) { /* host gave up: leave nothing behind */
                close(fd);
                unlink(tmp);
                return ECONNRESET;
            }
        }
        if (!e) {
            fchown(fd, existed ? old.st_uid : parent.st_uid, existed ? old.st_gid : parent.st_gid);
            fchmod(fd, existed ? old.st_mode & 07777 : mode);
            if (fsync(fd) < 0)
                e = errno;
        }
        close(fd);
        if (!e && rename(tmp, path) < 0)
            e = errno;
        if (e)
            unlink(tmp);
        return e;
    }
    if (!strcmp(op, "mkdir")) {
        char dir[4096];
        struct stat parent;
        snprintf(dir, sizeof(dir), "%s", path);
        *strrchr(dir, '/') = '\0';
        if (mkdir(path, 0755) < 0)
            return errno;
        if (stat(*dir ? dir : "/", &parent) == 0)
            chown(path, parent.st_uid, parent.st_gid);
        return 0;
    }
    if (!strcmp(op, "remove")) {
        struct stat st;
        if (lstat(path, &st) < 0)
            return errno;
        if (!strcmp(path, "/"))
            return EPERM;
        remove_all(path);
        return lstat(path, &st) == 0 ? EBUSY : 0;
    }
    if (!strcmp(op, "rename") && nf > 3)
        return rename(path, f[3]) < 0 ? errno : 0;
    char owner[64] = "0:0", expanded[4096];
    {   /* whose home a "~" mount path means: the image's user (root for system images) */
        char *u = config_user();
        if (u && *u) {
            unsigned long uid = strtoul(u, &u, 10), gid = *u == ':' ? strtoul(u + 1, NULL, 10) : 0;
            snprintf(owner, sizeof(owner), "%lu:%lu", uid, gid);
        }
    }
    if (!strcmp(op, "mount") && nf > 4)
        return -mount_share((unsigned)atoi(f[2]), f[3], strcmp(f[4], "ro") == 0, owner);
    if (!strcmp(op, "umount"))
        return umount2(home_path(path, owner, expanded, sizeof(expanded)), MNT_DETACH) < 0 ? errno : 0;
    return ENOSYS;
}

static void agent_session(int conn)
{
    struct buf b = {0};
    size_t flen;
    while (!(flen = frame_ready(&b)))
        if (fill(&b, conn) <= 0)
            _exit(0);
    if (b.d[0] == 'F') { /* file operations */
        char *f[8] = {0};
        int nf = 0;
        for (size_t i = 5; i < flen && nf < 7; i += strlen(b.d + i) + 1)
            f[nf++] = strndup(b.d + i, flen - i);
        consume(&b, flen);
        int e = nf >= 2 && strcmp(f[0], "fcvm2") == 0 ? file_op(conn, &b, f, nf) : EINVAL;
        if (e) {
            char why[300];
            snprintf(why, sizeof(why), "%s\n", strerror(e));
            send_frame(conn, 'E', why, strlen(why));
        }
        unsigned char x[4] = {e >> 24, e >> 16, e >> 8, e};
        send_frame(conn, 'X', x, 4);
        _exit(0);
    }
    if (b.d[0] == 'N') { /* netconf: re-identify after a snapshot restore */
        char *f[8] = {0}, err[300] = "";
        int nf = 0;
        for (size_t i = 5; i < flen && nf < 7; i += strlen(b.d + i) + 1)
            f[nf++] = b.d + i;
        unsigned char x[4] = {0, 0, 0, 0};
        if (nf < 6 || strcmp(f[0], "fcvm2") != 0) {
            snprintf(err, sizeof(err), "bad netconf request");
        } else if (netconf(f, err, sizeof(err)) < 0) {
            x[3] = 1;
        }
        if (*err) {
            strcat(err, "\n");
            send_frame(conn, 'E', err, strlen(err));
            x[3] = 1;
        }
        send_frame(conn, 'X', x, 4);
        _exit(0);
    }
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
    char *user = config_user();
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
    if (karg("fcvm.merge"))
        merge_main(); /* fcvm squash: never returns */
    char *exec = karg("fcvm.exec");
    if (assemble_root() < 0) {
        msg("cannot assemble the root filesystem; halting");
        shutdown_vm();
    }
    if (exec) {
        mount_shares("0:0"); /* system images: files appear owned by root */
        char *host = karg("systemd.hostname");
        if (host && *host)
            hosts_entry(host);
        char *init_argv[] = {exec, NULL};
        execv(exec, init_argv);
        msg("exec %s: %s", exec, strerror(errno));
        shutdown_vm();
    }

    setup_fs();
    {   /* app images: shared files appear owned by the image's user */
        char *u = config_user(), owner[64] = "0:0";
        if (u && *u) {
            unsigned long uid = strtoul(u, &u, 10), gid = *u == ':' ? strtoul(u + 1, NULL, 10) : 0;
            snprintf(owner, sizeof(owner), "%lu:%lu", uid, gid);
        }
        mount_shares(owner);
    }
    reboot(RB_DISABLE_CAD); /* Ctrl-Alt-Del arrives as SIGINT: graceful stop */
    unlink(CONF "exit-status");

    size_t alen = 0, elen = 0;
    char *abuf = slurp(CONF "argv", &alen);
    char *ebuf = slurp(CONF "env", &elen);
    char *workdir = trim(slurp(CONF "workdir", NULL));
    char *user = config_user();
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
