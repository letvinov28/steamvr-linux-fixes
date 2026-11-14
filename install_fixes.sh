#!/usr/bin/env bash
# ==============================================================================
# Build and apply Valve SteamVR 2.18.4 Linux component fixes:
#   1. steamvr.vrsettings (vkWaitForPresentKHR SIGSEGV workaround)
#   2. vrcompositor.real wrapper + libvrcompositor_comm.so (Error 303 / prctl)
#   3. Vulkan transfer ring buffer overflow patch (vulkanrenderer.cpp:5502)
#   4. CRenderThread::Update / UpdateAsync symbol restoration & libunwind hook
# ==============================================================================
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "[ERROR] Do not run this script as root. Run it as your regular user." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${SCRIPT_DIR}/src"

STEAM_ROOT="${HOME}/.local/share/Steam"
if [ ! -d "${STEAM_ROOT}" ] && [ -d "${HOME}/.steam/steam" ]; then
    STEAM_ROOT="$(realpath "${HOME}/.steam/steam")"
fi
STEAMVR_LINUX64="${STEAM_ROOT}/steamapps/common/SteamVR/bin/linux64"

if [ ! -d "${STEAMVR_LINUX64}" ]; then
    echo "[ERROR] SteamVR directory not found at: ${STEAMVR_LINUX64}" >&2
    echo "        Install SteamVR (Stable branch) in Steam first, then re-run." >&2
    exit 1
fi

echo "[INFO] Compiling libvrcompositor_comm.so and vrcompositor.real..."
gcc -O2 -shared -fPIC "${SRC_DIR}/libvrcompositor_comm.c" -o "${SRC_DIR}/libvrcompositor_comm.so" -ldl
gcc -O2 "${SRC_DIR}/vrcompositor_real_wrapper.c" -o "${SRC_DIR}/vrcompositor.real"

echo "[INFO] Applying SteamVR component patches..."
python3 "${SRC_DIR}/apply_steamvr_fixes.py"

if [ -f "${STEAMVR_LINUX64}/vrcompositor.bin" ]; then
    echo "[INFO] Granting cap_sys_nice=ep to vrcompositor.bin (may ask for sudo password)..."
    sudo setcap cap_sys_nice=ep "${STEAMVR_LINUX64}/vrcompositor.bin" || true
fi

echo "[OK] SteamVR component fixes applied successfully."
