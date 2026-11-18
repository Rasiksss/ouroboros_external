#include "process.h"

#include "util.h"

#include <errno.h>
#include <signal.h>
#include <sys/resource.h>
#include <unistd.h>

#ifdef __linux__
#include <sys/prctl.h>
#include <sys/syscall.h>
#endif

int enter_process_group(void) {
    if (setsid() != 0 && errno != EPERM) {
        return errno;
    }
    if (setpgid(0, 0) != 0 && errno != EPERM) {
        return errno;
    }
    return 0;
}

int close_inherited_fds(void) {
#ifdef __linux__
#ifdef SYS_close_range
    if (syscall(SYS_close_range, 3U, ~0U, 0U) == 0) {
        return 0;
    }
    if (errno != ENOSYS) {
        return errno;
    }
#endif
#endif

    struct rlimit limit;
    rlim_t max_fd;
    rlim_t fd;

    if (getrlimit(RLIMIT_NOFILE, &limit) != 0) {
        return errno;
    }
    max_fd = limit.rlim_cur;
    if (max_fd == RLIM_INFINITY || max_fd > 1048576) {
        max_fd = 1048576;
    }
    for (fd = 3; fd < max_fd; fd++) {
        close((int)fd);
    }
    return 0;
}

int apply_no_new_privs(void) {
#ifdef __linux__
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
        return errno;
    }
#else
    wrapper_warning("no_new_privs", ENOSYS, "PR_SET_NO_NEW_PRIVS is only available on Linux, skipping");
#endif
    return 0;
}

int apply_parent_death_signal(void) {
#ifdef __linux__
    if (prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0) != 0) {
        return errno;
    }
#else
    wrapper_warning("pdeathsig", ENOSYS, "PR_SET_PDEATHSIG is only available on Linux, skipping");
#endif
    return 0;
}

int apply_dumpable_zero(void) {
#ifdef __linux__
    if (prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0) {
        return errno;
    }
#else
    wrapper_warning("dumpable", ENOSYS, "PR_SET_DUMPABLE is only available on Linux, skipping");
#endif
    return 0;
}
