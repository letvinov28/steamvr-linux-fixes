# Valve SteamVR 2.18.4 Linux Component Fixes (`vrcompositor` & `vrserver`)

Low-level binary, library, and configuration fixes for **Valve SteamVR 2.18.4 (Stable, build `1792185270`)** on Linux (`vrcompositor` and `vrserver`), primarily affecting hybrid GPU setups (Intel/AMD iGPU + discrete AMD Radeon RADV) and external Vulkan capture layers/drivers such as **ALVR**.

> A ready-to-submit bug report for `ValveSoftware/SteamVR-for-Linux` is included in [`VALVE_BUG_REPORT.md`](VALVE_BUG_REPORT.md).

---

## Summary of Valve Component Bugs & Fixes

| # | Affected Component | Symptom / Error | Root Cause | Fix Implemented |
| :-: | :--- | :--- | :--- | :--- |
| **1** | `vrcompositor` (`vulkanrenderer.cpp:5502`) | `SIGABRT` crash after ~4–5 minutes (`ASSERT: "Vulkan transfer ring buffer overflow"`) | Geometry upload path does not wrap `m_nTransferRingBufferOffset` (`0x460(%rbx)`) back to `0` at 256 MB | 18-byte binary patch resetting offset to `0` and jumping back to normal execution |
| **2** | `vrcompositor` | Immediate `SIGSEGV` (`139`) at startup | Unchecked `vkWaitForPresentKHR` call on virtual swapchains with external Vulkan layers | Sets `"disableLinuxWaitForPresent": true` & `"forceDisableLinuxVulkanAsync": true` in `steamvr.vrsettings` |
| **3** | `vrserver` (`VRMsg_StartVRCompositor`) | **Error 303** (`timeout - vrcompositor process is not running`) & **Error 109** (`Init_InterfaceNotFound`) | `vrserver` strictly matches `/proc/<pid>/comm == "vrcompositor"`, which Linux truncates to `"vrcompositor.re"` for `vrcompositor.real` | `vrcompositor.real` wrapper + `libvrcompositor_comm.so` calling `prctl(PR_SET_NAME, "vrcompositor")` |
| **4** | `vrcompositor` (stripped binary) | Head rotation recenters in-world + `[WARNING] Desync detected` spam | `vrcompositor` is shipped without `.symtab`/`.dbg`, breaking `libunwind` frame lookup for `CRenderThread::Update*` | Restores symbols in `.symtab` via `objcopy` + hooks `_ULx86_64_get_proc_name` with prologue verification |

---

## Technical Details

### 1. `vrcompositor` `SIGABRT`: `ASSERT: "Vulkan transfer ring buffer overflow"` (`vulkanrenderer.cpp:5502`)
- **Cause**: `CVulkanVRRenderer` uses a 256 MB (`0x10000000`) transfer ring buffer with `m_nTransferRingBufferOffset` at `0x460(%rbx)`. While the texture upload path (`VA 0x412ba5`) wraps `0x460(%rbx)` back to `0` (`"Ring Buffer Wrapped!"`), the geometry upload path (`VA 0x411050` / file offset `0x410050`) jumps to `0x4111a8` on overflow and triggers an unconditional `AssertMsg` -> `abort()`.
- **Fix** (`src/apply_steamvr_fixes.py`): Locates the signature `488bbb600400004a8d0427483dffffff0f0f87` and replaces the `AssertMsg` block at the `ja` target with:
  ```asm
  movq   $0x0, 0x460(%rbx)
  xor    %edi, %edi
  jmp    after_ja
  ```

### 2. `vrcompositor` Startup `SIGSEGV` (`vkWaitForPresentKHR`)
- **Cause**: Starting in SteamVR 2.18, `vrcompositor` invokes `vkWaitForPresentKHR` on virtual swapchains created by external Vulkan capture layers, passing an internal handle down to RADV (`radv_WaitForPresentKHR`).
- **Fix** (`src/apply_steamvr_fixes.py`): Writes `"disableLinuxWaitForPresent": true` and `"forceDisableLinuxVulkanAsync": true` into `~/.local/share/Steam/config/steamvr.vrsettings`.

### 3. `vrserver` 15-Character `/proc/<pid>/comm` Truncation (Error 303 & `Init_InterfaceNotFound 109`)
- **Cause**: `vrserver` locates the compositor by reading `/proc/%d/comm` and requiring an exact match with `"vrcompositor"`. When a wrapper renames the binary to `vrcompositor.real`, the Linux kernel (`TASK_COMM_LEN = 16`) truncates `/proc/<pid>/comm` to `"vrcompositor.re"`, causing `vrserver` to time out after 10.3 seconds and return Error `303`. Additionally, `alvr_drm_lease_shim.so` calls `unsetenv("LD_PRELOAD")` in its constructor.
- **Fix** (`src/vrcompositor_real_wrapper.c` + `src/libvrcompositor_comm.c`):
  - `vrcompositor.real` sets `LD_PRELOAD=".../libvrcompositor_comm.so:/usr/libexec/alvr/alvr_drm_lease_shim.so"` and `execv`s `vrcompositor.bin`.
  - `libvrcompositor_comm.so` calls `prctl(PR_SET_NAME, "vrcompositor", 0, 0, 0)` in its constructor so `/proc/<pid>/comm` matches `"vrcompositor"`.

### 4. Stripped `vrcompositor` Breaking `libunwind` Pose Lookup (`CRenderThread::Update` / `UpdateAsync`)
- **Cause**: External Vulkan capture layers unwind the call stack via `libunwind` (`_ULx86_64_get_proc_name`) inside `vkQueuePresentKHR` to locate `_ZN13CRenderThread11UpdateAsyncEv` (`0x2878f0..0x28aaf3`) or `_ZN13CRenderThread6UpdateEv` (`0x28ab00..0x28dfc3`) and read `vr::TrackedDevicePose_t`. Because SteamVR 2.18.4 ships `vrcompositor` stripped, pose lookup falls back to an identity matrix, causing client-side reprojection to recenter the view on physical head rotation.
- **Fix** (`src/libvrcompositor_comm.c` + `src/apply_steamvr_fixes.py`):
  - Injects both symbols with their exact `st_size` (`0x3203` and `0x34c3`) into `vrcompositor.bin`'s `.symtab` via `objcopy --add-symbol`.
  - Hooks `_ULx86_64_get_proc_name` in `libvrcompositor_comm.so` (loading `libunwind.so.8` via `dlopen`) to resolve those address ranges.
  - **Version Safety Guard**: Both `apply_steamvr_fixes.py` and `libvrcompositor_comm.c` verify that the first 16 bytes at `0x2878f0` and `0x28ab00` (inside executable `PT_LOAD` segments) match SteamVR `2.18.4` (`1792185270`) before enabling patch #4. On any other SteamVR build, patch #4 is safely skipped/disabled.

---

## Quick Build & Apply

Requires `gcc`, `python3`, `binutils` (`objcopy`), and `libcap` (`setcap`).

```bash
chmod +x install_fixes.sh src/apply_steamvr_fixes.py
./install_fixes.sh
```

To verify whether the head-tracking hook is active at runtime:
```bash
ALVR_FIX_DEBUG=1 ~/.local/share/Steam/steamapps/common/SteamVR/bin/linux64/vrcompositor.real --version
```

---

## Compatibility & Rollback

- **Tested on**: SteamVR `2.18.4` (Stable, `bin/version.txt`: `1792185270`, Steam `buildid`: `25998080`) with ALVR `20.14.1` and AMD Radeon RADV (`Mesa 26.2.4`).
- **GPU note**: `src/vrcompositor_real_wrapper.c` enforces `DRI_PRIME=1` and `VK_DRIVER_FILES=/usr/share/vulkan/icd.d/radeon_icd.json` for hybrid Intel/AMD systems. Remove or adjust those two `setenv` lines if using a different GPU vendor.
- **Rollback**: A pristine backup is automatically saved as `~/.local/share/Steam/steamapps/common/SteamVR/bin/linux64/vrcompositor.bin.orig` (or verify SteamVR file integrity in Steam).
