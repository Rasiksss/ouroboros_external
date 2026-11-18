#include "env.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

extern char **environ;

static void clear_environment(void) {
    size_t count = 0;
    size_t i;
    char **keys;

    if (environ == NULL) {
        return;
    }
    while (environ[count] != NULL) {
        count++;
    }

    keys = calloc(count, sizeof(char *));
    if (keys == NULL) {
        return;
    }

    for (i = 0; i < count; i++) {
        char *equals = strchr(environ[i], '=');
        size_t key_len;
        if (equals == NULL) {
            continue;
        }
        key_len = (size_t)(equals - environ[i]);
        keys[i] = malloc(key_len + 1);
        if (keys[i] == NULL) {
            continue;
        }
        memcpy(keys[i], environ[i], key_len);
        keys[i][key_len] = '\0';
    }

    for (i = 0; i < count; i++) {
        if (keys[i] != NULL) {
            unsetenv(keys[i]);
            free(keys[i]);
        }
    }
    free(keys);
}

int load_env_file(const char *path) {
    FILE *file;
    char *line = NULL;
    size_t linecap = 0;
    ssize_t linelen;

    clear_environment();

    if (path == NULL || *path == '\0') {
        return 0;
    }

    file = fopen(path, "r");
    if (file == NULL) {
        return errno;
    }

    while ((linelen = getline(&line, &linecap, file)) >= 0) {
        char *equals;

        while (linelen > 0 && (line[linelen - 1] == '\n' || line[linelen - 1] == '\r')) {
            line[--linelen] = '\0';
        }
        if (linelen == 0) {
            continue;
        }

        equals = strchr(line, '=');
        if (equals == NULL || equals == line) {
            free(line);
            fclose(file);
            return EINVAL;
        }
        *equals = '\0';
        if (setenv(line, equals + 1, 1) != 0) {
            int err = errno;
            free(line);
            fclose(file);
            return err;
        }
    }

    free(line);
    if (ferror(file)) {
        int err = errno;
        fclose(file);
        return err;
    }
    fclose(file);
    return 0;
}
