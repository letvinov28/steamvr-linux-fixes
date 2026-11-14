# Bug Report for Valve (`ValveSoftware/SteamVR-for-Linux`)

> **Note:** Below is a clean, ready-to-paste GitHub issue (with no personal paths or private info) covering the 4 critical bugs found in **SteamVR 2.18.4 (Linux)** affecting `vrcompositor` and `vrserver`. You can submit it as a single consolidated issue or split each section into separate issues on [ValveSoftware/SteamVR-for-Linux](https://github.com/ValveSoftware/SteamVR-for-Linux/issues).

---

**Issue Title:**
`[SteamVR 2.18.4][Linux] Multiple vrcompositor / vrserver regressions: Vulkan transfer ring buffer overflow assert, vkWaitForPresentKHR SIGSEGV, 15-char comm check (Error 303), and stripped CRenderThread symbols`

---

**Issue Body (copy from below this line):**

### System Information
- **OS**: CachyOS Linux (Arch-based, KDE Plasma 6 Wayland)
- **Kernel**: `7.3.0-4-cachyos`
- **CPU**: 11th Gen Intel(R) Core(TM) i7-11850H @ 2.50GHz
- **GPU (Hybrid Setup)**:
  - iGPU (`GPU0`): Intel UHD Graphics (TGL GT1, `i915`)
  - dGPU (`GPU1`): AMD Radeon RX 7800 XT (`amdgpu` / RADV, Mesa `26.2.4`)
- **SteamVR Version**: `2.18.4` (Stable, `bin/version.txt`: `1792185270`, Steam `buildid`: `25998080`)
- **VR Driver / HMD**: ALVR `20.14.1` with Meta Quest 3S (Wi-Fi)

---

### Summary
While running **SteamVR 2.18.4** on Linux with a hybrid GPU setup (Intel iGPU + AMD Radeon dGPU) and an external Vulkan capture layer/driver (ALVR), we diagnosed **four distinct bugs in `vrcompositor` and `vrserver`**:

1. **`vrcompositor` aborts after ~4–5 minutes (`SIGABRT`)**: `ASSERT: "Vulkan transfer ring buffer overflow"` at `vulkanrenderer.cpp:5502` because the geometry upload path does not wrap the 256 MB ring buffer offset back to `0`.
2. **`vrcompositor` crashes at startup (`SIGSEGV`, exit code `139`)**: Unchecked `vkWaitForPresentKHR` call on virtual swapchains unless hidden setting `"disableLinuxWaitForPresent": true` is manually added.
3. **SteamVR Error 303 / `Init_InterfaceNotFound (109)` in `vrserver`**: `vrserver` identifies the compositor strictly via `/proc/<pid>/comm == "vrcompositor"`, which breaks whenever a wrapper renames the binary (e.g., `vrcompositor.real` gets truncated by the Linux kernel to 15 characters: `"vrcompositor.re"`).
4. **Stripped `vrcompositor` breaks pose lookup (`CRenderThread::Update` / `UpdateAsync`)**: Removing `.symtab` without providing `.dbg` breaks third-party Linux Vulkan capture layers that rely on `libunwind` to read `TrackedDevicePose_t` for the active frame.

---

### Bug 1: `ASSERT: "Vulkan transfer ring buffer overflow"` in `vrcompositor` (`vulkanrenderer.cpp:5502`)

#### Description
After ~4–5 minutes of streaming, `vrcompositor` crashes with `SIGABRT` and logs the following in `vrcompositor.txt`:
```text
ASSERT: "Vulkan transfer ring buffer overflow; transfer size 2315040!" at /data/src/common/vrcommon/vrrenderer/vulkanrenderer.cpp:5502.
```

#### Root Cause (from disassembly of `vrcompositor` `1792185270`)
`CVulkanVRRenderer` uses a 256 MB (`0x10000000` bytes) transfer ring buffer, storing `m_nTransferRingBufferOffset` at `0x460(%rbx)`.
- In the **texture upload path** (`VA 0x412ba5`), when `offset + size > 0xfffffff`, `0x460(%rbx)` is properly reset to `0` (`"Ring Buffer Wrapped!\n"`).
- In the **geometry buffer upload path** (`VA 0x411050` / file offset `0x410050`):
  ```asm
  411050: 48 8b bb 60 04 00 00    mov    0x460(%rbx),%rdi
  411057: 4a 8d 04 27             lea    (%rdi,%r12,1),%rax
  41105b: 48 3d ff ff ff 0f       cmp    $0xfffffff,%rax
  411061: 0f 87 41 01 00 00       ja     4111a8
  411067: 48 03 bb 48 04 00 00    add    0x448(%rbx),%rdi
  ```
  When `offset + size > 0xfffffff`, jumping to `0x4111a8` triggers an unconditional `AssertMsg` -> `abort()` instead of wrapping `m_nTransferRingBufferOffset` back to `0`.

#### Expected Behavior
The geometry upload path should wrap `m_nTransferRingBufferOffset` back to `0` when reaching the end of the 256 MB ring buffer, matching the texture upload path.

---

### Bug 2: `vrcompositor` immediate `SIGSEGV` (`139`) on `vkWaitForPresentKHR`

#### Description
When `vrcompositor` starts with an external Vulkan capture layer (such as `VK_LAYER_ALVR_capture` / `libalvr_vulkan_layer.so`) on a hybrid GPU system (Intel iGPU + AMD RADV dGPU), it immediately crashes with `SIGSEGV` (`139`) when calling `vkWaitForPresentKHR` on a virtual swapchain handle passed down to RADV (`radv_WaitForPresentKHR`).

#### Workaround Found
Disassembly showed an internal `steamvr.vrsettings` flag `"disableLinuxWaitForPresent"`. Setting:
```json
"steamvr": {
  "disableLinuxWaitForPresent": true,
  "forceDisableLinuxVulkanAsync": true
}
```
completely eliminates the crash.

#### Expected Behavior
`vrcompositor` should check whether `VK_KHR_present_wait` / `vkWaitForPresentKHR` is supported and valid for the active swapchain/layer before calling it (or safely fall back when an external swapchain layer is active).

---

### Bug 3: `vrserver` fails to detect `vrcompositor` via 15-char `/proc/<pid>/comm` check (Error 303 & `Init_InterfaceNotFound 109`)

#### Description
Even when `vrcompositor` is running and rendering frames, SteamVR shows **Error 303** (`Unable to get compositor interface (303)`), and OpenVR/Proton games (e.g., VRChat) fail to initialize VR with:
```text
[OpenVR] Could not initialize OpenVR. Error code: Init_InterfaceNotFound (109)
```
In `vrserver.txt`:
```text
timeout - vrcompositor process is not running.
```

#### Root Cause
When handling `VRMsg_StartVRCompositor`, `vrserver` scans `/proc/%d/comm` and performs an exact string comparison against `"vrcompositor"`.
On Linux, `prctl` / `/proc/<pid>/comm` is strictly limited by the kernel (`TASK_COMM_LEN`) to **15 characters**. Whenever a third-party driver or launcher uses a wrapper script/binary and renames the real binary to `vrcompositor.real` (standard practice for ALVR's `vrcompositor-wrapper`), the kernel truncates `/proc/<pid>/comm` to **`"vrcompositor.re"`**.
Because `"vrcompositor.re" != "vrcompositor"`, `vrserver` spins for ~10.3 seconds (`101 * 100 ms`) and returns `VRInitError_Compositor_Failed` (`303`).

#### Expected Behavior
`vrserver` should verify the compositor process using `/proc/<pid>/cmdline`, `/proc/<pid>/exe`, or accept a prefix match / IPC handshake rather than relying strictly on an exact 15-byte `/proc/<pid>/comm` match.

---

### Bug 4: `vrcompositor` binary shipped stripped breaks `libunwind` frame lookup for external Vulkan layers

#### Description
In **SteamVR 2.18.4**, `bin/linux64/vrcompositor` is shipped completely stripped (no `.symtab`, and no `.dbg` file).
External Linux VR layers (like ALVR's `libalvr_vulkan_layer.so`) rely on `libunwind` (`_ULx86_64_get_proc_name`) inside `vkQueuePresentKHR` to locate `CRenderThread::Update()` (`_ZN13CRenderThread6UpdateEv`) and `CRenderThread::UpdateAsync()` (`_ZN13CRenderThread11UpdateAsyncEv`) in order to associate the rendered frame with its `vr::TrackedDevicePose_t`.

Without these symbols:
- `_ULx86_64_get_proc_name` fails to match the render thread frames.
- Pose matching falls back to an identity matrix, causing client-side reprojection to constantly snap/recenter the view whenever the user physically rotates their head, along with continuous `[WARNING] Desync detected. Attempting recovery.` spam.

#### Expected Behavior
Either preserve/export the minimal symbols (`CRenderThread::Update`, `CRenderThread::UpdateAsync`) in `vrcompositor` (or ship debug symbols), or provide an official Vulkan/OpenVR frame-pose metadata mechanism for external drivers so they do not have to depend on `libunwind` stack walking.
