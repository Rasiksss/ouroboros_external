#ifndef WS_SANDBOX_ARGS_H
#define WS_SANDBOX_ARGS_H

#include <stdint.h>
#include <stdio.h>

struct sandbox_args {
    int show_help;
    const char *session_id;
    const char *session_dir;
    const char *bin_dir;
    const char *cwd;
    uint64_t memory_max;
    uint64_t pids_max;
    uint64_t cpu_quota_pct;
    uint64_t rlimit_as;
    uint64_t rlimit_fsize;
    uint64_t rlimit_cpu;
    uint64_t rlimit_nofile;
    uint64_t rlimit_nproc;
    uint64_t rlimit_stack;
    uint64_t rlimit_core;
    uint64_t rlimit_memlock;
    const char *env_file;
    const char *exec_allowlist;
    int network_enabled;
    char **command_argv;
    int command_argc;
};

int parse_args(int argc, char **argv, struct sandbox_args *out, const char **error);
void print_usage(FILE *stream);

#endif
