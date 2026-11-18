#include "util.h"

#include <stdio.h>
#include <string.h>

void wrapper_print_quoted(FILE *stream, const char *value) {
    const unsigned char *cursor;

    if (value == NULL) {
        fputs("null", stream);
        return;
    }

    fputc('"', stream);
    for (cursor = (const unsigned char *)value; *cursor != '\0'; cursor++) {
        switch (*cursor) {
        case '\\':
            fputs("\\\\", stream);
            break;
        case '"':
            fputs("\\\"", stream);
            break;
        case '\n':
            fputs("\\n", stream);
            break;
        case '\r':
            fputs("\\r", stream);
            break;
        case '\t':
            fputs("\\t", stream);
            break;
        default:
            if (*cursor < 0x20 || *cursor == 0x7f) {
                fprintf(stream, "\\x%02x", *cursor);
            } else {
                fputc(*cursor, stream);
            }
            break;
        }
    }
    fputc('"', stream);
}

void wrapper_error(const char *step, int errnum, const char *message) {
    fprintf(stderr, "WRAPPER: error step=%s errno=%d errno_text=", step, errnum);
    wrapper_print_quoted(stderr, strerror(errnum));
    fputs(" msg=", stderr);
    wrapper_print_quoted(stderr, message);
    fputc('\n', stderr);
}

void wrapper_warning(const char *step, int errnum, const char *message) {
    fprintf(stderr, "WRAPPER: warning step=%s errno=%d errno_text=", step, errnum);
    wrapper_print_quoted(stderr, strerror(errnum));
    fputs(" msg=", stderr);
    wrapper_print_quoted(stderr, message);
    fputc('\n', stderr);
}
