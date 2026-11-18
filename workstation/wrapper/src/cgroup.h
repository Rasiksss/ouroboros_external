#ifndef WS_SANDBOX_CGROUP_H
#define WS_SANDBOX_CGROUP_H

#include "args.h"

int cgroup_attach_if_available(const struct sandbox_args *args);

#endif
