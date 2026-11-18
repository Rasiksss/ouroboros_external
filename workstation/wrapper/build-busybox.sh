#!/usr/bin/env bash
# Reproducible build of the vendored busybox binary used by the sandbox rootfs.
#
# Output: vendor/bin/busybox (static x86-64, musl-linked).
#
# Requires docker on the host. Run from the repo root:
#   ./wrapper/build-busybox.sh
set -euo pipefail

BUSYBOX_VERSION="${BUSYBOX_VERSION:-1.36.1}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRAGMENT="${REPO_ROOT}/wrapper/busybox.config"
OUTPUT="${REPO_ROOT}/vendor/bin/busybox"
BUILD_CTX="$(mktemp -d)"
trap 'rm -rf "${BUILD_CTX}"' EXIT

cp "${FRAGMENT}" "${BUILD_CTX}/fragment.cfg"

cat > "${BUILD_CTX}/Dockerfile" <<EOF
FROM alpine:3.20
RUN apk add --no-cache build-base linux-headers wget bash bzip2 perl musl-dev
WORKDIR /work
RUN wget -q https://busybox.net/downloads/busybox-${BUSYBOX_VERSION}.tar.bz2 \\
 && tar xjf busybox-${BUSYBOX_VERSION}.tar.bz2
WORKDIR /work/busybox-${BUSYBOX_VERSION}
COPY fragment.cfg /tmp/fragment.cfg
# allnoconfig seeds .config with NONE picks for the sh/bash choice groups.
# Disable them so the fragment's SH_IS_ASH / BASH_IS_ASH win during oldconfig.
RUN make allnoconfig >/dev/null \\
 && sed -i 's/^CONFIG_SH_IS_NONE=y/# CONFIG_SH_IS_NONE is not set/; s/^CONFIG_BASH_IS_NONE=y/# CONFIG_BASH_IS_NONE is not set/' .config \\
 && bash -c ' \\
      while IFS= read -r line; do \\
        [[ "\$line" =~ ^CONFIG_ ]] || continue; \\
        key="\${line%%=*}"; \\
        sed -i "/^\${key}=/d; /^# \${key} is not set/d" .config; \\
        echo "\$line" >> .config; \\
      done < /tmp/fragment.cfg' \\
 && yes "" | make oldconfig >/dev/null 2>&1
RUN make -j\$(nproc) busybox
EOF

IMG="ws-busybox-build:${BUSYBOX_VERSION}"
docker build -f "${BUILD_CTX}/Dockerfile" -t "${IMG}" "${BUILD_CTX}"

cid=$(docker create "${IMG}")
trap 'docker rm "${cid}" >/dev/null 2>&1 || true; rm -rf "${BUILD_CTX}"' EXIT
docker cp "${cid}:/work/busybox-${BUSYBOX_VERSION}/busybox" "${OUTPUT}"
chmod 0755 "${OUTPUT}"

echo "--- built ---"
file "${OUTPUT}"
ls -la "${OUTPUT}"
echo "--- applets ---"
"${OUTPUT}" --list | sort
