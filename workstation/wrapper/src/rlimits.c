#include "rlimits.h"

#include <errno.h>
#include <stdint.h>
#include <sys/resource.h>

static int set_limit_if_nonzero(int resource, uint64_t value) {
    struct rlimit limit;

    if (value == 0) {
        return 0;
    }

    limit.rlim_cur = (rlim_t)value;
    limit.rlim_max = (rlim_t)value;
    return setrlimit(resource, &limit);
}

static int set_limit_always(int resource, uint64_t value) {
    struct rlimit limit;

    limit.rlim_cur = (rlim_t)value;
    limit.rlim_max = (rlim_t)value;
    return setrlimit(resource, &limit);
}

int apply_rlimits(const struct sandbox_args *args) {
    if (set_limit_if_nonzero(RLIMIT_AS, args->rlimit_as) != 0) {
        return errno;
    }
    if (set_limit_if_nonzero(RLIMIT_FSIZE, args->rlimit_fsize) != 0) {
        return errno;
    }
    if (set_limit_if_nonzero(RLIMIT_CPU, args->rlimit_cpu) != 0) {
        return errno;
    }
    if (set_limit_if_nonzero(RLIMIT_NOFILE, args->rlimit_nofile) != 0) {
        return errno;
    }
#ifdef RLIMIT_NPROC
    if (set_limit_if_nonzero(RLIMIT_NPROC, args->rlimit_nproc) != 0) {
        return errno;
    }
#else
    (void)args->rlimit_nproc;
#endif
    if (set_limit_if_nonzero(RLIMIT_STACK, args->rlimit_stack) != 0) {
        return errno;
    }
    if (set_limit_always(RLIMIT_CORE, args->rlimit_core) != 0) {
        return errno;
    }
#ifdef RLIMIT_MEMLOCK
    if (set_limit_always(RLIMIT_MEMLOCK, args->rlimit_memlock) != 0) {
        return errno;
    }
#else
    (void)set_limit_always;
#endif
    return 0;
}
