#include "cgroup.h"

#include "util.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>

#ifdef __linux__
#include <ctype.h>
#include <fcntl.h>
#include <limits.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define CGROUP_ROOT "/sys/fs/cgroup"

static int is_skip_errno(int err) {
    return err == EACCES || err == EPERM || err == ENOENT || err == EROFS || err == ENOTDIR;
}

static void sanitize_session_id(const char *session_id, char *out, size_t out_size) {
    size_t i;
    size_t pos = 0;

    for (i = 0; session_id[i] != '\0' && pos + 1 < out_size; i++) {
        unsigned char ch = (unsigned char)session_id[i];
        if (isalnum(ch) || ch == '.' || ch == '_' || ch == '-') {
            out[pos++] = (char)ch;
        } else {
            out[pos++] = '_';
        }
    }
    out[pos] = '\0';
}

static int write_file(const char *path, const char *value) {
    int fd = open(path, O_WRONLY | O_CLOEXEC);
    size_t len;
    ssize_t written;

    if (fd < 0) {
        return errno;
    }

    len = strlen(value);
    written = write(fd, value, len);
    if (written < 0 || (size_t)written != len) {
        int err = written < 0 ? errno : EIO;
        close(fd);
        return err;
    }
    if (close(fd) != 0) {
        return errno;
    }
    return 0;
}

static int join_path(char *out, size_t out_size, const char *dir, const char *name) {
    size_t dir_len = strlen(dir);
    size_t name_len = strlen(name);

    if (dir_len + 1 + name_len + 1 > out_size) {
        return ENAMETOOLONG;
    }
    memcpy(out, dir, dir_len);
    out[dir_len] = '/';
    memcpy(out + dir_len + 1, name, name_len);
    out[dir_len + 1 + name_len] = '\0';
    return 0;
}

static int write_u64_limit(const char *dir, const char *name, uint64_t value) {
    char path[PATH_MAX];
    char content[64];
    int err;

    if (value == 0) {
        return 0;
    }

    err = join_path(path, sizeof(path), dir, name);
    if (err != 0) {
        return err;
    }
    snprintf(content, sizeof(content), "%llu\n", (unsigned long long)value);
    return write_file(path, content);
}

static int write_cpu_limit(const char *dir, uint64_t cpu_quota_pct) {
    char path[PATH_MAX];
    char content[64];
    unsigned long long quota_us;
    int err;

    if (cpu_quota_pct == 0) {
        return 0;
    }

    quota_us = (unsigned long long)cpu_quota_pct * 10000ULL;
    err = join_path(path, sizeof(path), dir, "cpu.max");
    if (err != 0) {
        return err;
    }
    snprintf(content, sizeof(content), "%llu 100000\n", quota_us);
    return write_file(path, content);
}
#endif

int cgroup_attach_if_available(const struct sandbox_args *args) {
#ifdef __linux__
    char sanitized[160];
    char cgroup_dir[PATH_MAX];
    char path[PATH_MAX];
    char pid_buf[64];
    int fd;
    int err;

    fd = open(CGROUP_ROOT "/cgroup.procs", O_WRONLY | O_CLOEXEC);
    if (fd < 0) {
        if (is_skip_errno(errno)) {
            return 0;
        }
        return errno;
    }
    close(fd);

    err = write_file(CGROUP_ROOT "/cgroup.subtree_control", "+memory +pids +cpu\n");
    if (err != 0 && err != EBUSY) {
        if (is_skip_errno(err)) {
            return 0;
        }
        return err;
    }

    sanitize_session_id(args->session_id, sanitized, sizeof(sanitized));
    if (sanitized[0] == '\0') {
        return EINVAL;
    }

    snprintf(cgroup_dir, sizeof(cgroup_dir), CGROUP_ROOT "/ws-%s", sanitized);
    if (mkdir(cgroup_dir, 0755) != 0 && errno != EEXIST) {
        if (is_skip_errno(errno)) {
            return 0;
        }
        return errno;
    }

    err = write_u64_limit(cgroup_dir, "memory.max", args->memory_max);
    if (err != 0) {
        return err;
    }
    err = write_u64_limit(cgroup_dir, "pids.max", args->pids_max);
    if (err != 0) {
        return err;
    }
    err = write_cpu_limit(cgroup_dir, args->cpu_quota_pct);
    if (err != 0) {
        return err;
    }

    err = join_path(path, sizeof(path), cgroup_dir, "cgroup.procs");
    if (err != 0) {
        return err;
    }
    snprintf(pid_buf, sizeof(pid_buf), "%ld\n", (long)getpid());
    err = write_file(path, pid_buf);
    if (err != 0) {
        return err;
    }
#else
    (void)args;
    wrapper_warning("cgroup", ENOSYS, "cgroup v2 is only available on Linux, skipping");
#endif
    return 0;
}
