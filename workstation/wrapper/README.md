# ws-sandbox

`ws-sandbox` is the local runtime wrapper for `RUNTIME_MODE=local`.

Current implementation status: Linux security implementation in progress. The
binary accepts the planned CLI, returns parser errors with exit code `64`,
applies process group, best-effort cgroup v2 limits, rlimits, close-fd,
`NO_NEW_PRIVS`, Landlock, Linux `PDEATHSIG`/`DUMPABLE`, seccomp, env-file, cwd,
and `execve` steps. On non-Linux developer machines, Linux-only mechanisms emit
warnings and are skipped.

`NO_NEW_PRIVS` intentionally runs before Landlock. See
`../LANDLOCK_NO_NEW_PRIVS.md` for the reason and the approved order decision.

## Build

```bash
make
```

The binary is written to `build/ws-sandbox`.

On Linux, the Makefile links with `-static` and `-lseccomp`. On non-Linux
developer machines it builds a local test binary with the platform C compiler
and skips Linux-only mechanisms at runtime.

Current Debian/ARM64 Docker evidence: the binary is static, `ldd` reports
`not a dynamic executable`, and the stripped musl build is `198320` bytes,
below the `300 KB` target.

## Clean

```bash
make clean
```

## CLI

```bash
build/ws-sandbox \
  --session-id session-1 \
  --session-dir /tmp/workstation/sessions/session-1 \
  --bin-dir /opt/workstation/bin \
  --cwd /workspace \
  --memory-max 536870912 \
  --pids-max 64 \
  --cpu-quota-pct 50 \
  --rlimit-as 1073741824 \
  --rlimit-fsize 104857600 \
  --rlimit-cpu 60 \
  --rlimit-nofile 256 \
  --rlimit-nproc 1024 \
  --rlimit-stack 0 \
  --rlimit-core 0 \
  --rlimit-memlock 0 \
  --env-file /tmp/workstation/sessions/session-1/.sandbox/exec/run-1/env \
  --exec-allowlist /tmp/workstation/sessions/session-1/.sandbox/exec/run-1/exec.allow \
  --network-enabled 0 \
  -- \
  /bin/sh -lc 'pwd'
```
