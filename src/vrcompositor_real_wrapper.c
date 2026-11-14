#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <limits.h>

int main(int argc, char *argv[]) {
    char exe_path[PATH_MAX];
    ssize_t len = readlink("/proc/self/exe", exe_path, sizeof(exe_path) - 1);
    if (len < 0) {
        perror("readlink /proc/self/exe");
        return 1;
    }
    exe_path[len] = '\0';

    char *last_slash = strrchr(exe_path, '/');
    if (!last_slash) {
        return 1;
    }
    *last_slash = '\0';

    char so_path[PATH_MAX + 64];
    char bin_path[PATH_MAX + 64];
    char preload[PATH_MAX + 256];
    snprintf(so_path, sizeof(so_path), "%s/libvrcompositor_comm.so", exe_path);
    snprintf(bin_path, sizeof(bin_path), "%s/vrcompositor.bin", exe_path);
    snprintf(preload, sizeof(preload), "%s:/usr/libexec/alvr/alvr_drm_lease_shim.so", so_path);

    setenv("LD_PRELOAD", preload, 1);
    setenv("DRI_PRIME", "1", 1);
    setenv("VK_DRIVER_FILES", "/usr/share/vulkan/icd.d/radeon_icd.json", 1);
    setenv("DISABLE_LSFGVK", "1", 1);

    execv(bin_path, argv);
    perror("execv vrcompositor.bin");
    return 1;
}

