#ifndef WS_SANDBOX_UTIL_H
#define WS_SANDBOX_UTIL_H

#include <stdio.h>

void wrapper_error(const char *step, int errnum, const char *message);
void wrapper_warning(const char *step, int errnum, const char *message);
void wrapper_print_quoted(FILE *stream, const char *value);

#endif
