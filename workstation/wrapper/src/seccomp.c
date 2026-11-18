#include "seccomp_filter.h"

#include "util.h"

#include <errno.h>

#ifdef __linux__
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <seccomp.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <unistd.h>

static int ignore_unknown_syscall(int rc) {
    return rc == -EDOM || rc == -EOPNOTSUPP;
}

static int add_simple_rule(scmp_filter_ctx ctx, uint32_t action, int syscall_nr) {
    int rc = seccomp_rule_add(ctx, action, syscall_nr, 0);
    if (ignore_unknown_syscall(rc)) {
        return 0;
    }
    return rc;
}

static uint32_t seccomp_errno(int err) {
    return SECCOMP_RET_ERRNO | ((uint32_t)err & SECCOMP_RET_DATA);
}

static unsigned int seccomp_arg_offset(unsigned int arg) {
    return (unsigned int)offsetof(struct seccomp_data, args[arg]);
}

static int install_one_pid_filter(int syscall_nr, unsigned int arg, pid_t self_pid) {
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, (unsigned int)offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (uint32_t)syscall_nr, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, seccomp_arg_offset(arg)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, 0, 3, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (uint32_t)self_pid, 2, 0),
        BPF_JUMP(BPF_JMP | BPF_JGT | BPF_K, 0, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, seccomp_errno(EPERM)),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog prog = {
        .len = (unsigned short)(sizeof(filter) / sizeof(filter[0])),
        .filter = filter,
    };

    if (prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &prog) != 0) {
        return errno;
    }
    return 0;
}

static int install_two_pid_filter(int syscall_nr, unsigned int first_arg, unsigned int second_arg, pid_t self_pid) {
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, (unsigned int)offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (uint32_t)syscall_nr, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, seccomp_arg_offset(first_arg)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, 0, 3, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (uint32_t)self_pid, 2, 0),
        BPF_JUMP(BPF_JMP | BPF_JGT | BPF_K, 0, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, seccomp_errno(EPERM)),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, seccomp_arg_offset(second_arg)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, 0, 3, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (uint32_t)self_pid, 2, 0),
        BPF_JUMP(BPF_JMP | BPF_JGT | BPF_K, 0, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, seccomp_errno(EPERM)),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog prog = {
        .len = (unsigned short)(sizeof(filter) / sizeof(filter[0])),
        .filter = filter,
    };

    if (prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &prog) != 0) {
        return errno;
    }
    return 0;
}

static int install_signal_pid_filters(pid_t self_pid) {
    int err;

    err = install_one_pid_filter(SCMP_SYS(kill), 0, self_pid);
    if (err != 0) {
        return err;
    }
    err = install_one_pid_filter(SCMP_SYS(tkill), 0, self_pid);
    if (err != 0) {
        return err;
    }
    return install_two_pid_filter(SCMP_SYS(tgkill), 0, 1, self_pid);
}
#endif

int seccomp_install(int network_enabled) {
#ifdef __linux__
    scmp_filter_ctx ctx;
    int rc;
    pid_t self_pid;

    ctx = seccomp_init(SCMP_ACT_ALLOW);
    if (ctx == NULL) {
        return ENOMEM;
    }
    self_pid = getpid();

    rc = add_simple_rule(ctx, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(ptrace));
    if (rc == 0) {
        rc = add_simple_rule(ctx, SCMP_ACT_ERRNO(ENOSYS), SCMP_SYS(clone3));
    }
    if (rc == 0) {
        rc = add_simple_rule(ctx, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(process_vm_readv));
    }
    if (rc == 0) {
        rc = add_simple_rule(ctx, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(process_vm_writev));
    }
    if (rc == 0) {
        rc = add_simple_rule(ctx, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(pidfd_open));
    }
    if (rc == 0) {
        rc = add_simple_rule(ctx, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(pidfd_send_signal));
    }
    if (rc == 0) {
        rc = add_simple_rule(ctx, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(pidfd_getfd));
    }
    if (rc == 0 && !network_enabled) {
        /* Landlock gates only TCP; block UDP/datagram egress by rejecting any
           SOCK_DGRAM socket. Mask off SOCK_CLOEXEC|SOCK_NONBLOCK so the type
           match survives the flag bits callers OR into arg1. */
        rc = seccomp_rule_add(
            ctx, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(socket), 1,
            SCMP_A1(SCMP_CMP_MASKED_EQ,
                    (scmp_datum_t) ~(unsigned int)(SOCK_CLOEXEC | SOCK_NONBLOCK),
                    (scmp_datum_t)SOCK_DGRAM));
    }
    if (rc == 0) {
        rc = seccomp_load(ctx);
    }

    seccomp_release(ctx);
    if (rc != 0) {
        return -rc;
    }
    rc = install_signal_pid_filters(self_pid);
    if (rc != 0) {
        return rc;
    }
#else
    (void)network_enabled;
    wrapper_warning("seccomp", ENOSYS, "seccomp is only available on Linux, skipping");
#endif
    return 0;
}
