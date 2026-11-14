#!/usr/bin/env python3
"""
Automated verification and application of all 4 critical fixes for SteamVR + ALVR on Linux:
1. Compositor launch chain:
   vrcompositor (symlink -> /usr/libexec/alvr/vrcompositor-wrapper)
   -> vrcompositor.real (C wrapper passing LD_PRELOAD=libvrcompositor_comm.so:alvr_drm_lease_shim.so)
   -> vrcompositor.bin (original compositor binary with binary & symbol patches).
2. Fix for SteamVR Error 303 ("A key component of SteamVR isn't working properly") & VRChat Error 109:
   libvrcompositor_comm.so sets /proc/<pid>/comm to "vrcompositor" via prctl(PR_SET_NAME).
3. Restoration of 6DoF physical head rotation & elimination of "Desync detected. Attempting recovery.":
   Restores _ZN13CRenderThread11UpdateAsyncEv and _ZN13CRenderThread6UpdateEv symbols in the stripped
   vrcompositor.bin and via the _ULx86_64_get_proc_name hook in libvrcompositor_comm.so.
4. Binary patch for vrcompositor.bin:
   Prevents the ~4-minute crash caused by ASSERT "Vulkan transfer ring buffer overflow" (vulkanrenderer.cpp:5502)
   by resetting the 256 MB ring buffer offset m_nTransferRingBufferOffset (0x460(%rbx)) back to 0.
5. Configuration in steamvr.vrsettings:
   disableLinuxWaitForPresent=true, forceDisableLinuxVulkanAsync=true (eliminates SIGSEGV in vkWaitForPresentKHR),
   enableSafeMode=false, driver_alvr_server.enable=true.
"""

import json
import os
import shutil
import struct
import subprocess

HOME = os.path.expanduser("~")
STEAM_ROOT = os.path.join(HOME, ".local/share/Steam")
if not os.path.isdir(STEAM_ROOT) and os.path.isdir(os.path.join(HOME, ".steam/steam")):
    STEAM_ROOT = os.path.realpath(os.path.join(HOME, ".steam/steam"))

STEAMVR_LINUX64 = os.path.join(STEAM_ROOT, "steamapps/common/SteamVR/bin/linux64")
VRSETTINGS_PATH = os.path.join(STEAM_ROOT, "config/steamvr.vrsettings")
FIX_DIR = os.path.dirname(os.path.abspath(__file__))

ALVR_WRAPPER = "/usr/libexec/alvr/vrcompositor-wrapper"
if not os.path.exists(ALVR_WRAPPER) and os.path.exists("/usr/lib/alvr/vrcompositor-wrapper"):
    ALVR_WRAPPER = "/usr/lib/alvr/vrcompositor-wrapper"

# Virtual addresses and first 16 bytes of CRenderThread::UpdateAsync / Update in SteamVR 2.18.4
RENDERTHREAD_PROLOGUES = {
    0x2878F0: bytes.fromhex("554889e54157415641554154534889fb"),
    0x28AB00: bytes.fromhex("554889e541574989ff4156415541544c"),
}


def _va_to_file_offset(data: bytes, va: int):
    """Translates a virtual address into a file offset using ELF64 program headers."""
    if data[:4] != b"\x7fELF" or data[4] != 2:
        return None
    e_phoff = struct.unpack_from("<Q", data, 0x20)[0]
    e_phentsize, e_phnum = struct.unpack_from("<HH", data, 0x36)
    for i in range(e_phnum):
        p_type, p_flags, p_offset, p_vaddr, _p_paddr, p_filesz = struct.unpack_from(
            "<IIQQQQ", data, e_phoff + i * e_phentsize
        )
        if p_type == 1 and (p_flags & 1) and p_vaddr <= va and va + 16 <= p_vaddr + p_filesz:
            return va - p_vaddr + p_offset
    return None


def is_supported_renderthread_layout(data: bytes) -> bool:
    """Checks that CRenderThread::UpdateAsync/Update are located at the SteamVR 2.18.4 offsets."""
    for va, prologue in RENDERTHREAD_PROLOGUES.items():
        off = _va_to_file_offset(data, va)
        if off is None or data[off : off + 16] != prologue:
            return False
    return True


def patch_ring_buffer_overflow(comp_bin: str) -> None:
    """Locates and patches the 256 MB Vulkan transfer ring buffer check in vrcompositor.bin."""
    with open(comp_bin, "rb") as f:
        data = bytearray(f.read())

    # Signature in CVulkanVRRenderer:
    # 48 8b bb 60 04 00 00    mov 0x460(%rbx),%rdi
    # 4a 8d 04 27             lea (%rdi,%r12,1),%rax
    # 48 3d ff ff ff 0f       cmp $0xfffffff,%rax
    # 0f 87 <rel32>           ja  <assert_block>
    sig = bytes.fromhex("488bbb600400004a8d0427483dffffff0f0f87")
    idx = data.find(sig)
    if idx == -1:
        return

    rel32 = struct.unpack_from("<i", data, idx + len(sig))[0]
    after_ja = idx + len(sig) + 4
    assert_offset = after_ja + rel32

    # 18-byte patch at assert_offset:
    # movq $0x0, 0x460(%rbx) ; xor %edi, %edi ; jmp after_ja
    jmp_back_rel32 = after_ja - (assert_offset + 18)
    patch = (
        bytes.fromhex("48c783600400000000000031ffe9")
        + struct.pack("<i", jmp_back_rel32)
    )

    if data[assert_offset : assert_offset + len(patch)] != patch:
        data[assert_offset : assert_offset + len(patch)] = patch
        tmp_bin = comp_bin + ".tmp"
        with open(tmp_bin, "wb") as f:
            f.write(data)
        os.chmod(tmp_bin, 0o755)
        os.replace(tmp_bin, comp_bin)
        print(f"[steamvr_fix] Applied Vulkan transfer ring buffer patch at offset {hex(assert_offset)}")


def ensure_renderthread_symbols(comp_bin: str) -> None:
    """Injects _ZN13CRenderThread11UpdateAsyncEv and _ZN13CRenderThread6UpdateEv into .symtab."""
    with open(comp_bin, "rb") as f:
        bin_data = f.read()
    if b"_ZN13CRenderThread11UpdateAsyncEv" in bin_data:
        return
    if not is_supported_renderthread_layout(bin_data):
        print("[steamvr_fix] Unsupported SteamVR build: CRenderThread prologues do not match SteamVR 2.18.4. Head-tracking symbol patch (#4) skipped.")
        return

    tmp_sym = comp_bin + ".sym.tmp"
    subprocess.run(
        [
            "objcopy",
            "--add-symbol",
            "_ZN13CRenderThread11UpdateAsyncEv=.text:0x136550,function,global",
            "--add-symbol",
            "_ZN13CRenderThread6UpdateEv=.text:0x139760,function,global",
            comp_bin,
            tmp_sym,
        ],
        check=True,
    )
    with open(tmp_sym, "rb") as f:
        sym_data = bytearray(f.read())
    for addr, size in [(0x2878F0, 0x3203), (0x28AB00, 0x34C3)]:
        needle = struct.pack("<BBHQQ", 0x12, 0, 26, addr, 0)
        idx = sym_data.find(needle)
        if idx != -1:
            sym_data[idx : idx + len(needle)] = struct.pack(
                "<BBHQQ", 0x12, 0, 26, addr, size
            )
    with open(tmp_sym, "wb") as f:
        f.write(sym_data)
    os.chmod(tmp_sym, 0o755)
    os.replace(tmp_sym, comp_bin)
    print("[steamvr_fix] Restored CRenderThread::UpdateAsync/Update symbols in vrcompositor.bin")


def ensure_compositor_chain() -> None:
    if not os.path.isdir(STEAMVR_LINUX64):
        return

    comp_link = os.path.join(STEAMVR_LINUX64, "vrcompositor")
    comp_real = os.path.join(STEAMVR_LINUX64, "vrcompositor.real")
    comp_bin = os.path.join(STEAMVR_LINUX64, "vrcompositor.bin")
    comp_orig = os.path.join(STEAMVR_LINUX64, "vrcompositor.bin.orig")
    comp_so = os.path.join(STEAMVR_LINUX64, "libvrcompositor_comm.so")

    if os.path.exists(comp_link) and not os.path.islink(comp_link):
        if os.path.getsize(comp_link) > 1_000_000:
            shutil.copy2(comp_link, comp_orig)
            shutil.move(comp_link, comp_bin)
        else:
            os.remove(comp_link)

    if os.path.exists(comp_real) and not os.path.islink(comp_real):
        if os.path.getsize(comp_real) > 1_000_000:
            shutil.copy2(comp_real, comp_orig)
            shutil.move(comp_real, comp_bin)

    if not os.path.exists(comp_bin) and os.path.exists(comp_orig):
        shutil.copy2(comp_orig, comp_bin)

    if os.path.exists(comp_bin) and os.path.getsize(comp_bin) > 1_000_000:
        patch_ring_buffer_overflow(comp_bin)
        ensure_renderthread_symbols(comp_bin)
        subprocess.run(["setcap", "cap_sys_nice=ep", comp_bin], stderr=subprocess.DEVNULL)

    fix_so = os.path.join(FIX_DIR, "libvrcompositor_comm.so")
    fix_real = os.path.join(FIX_DIR, "vrcompositor.real")
    if os.path.exists(fix_so):
        need_copy = not os.path.exists(comp_so)
        if not need_copy:
            with open(fix_so, "rb") as f1, open(comp_so, "rb") as f2:
                need_copy = f1.read() != f2.read()
        if need_copy:
            shutil.copy2(fix_so, comp_so + ".tmp")
            os.chmod(comp_so + ".tmp", 0o755)
            os.replace(comp_so + ".tmp", comp_so)
    if os.path.exists(fix_real):
        need_copy = not os.path.exists(comp_real)
        if not need_copy:
            with open(fix_real, "rb") as f1, open(comp_real, "rb") as f2:
                need_copy = f1.read() != f2.read()
        if need_copy:
            shutil.copy2(fix_real, comp_real + ".tmp")
            os.chmod(comp_real + ".tmp", 0o755)
            os.replace(comp_real + ".tmp", comp_real)

    if os.path.islink(comp_link):
        if os.readlink(comp_link) != ALVR_WRAPPER:
            os.remove(comp_link)
            os.symlink(ALVR_WRAPPER, comp_link)
    elif not os.path.exists(comp_link):
        os.symlink(ALVR_WRAPPER, comp_link)


def ensure_vrsettings() -> None:
    os.makedirs(os.path.dirname(VRSETTINGS_PATH), exist_ok=True)
    data = {}
    if os.path.exists(VRSETTINGS_PATH):
        try:
            with open(VRSETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    s = data.setdefault("steamvr", {})
    s["disableLinuxWaitForPresent"] = True
    s["forceDisableLinuxVulkanAsync"] = True
    s["enableLinuxVulkanAsync"] = False
    s["disableAsync"] = True
    s["enableSafeMode"] = False

    drv = data.setdefault("driver_alvr_server", {})
    drv["enable"] = True
    drv["blocked_by_safe_mode"] = False

    drv_null = data.setdefault("driver_null", {})
    drv_null["enable"] = False

    with open(VRSETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=3)


if __name__ == "__main__":
    ensure_compositor_chain()
    ensure_vrsettings()

