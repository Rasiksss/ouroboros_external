BUILD_DIR := build
TARGET := $(BUILD_DIR)/ws-sandbox
SRC := $(wildcard src/*.c)
OBJ := $(patsubst src/%.c,$(BUILD_DIR)/%.o,$(SRC))
UNAME_S := $(shell uname -s)

CC ?= cc
CPPFLAGS ?=
CFLAGS ?= -D_GNU_SOURCE -std=c11 -Wall -Wextra -Werror -Os -ffunction-sections -fdata-sections -Iinclude -Isrc
LDFLAGS ?=
LDLIBS ?=
EXTRA_CFLAGS ?=
EXTRA_LDFLAGS ?=
EXTRA_LDLIBS ?=

ifeq ($(UNAME_S),Linux)
  LDFLAGS += -static
  LDFLAGS += -Wl,--gc-sections -s
  LDLIBS += -lseccomp
endif

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(OBJ)
	$(CC) $(CFLAGS) $(EXTRA_CFLAGS) $(OBJ) $(LDFLAGS) $(EXTRA_LDFLAGS) -o $@ $(LDLIBS) $(EXTRA_LDLIBS)

$(BUILD_DIR)/%.o: src/%.c | $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(EXTRA_CFLAGS) -c $< -o $@

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

clean:
	rm -rf $(BUILD_DIR)
