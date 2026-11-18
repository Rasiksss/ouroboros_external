#include "args.h"
#include "cgroup.h"
#include "env.h"
#include "landlock.h"
#include "process.h"
#include "rlimits.h"
#include "seccomp_filter.h"
#include "util.h"

#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <sysexits.h>
#include <unistd.h>

extern char **environ;

static void print_failure_context(const struct sandbox_args *args, const char *command_path) {
    fprintf(stderr, "WRAPPER: context pid=%ld uid=%ld euid=%ld session_id=", (long)getpid(), (long)getuid(), (long)geteuid());
    wrapper_print_quoted(stderr, args->session_id);
    fputs(" session_dir=", stderr);
    wrapper_print_quoted(stderr, args->session_dir);
    fputs(" cwd=", stderr);
    wrapper_print_quoted(stderr, args->cwd);
    fputs(" command=", stderr);
    wrapper_print_quoted(stderr, args->command_argv != NULL && args->command_argc > 0 ? args->command_argv[0] : NULL);
    fputs(" resolved_command=", stderr);
    wrapper_print_quoted(stderr, command_path);
    fprintf(stderr, " command_argc=%d\n", args->command_argc);
}

static int fail_step(const struct sandbox_args *args, const char *step, int errnum, const char *message, const char *command_path) {
    wrapper_error(step, errnum, message);
    print_failure_context(args, command_path);
    return EX_OSERR;
}

static int chdir_session_cwd(const struct sandbox_args *args) {
    const char *cwd = args->cwd;

    if (chdir(args->session_dir) != 0) {
        return errno;
    }
    if (cwd == NULL || strcmp(cwd, "/") == 0 || *cwd == '\0') {
        return 0;
    }
    while (*cwd == '/') {
        cwd++;
    }
    if (*cwd == '\0') {
        return 0;
    }
    if (chdir(cwd) != 0) {
        return errno;
    }
    return 0;
}

static int resolve_exec_path(const struct sandbox_args *args, char *out, size_t out_size) {
    const char *command = args->command_argv[0];
    size_t prefix_len;
    size_t command_len;

    if (command[0] != '/') {
        return 0;
    }

    /* /bin/X resolves under the shared, read-only toolchain so sessions
     * cannot execute a binary they themselves may have written into
     * session_dir. Other absolute paths still resolve under the
     * per-session rootfs. */
    if (strncmp(command, "/bin/", 5) == 0) {
        const char *basename = command + 5;
        size_t base_len = strlen(basename);
        prefix_len = strlen(args->bin_dir);
        if (prefix_len + 1 + base_len + 1 > out_size) {
            return ENAMETOOLONG;
        }
        memcpy(out, args->bin_dir, prefix_len);
        out[prefix_len] = '/';
        memcpy(out + prefix_len + 1, basename, base_len + 1);
        return 0;
    }

    prefix_len = strlen(args->session_dir);
    command_len = strlen(command);
    if (prefix_len + command_len + 1 > out_size) {
        return ENAMETOOLONG;
    }
    memcpy(out, args->session_dir, prefix_len);
    memcpy(out + prefix_len, command, command_len + 1);
    return 0;
}

int main(int argc, char **argv) {
    struct sandbox_args args;
    const char *error = NULL;
    char exec_path[PATH_MAX];

    if (parse_args(argc, argv, &args, &error) != 0) {
        if (error != NULL) {
            fprintf(stderr, "error: %s\n", error);
        }
        print_usage(stderr);
        return EX_USAGE;
    }

    if (args.show_help) {
        print_usage(stdout);
        return 0;
    }

    int err;
    const char *command_path = args.command_argv[0];

    err = resolve_exec_path(&args, exec_path, sizeof(exec_path));
    if (err != 0) {
        return fail_step(&args, "execve", err, "failed to resolve command path", command_path);
    }
    if (args.command_argv[0][0] == '/') {
        command_path = exec_path;
    }

    err = enter_process_group();
    if (err != 0) {
        return fail_step(&args, "setpgid", err, "failed to enter process group", command_path);
    }

    err = cgroup_attach_if_available(&args);
    if (err != 0) {
        return fail_step(&args, "cgroup", err, "failed to attach cgroup", command_path);
    }

    err = apply_rlimits(&args);
    if (err != 0) {
        return fail_step(&args, "rlimits", err, "failed to apply resource limits", command_path);
    }

    err = close_inherited_fds();
    if (err != 0) {
        return fail_step(&args, "close_range", err, "failed to close inherited file descriptors", command_path);
    }

    err = apply_no_new_privs();
    if (err != 0) {
        return fail_step(&args, "no_new_privs", err, "failed to apply PR_SET_NO_NEW_PRIVS", command_path);
    }

    err = landlock_apply(&args);
    if (err != 0) {
        return fail_step(&args, "landlock", err, "failed to apply Landlock ruleset", command_path);
    }

    err = apply_parent_death_signal();
    if (err != 0) {
        return fail_step(&args, "pdeathsig", err, "failed to apply PR_SET_PDEATHSIG", command_path);
    }

    err = apply_dumpable_zero();
    if (err != 0) {
        return fail_step(&args, "dumpable", err, "failed to apply PR_SET_DUMPABLE", command_path);
    }

    err = seccomp_install(args.network_enabled);
    if (err != 0) {
        return fail_step(&args, "seccomp", err, "failed to install seccomp filter", command_path);
    }

    err = load_env_file(args.env_file);
    if (err != 0) {
        return fail_step(&args, "env", err, "failed to load env file", command_path);
    }

    err = chdir_session_cwd(&args);
    if (err != 0) {
        return fail_step(&args, "chdir", err, "failed to enter session cwd", command_path);
    }

    execve(command_path, args.command_argv, environ);
    return fail_step(&args, "execve", errno, "failed to execute command", command_path);
}
