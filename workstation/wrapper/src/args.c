#include "args.h"

#include <errno.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>

static int parse_u64(const char *value, uint64_t *out) {
    char *end = NULL;
    unsigned long long parsed;

    if (value == NULL || *value == '\0' || value[0] == '-') {
        return -1;
    }

    errno = 0;
    parsed = strtoull(value, &end, 10);
    if (errno != 0 || end == value || *end != '\0') {
        return -1;
    }

    *out = (uint64_t)parsed;
    return 0;
}

static int require_value(int index, int argc, const char **error) {
    if (index + 1 >= argc) {
        *error = "missing value for option";
        return -1;
    }
    return 0;
}

static int missing_required(const struct sandbox_args *args, const char **error) {
    if (args->session_id == NULL) {
        *error = "missing --session-id";
        return -1;
    }
    if (args->session_dir == NULL) {
        *error = "missing --session-dir";
        return -1;
    }
    if (args->bin_dir == NULL) {
        *error = "missing --bin-dir";
        return -1;
    }
    if (args->cwd == NULL) {
        *error = "missing --cwd";
        return -1;
    }
    if (args->env_file == NULL) {
        *error = "missing --env-file";
        return -1;
    }
    if (args->exec_allowlist == NULL) {
        *error = "missing --exec-allowlist";
        return -1;
    }
    if (args->command_argc == 0 || args->command_argv == NULL) {
        *error = "missing command after --";
        return -1;
    }
    return 0;
}

int parse_args(int argc, char **argv, struct sandbox_args *out, const char **error) {
    int i;

    memset(out, 0, sizeof(*out));
    *error = NULL;

    if (argc == 2 && strcmp(argv[1], "--help") == 0) {
        out->show_help = 1;
        return 0;
    }

    for (i = 1; i < argc; i++) {
        const char *flag = argv[i];
        const char *value;

        if (strcmp(flag, "--") == 0) {
            out->command_argv = &argv[i + 1];
            out->command_argc = argc - i - 1;
            break;
        }

        if (require_value(i, argc, error) != 0) {
            return -1;
        }
        value = argv[++i];

        if (strcmp(flag, "--session-id") == 0) {
            out->session_id = value;
        } else if (strcmp(flag, "--session-dir") == 0) {
            out->session_dir = value;
        } else if (strcmp(flag, "--bin-dir") == 0) {
            out->bin_dir = value;
        } else if (strcmp(flag, "--cwd") == 0) {
            out->cwd = value;
        } else if (strcmp(flag, "--memory-max") == 0) {
            if (parse_u64(value, &out->memory_max) != 0) {
                *error = "invalid --memory-max";
                return -1;
            }
        } else if (strcmp(flag, "--pids-max") == 0) {
            if (parse_u64(value, &out->pids_max) != 0) {
                *error = "invalid --pids-max";
                return -1;
            }
        } else if (strcmp(flag, "--cpu-quota-pct") == 0) {
            if (parse_u64(value, &out->cpu_quota_pct) != 0) {
                *error = "invalid --cpu-quota-pct";
                return -1;
            }
        } else if (strcmp(flag, "--rlimit-as") == 0) {
            if (parse_u64(value, &out->rlimit_as) != 0) {
                *error = "invalid --rlimit-as";
                return -1;
            }
        } else if (strcmp(flag, "--rlimit-fsize") == 0) {
            if (parse_u64(value, &out->rlimit_fsize) != 0) {
                *error = "invalid --rlimit-fsize";
                return -1;
            }
        } else if (strcmp(flag, "--rlimit-cpu") == 0) {
            if (parse_u64(value, &out->rlimit_cpu) != 0) {
                *error = "invalid --rlimit-cpu";
                return -1;
            }
        } else if (strcmp(flag, "--rlimit-nofile") == 0) {
            if (parse_u64(value, &out->rlimit_nofile) != 0) {
                *error = "invalid --rlimit-nofile";
                return -1;
            }
        } else if (strcmp(flag, "--rlimit-nproc") == 0) {
            if (parse_u64(value, &out->rlimit_nproc) != 0) {
                *error = "invalid --rlimit-nproc";
                return -1;
            }
        } else if (strcmp(flag, "--rlimit-stack") == 0) {
            if (parse_u64(value, &out->rlimit_stack) != 0) {
                *error = "invalid --rlimit-stack";
                return -1;
            }
        } else if (strcmp(flag, "--rlimit-core") == 0) {
            if (parse_u64(value, &out->rlimit_core) != 0) {
                *error = "invalid --rlimit-core";
                return -1;
            }
        } else if (strcmp(flag, "--rlimit-memlock") == 0) {
            if (parse_u64(value, &out->rlimit_memlock) != 0) {
                *error = "invalid --rlimit-memlock";
                return -1;
            }
        } else if (strcmp(flag, "--env-file") == 0) {
            out->env_file = value;
        } else if (strcmp(flag, "--exec-allowlist") == 0) {
            out->exec_allowlist = value;
        } else if (strcmp(flag, "--network-enabled") == 0) {
            uint64_t parsed;
            if (parse_u64(value, &parsed) != 0 || parsed > 1) {
                *error = "invalid --network-enabled (expected 0 or 1)";
                return -1;
            }
            out->network_enabled = (int)parsed;
        } else {
            *error = "unknown option";
            return -1;
        }
    }

    return missing_required(out, error);
}

void print_usage(FILE *stream) {
    fputs(
        "usage: ws-sandbox --session-id ID --session-dir PATH --bin-dir PATH "
        "--cwd PATH --memory-max BYTES --pids-max N --cpu-quota-pct N "
        "--rlimit-as BYTES --rlimit-fsize BYTES --rlimit-cpu SEC "
        "--rlimit-nofile N --rlimit-nproc N --rlimit-stack BYTES --rlimit-core BYTES "
        "--rlimit-memlock BYTES --env-file PATH --exec-allowlist PATH "
        "--network-enabled 0|1 -- COMMAND [ARG...]\n",
        stream);
}
