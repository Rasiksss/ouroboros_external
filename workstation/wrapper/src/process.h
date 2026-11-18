#ifndef WS_SANDBOX_PROCESS_H
#define WS_SANDBOX_PROCESS_H

int enter_process_group(void);
int close_inherited_fds(void);
int apply_no_new_privs(void);
int apply_parent_death_signal(void);
int apply_dumpable_zero(void);

#endif
