#define _GNU_SOURCE
#include <sys/prctl.h>
#include <unistd.h>
#include <string.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <dlfcn.h>
#include <link.h>
#include <stdlib.h>

#ifndef UPDATE_ASYNC_START
#define UPDATE_ASYNC_START 0x2878f0UL
#endif
#ifndef UPDATE_ASYNC_END
#define UPDATE_ASYNC_END   0x28aaf3UL
#endif
#ifndef UPDATE_SYNC_START
#define UPDATE_SYNC_START  0x28ab00UL
#endif
#ifndef UPDATE_SYNC_END
#define UPDATE_SYNC_END    0x28dfc3UL
#endif

typedef int (*get_proc_name_fn)(void *cursor, char *buf, size_t len, uintptr_t *offp);
typedef int (*get_reg_fn)(void *cursor, int regnum, uintptr_t *valp);

/* First 16 bytes of each function in SteamVR 2.18.4 (build 1792185270).
 * Used to verify that the hardcoded offsets match the running vrcompositor.bin. */
static const unsigned char UPDATE_ASYNC_PROLOGUE[16] = {
    0x55, 0x48, 0x89, 0xe5, 0x41, 0x57, 0x41, 0x56,
    0x41, 0x55, 0x41, 0x54, 0x53, 0x48, 0x89, 0xfb };
static const unsigned char UPDATE_SYNC_PROLOGUE[16] = {
    0x55, 0x48, 0x89, 0xe5, 0x41, 0x57, 0x49, 0x89,
    0xff, 0x41, 0x56, 0x41, 0x55, 0x41, 0x54, 0x4c };

static uintptr_t main_base = 0;
static int hook_enabled = 0;

static void *unw_handle = NULL;
static get_proc_name_fn real_get_proc_name = NULL;
static get_reg_fn real_get_reg = NULL;

static void ensure_unw_symbols(void) {
    if (!unw_handle) {
        unw_handle = dlopen("libunwind.so.8", RTLD_LAZY | RTLD_NOLOAD);
        if (!unw_handle) {
            unw_handle = dlopen("libunwind.so.8", RTLD_LAZY);
        }
    }
    if (unw_handle) {
        if (!real_get_proc_name) {
            real_get_proc_name = (get_proc_name_fn)dlsym(unw_handle, "_ULx86_64_get_proc_name");
        }
        if (!real_get_reg) {
            real_get_reg = (get_reg_fn)dlsym(unw_handle, "_ULx86_64_get_reg");
        }
    }
}

static int range_in_exec_segment(struct dl_phdr_info *info, uintptr_t off, size_t len) {
    for (int i = 0; i < info->dlpi_phnum; i++) {
        const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
        if (ph->p_type == PT_LOAD && (ph->p_flags & PF_X) &&
            off >= ph->p_vaddr && off + len <= ph->p_vaddr + ph->p_filesz) {
            return 1;
        }
    }
    return 0;
}

static int find_main_base(struct dl_phdr_info *info, size_t size, void *data) {
    (void)size;
    (void)data;
    /* The first entry with an empty name is the main executable.
     * Also verify that both addresses lie inside an executable PT_LOAD segment,
     * so we never read unmapped memory on other SteamVR builds. */
    if (info->dlpi_name == NULL || info->dlpi_name[0] == '\0') {
        main_base = (uintptr_t)info->dlpi_addr;
        if (range_in_exec_segment(info, UPDATE_ASYNC_START, 16) &&
            range_in_exec_segment(info, UPDATE_SYNC_START, 16) &&
            memcmp((const void *)(main_base + UPDATE_ASYNC_START), UPDATE_ASYNC_PROLOGUE, 16) == 0 &&
            memcmp((const void *)(main_base + UPDATE_SYNC_START), UPDATE_SYNC_PROLOGUE, 16) == 0) {
            hook_enabled = 1;
        }
        return 1;
    }
    return 0;
}

__attribute__((constructor))
static void fix_vrcompositor_comm(void) {
    char exe[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (n <= 0) {
        return;
    }
    exe[n] = '\0';
    const char *base = strrchr(exe, '/');
    base = base ? base + 1 : exe;
    if (strcmp(base, "vrcompositor.bin") != 0 && strcmp(base, "vrcompositor.real") != 0) {
        return;
    }

    prctl(PR_SET_NAME, "vrcompositor", 0, 0, 0);

    dl_iterate_phdr(find_main_base, NULL);

    if (getenv("ALVR_FIX_DEBUG")) {
        fprintf(stderr, "[libvrcompositor_comm] base=%p head-tracking hook %s\n",
                (void *)main_base,
                hook_enabled ? "ENABLED" : "DISABLED (unsupported SteamVR build)");
    }
}

/*
 * Restores render thread function names in stripped vrcompositor (SteamVR 2.18+):
 *   - _ZN13CRenderThread11UpdateAsyncEv (CRenderThread::UpdateAsync())
 *   - _ZN13CRenderThread6UpdateEv      (CRenderThread::Update())
 * so that libalvr_vulkan_layer.so can extract TrackedDevicePose_t from the stack.
 */
int _ULx86_64_get_proc_name(void *cursor, char *buf, size_t len, uintptr_t *offp) {
    ensure_unw_symbols();

    uintptr_t ip = 0;
    if (cursor) {
        if (real_get_reg) {
            real_get_reg(cursor, 16 /* UNW_X86_64_RIP */, &ip);
        }
        if (ip == 0) {
            ip = *(const uintptr_t *)((const char *)cursor + 0x18);
        }
    }

    if (hook_enabled && ip > main_base) {
        uintptr_t offset = ip - main_base;
        if (offset >= UPDATE_ASYNC_START && offset < UPDATE_ASYNC_END) {
            if (buf && len > 0) {
                strncpy(buf, "_ZN13CRenderThread11UpdateAsyncEv", len - 1);
                buf[len - 1] = '\0';
            }
            if (offp) {
                *offp = offset - UPDATE_ASYNC_START;
            }
            return 0;
        }
        if (offset >= UPDATE_SYNC_START && offset < UPDATE_SYNC_END) {
            if (buf && len > 0) {
                strncpy(buf, "_ZN13CRenderThread6UpdateEv", len - 1);
                buf[len - 1] = '\0';
            }
            if (offp) {
                *offp = offset - UPDATE_SYNC_START;
            }
            return 0;
        }
    }

    if (real_get_proc_name) {
        return real_get_proc_name(cursor, buf, len, offp);
    }
    return -1;
}

