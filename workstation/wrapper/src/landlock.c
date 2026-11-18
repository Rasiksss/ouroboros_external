#include "landlock.h"

#include "landlock_uapi.h"
#include "util.h"

#include <errno.h>
#include <limits.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

#ifdef __linux__
#include <fcntl.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef SYS_landlock_create_ruleset
#ifdef __NR_landlock_create_ruleset
#define SYS_landlock_create_ruleset __NR_landlock_create_ruleset
#endif
#endif

#ifndef SYS_landlock_add_rule
#ifdef __NR_landlock_add_rule
#define SYS_landlock_add_rule __NR_landlock_add_rule
#endif
#endif

#ifndef SYS_landlock_restrict_self
#ifdef __NR_landlock_restrict_self
#define SYS_landlock_restrict_self __NR_landlock_restrict_self
#endif
#endif

static uint64_t supported_access_mask(int abi) {
    uint64_t mask =
        LANDLOCK_ACCESS_FS_EXECUTE |
        LANDLOCK_ACCESS_FS_WRITE_FILE |
        LANDLOCK_ACCESS_FS_READ_FILE |
        LANDLOCK_ACCESS_FS_READ_DIR |
        LANDLOCK_ACCESS_FS_REMOVE_DIR |
        LANDLOCK_ACCESS_FS_REMOVE_FILE |
        LANDLOCK_ACCESS_FS_MAKE_CHAR |
        LANDLOCK_ACCESS_FS_MAKE_DIR |
        LANDLOCK_ACCESS_FS_MAKE_REG |
        LANDLOCK_ACCESS_FS_MAKE_SOCK |
        LANDLOCK_ACCESS_FS_MAKE_FIFO |
        LANDLOCK_ACCESS_FS_MAKE_BLOCK |
        LANDLOCK_ACCESS_FS_MAKE_SYM;

    if (abi >= 2) {
        mask |= LANDLOCK_ACCESS_FS_REFER;
    }
    if (abi >= 3) {
        mask |= LANDLOCK_ACCESS_FS_TRUNCATE;
    }
    return mask;
}

static uint64_t session_access(uint64_t supported) {
    return supported & (
        LANDLOCK_ACCESS_FS_WRITE_FILE |
        LANDLOCK_ACCESS_FS_READ_FILE |
        LANDLOCK_ACCESS_FS_READ_DIR |
        LANDLOCK_ACCESS_FS_REMOVE_DIR |
        LANDLOCK_ACCESS_FS_REMOVE_FILE |
        LANDLOCK_ACCESS_FS_MAKE_CHAR |
        LANDLOCK_ACCESS_FS_MAKE_DIR |
        LANDLOCK_ACCESS_FS_MAKE_REG |
        LANDLOCK_ACCESS_FS_MAKE_SOCK |
        LANDLOCK_ACCESS_FS_MAKE_FIFO |
        LANDLOCK_ACCESS_FS_MAKE_BLOCK |
        LANDLOCK_ACCESS_FS_MAKE_SYM |
        LANDLOCK_ACCESS_FS_REFER |
        LANDLOCK_ACCESS_FS_TRUNCATE);
}

static uint64_t rx_access(uint64_t supported) {
    return supported & (
        LANDLOCK_ACCESS_FS_EXECUTE |
        LANDLOCK_ACCESS_FS_READ_FILE |
        LANDLOCK_ACCESS_FS_READ_DIR);
}

static uint64_t exec_file_access(uint64_t supported) {
    return supported & (
        LANDLOCK_ACCESS_FS_EXECUTE |
        LANDLOCK_ACCESS_FS_READ_FILE);
}

static uint64_t read_only_access(uint64_t supported) {
    return supported & (
        LANDLOCK_ACCESS_FS_READ_FILE |
        LANDLOCK_ACCESS_FS_READ_DIR);
}

static uint64_t dev_chardev_access(uint64_t supported) {
    return supported & (
        LANDLOCK_ACCESS_FS_READ_FILE |
        LANDLOCK_ACCESS_FS_WRITE_FILE);
}

static int add_path_rule(int ruleset_fd, const char *path, uint64_t access, int required) {
    struct landlock_path_beneath_attr attr;
    int parent_fd;

    parent_fd = open(path, O_PATH | O_CLOEXEC);
    if (parent_fd < 0) {
        if (!required && (errno == ENOENT || errno == ENOTDIR)) {
            return 0;
        }
        return errno;
    }

    attr.allowed_access = access;
    attr.parent_fd = parent_fd;
    if (syscall(SYS_landlock_add_rule, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, &attr, 0) != 0) {
        int err = errno;
        close(parent_fd);
        return err;
    }

    close(parent_fd);
    return 0;
}

/* Resolve an exec-allowlist entry to its on-disk path inside the shared bin
 * directory. We use only the basename of the entry — the EXEC_ALLOWLIST
 * format (e.g. "/bin/python3") is a virtual contract; the actual toolchain
 * lives in --bin-dir and is identical for every session. */
static int resolve_bin_path(
    const char *bin_dir,
    const char *line,
    size_t line_len,
    char *out,
    size_t out_size
) {
    const char *basename = line;
    size_t base_len;
    size_t bin_len;

    for (size_t i = 0; i < line_len; i++) {
        if (line[i] == '/') {
            basename = &line[i + 1];
        }
    }
    base_len = line_len - (size_t)(basename - line);
    if (base_len == 0) {
        return EINVAL;
    }

    bin_len = strlen(bin_dir);
    if (bin_len + 1 + base_len + 1 > out_size) {
        return ENAMETOOLONG;
    }
    memcpy(out, bin_dir, bin_len);
    out[bin_len] = '/';
    memcpy(out + bin_len + 1, basename, base_len);
    out[bin_len + 1 + base_len] = '\0';
    return 0;
}

static int add_session_exec_allowlist(
    int ruleset_fd,
    const char *bin_dir,
    const char *allowlist_path,
    uint64_t access
) {
    FILE *file;
    char line[4096];
    char resolved[PATH_MAX];

    file = fopen(allowlist_path, "r");
    if (file == NULL) {
        return errno;
    }

    while (fgets(line, sizeof(line), file) != NULL) {
        size_t len = 0;
        int err;

        while (line[len] != '\0') {
            len++;
        }
        while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r')) {
            line[--len] = '\0';
        }
        if (len == 0) {
            continue;
        }

        err = resolve_bin_path(bin_dir, line, len, resolved, sizeof(resolved));
        if (err == 0) {
            err = add_path_rule(ruleset_fd, resolved, access, 1);
        }
        if (err != 0) {
            fclose(file);
            return err;
        }
    }

    if (ferror(file)) {
        int err = errno;
        fclose(file);
        return err;
    }
    fclose(file);
    return 0;
}

static int add_dev_allowlist(int ruleset_fd, uint64_t access) {
    static const char *const paths[] = {
        "/dev/null",
        "/dev/zero",
        "/dev/full",
        "/dev/random",
        "/dev/urandom",
        "/dev/tty",
    };
    size_t i;

    for (i = 0; i < sizeof(paths) / sizeof(paths[0]); i++) {
        int err = add_path_rule(ruleset_fd, paths[i], access, 0);
        if (err == EINVAL || err == EOPNOTSUPP) {
            continue;
        }
        if (err != 0) {
            return err;
        }
    }
    return 0;
}

static int add_proc_allowlist(int ruleset_fd, uint64_t access) {
    static const char *const paths[] = {
        "/proc/self/status",
        "/proc/cpuinfo",
        "/proc/meminfo",
        "/proc/stat",
        "/proc/uptime",
        "/proc/loadavg",
        "/proc/sys/kernel/seccomp",
    };
    size_t i;

    for (i = 0; i < sizeof(paths) / sizeof(paths[0]); i++) {
        int err = add_path_rule(ruleset_fd, paths[i], access, 0);
        if (err == EINVAL || err == EOPNOTSUPP) {
            continue;
        }
        if (err != 0) {
            return err;
        }
    }
    return 0;
}

static int add_flexiblas_allowlist(int ruleset_fd, uint64_t access) {
    static const char *const paths[] = {
        "/etc/flexiblasrc",
        "/etc/flexiblasrc.d",
    };
    size_t i;

    for (i = 0; i < sizeof(paths) / sizeof(paths[0]); i++) {
        int err = add_path_rule(ruleset_fd, paths[i], access, 0);
        if (err == EINVAL || err == EOPNOTSUPP) {
            continue;
        }
        if (err != 0) {
            return err;
        }
    }
    return 0;
}
#endif

int landlock_apply(const struct sandbox_args *args) {
#ifdef __linux__
#if defined(SYS_landlock_create_ruleset) && defined(SYS_landlock_add_rule) && defined(SYS_landlock_restrict_self)
    int abi;
    int ruleset_fd;
    int err;
    uint64_t supported;
    struct landlock_ruleset_attr ruleset_attr;

    abi = (int)syscall(SYS_landlock_create_ruleset, NULL, 0, LANDLOCK_CREATE_RULESET_VERSION);
    if (abi < 0) {
        if (errno == ENOSYS) {
            wrapper_warning("landlock", errno, "Landlock syscalls are unavailable, skipping");
            return 0;
        }
        if (errno == EINVAL) {
            wrapper_warning("landlock", errno, "Landlock ABI version query failed, skipping");
            return 0;
        }
        return errno;
    }
    if (abi < 1) {
        wrapper_warning("landlock", ENOSYS, "Landlock ABI is too old, skipping");
        return 0;
    }

    supported = supported_access_mask(abi);
    memset(&ruleset_attr, 0, sizeof(ruleset_attr));
    ruleset_attr.handled_access_fs = supported;

    size_t attr_size = offsetof(struct landlock_ruleset_attr, handled_access_net);
    if (abi >= 4 && !args->network_enabled) {
        ruleset_attr.handled_access_net =
            LANDLOCK_ACCESS_NET_BIND_TCP | LANDLOCK_ACCESS_NET_CONNECT_TCP;
        attr_size = offsetof(struct landlock_ruleset_attr, scoped);
    }

    ruleset_fd = (int)syscall(SYS_landlock_create_ruleset, &ruleset_attr, attr_size, 0);
    if (ruleset_fd < 0) {
        if (errno == ENOSYS) {
            wrapper_warning("landlock", errno, "Landlock rulesets are unavailable, skipping");
            return 0;
        }
        return errno;
    }

    err = add_path_rule(ruleset_fd, args->session_dir, session_access(supported), 1);
    /* Shared sandbox toolchain. Read-only at the directory level — sessions
     * cannot write into bin_dir at all. EXECUTE is added per-file by the
     * exec.allow rules below, so only allowlisted binaries can run even
     * though every file in bin_dir is readable. */
    if (err == 0) {
        err = add_path_rule(ruleset_fd, args->bin_dir, read_only_access(supported), 1);
    }
    if (err == 0) {
        err = add_path_rule(ruleset_fd, "/lib", rx_access(supported), 0);
    }
    if (err == 0) {
        err = add_path_rule(ruleset_fd, "/lib64", rx_access(supported), 0);
    }
    if (err == 0) {
        err = add_path_rule(ruleset_fd, "/usr/lib", rx_access(supported), 0);
    }
    if (err == 0) {
        err = add_path_rule(ruleset_fd, "/usr/lib64", rx_access(supported), 0);
    }
    if (err == 0) {
        err = add_proc_allowlist(ruleset_fd, read_only_access(supported));
    }
    if (err == 0) {
        err = add_flexiblas_allowlist(ruleset_fd, read_only_access(supported));
    }
    if (err == 0) {
        err = add_dev_allowlist(ruleset_fd, dev_chardev_access(supported));
    }
    if (err == 0) {
        err = add_session_exec_allowlist(
            ruleset_fd,
            args->bin_dir,
            args->exec_allowlist,
            exec_file_access(supported));
    }
    if (err != 0) {
        close(ruleset_fd);
        return err;
    }

    if (syscall(SYS_landlock_restrict_self, ruleset_fd, 0) != 0) {
        err = errno;
        close(ruleset_fd);
        return err;
    }
    close(ruleset_fd);
#else
    (void)args;
    wrapper_warning("landlock", ENOSYS, "Landlock syscall numbers are unavailable, skipping");
#endif
#else
    (void)args;
    wrapper_warning("landlock", ENOSYS, "Landlock is only available on Linux, skipping");
#endif
    return 0;
}
