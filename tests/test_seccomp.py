"""The package reader's syscall filter: the numbers, the bytecode, and what the kernel really does with it."""

import json
import re
import signal
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from cygnus.helper import seccomp

REPO = Path(__file__).resolve().parent.parent
HEADER = Path("/usr/include/asm/unistd_64.h")
x86_64_only = pytest.mark.skipif(not seccomp.available(), reason="the filter is built for x86_64")

ALLOW = seccomp.SECCOMP_RET_ALLOW
KILL = seccomp.SECCOMP_RET_KILL_PROCESS
EPERM = seccomp.SECCOMP_RET_ERRNO | 1
ENOSYS = seccomp.SECCOMP_RET_ERRNO | 38
AUDIT_ARCH_I386 = 0x40000003


def verdict(nr, *, arch=seccomp.AUDIT_ARCH_X86_64, flags=0):
    """What the filter answers for one system call: a tiny classic-BPF interpreter (the four instructions it uses)."""
    code = seccomp.program()
    data = struct.pack("<IIQ6Q", nr & 0xFFFFFFFF, arch, 0, flags, 0, 0, 0, 0, 0)  # seccomp_data: nr, arch, ip, args[6]
    acc, pc = 0, 0
    for _ in range(len(code) // 8):  # a filter has no backward jumps, so it can never take more steps than it has lines
        op, jt, jf, k = struct.unpack_from("<HBBI", code, pc * 8)
        pc += 1
        if op == seccomp.BPF_LD_W_ABS:
            acc = struct.unpack_from("<I", data, k)[0]
        elif op == seccomp.BPF_JEQ_K:
            pc += jt if acc == k else jf
        elif op == seccomp.BPF_JSET_K:
            pc += jt if acc & k else jf
        elif op == seccomp.BPF_RET_K:
            return k
        else:
            raise AssertionError(f"unexpected instruction {op:#x}")
    raise AssertionError("the filter ran off its end")


# -- the data ----------------------------------------------------------------------------------------------------
@pytest.mark.skipif(not HEADER.exists(), reason="the kernel header asm/unistd_64.h is not installed")
def test_every_number_in_the_filter_is_the_one_the_kernel_header_gives():
    numbers = {name: int(nr) for name, nr in re.findall(r"^#define __NR_(\w+)\s+(\d+)\s*$", HEADER.read_text(), re.M)}
    wanted = {**seccomp.DENIED, **seccomp.NO_SYSCALL, "clone": seccomp.CLONE}
    wrong = {name: (nr, numbers.get(name)) for name, nr in wanted.items() if numbers.get(name) != nr}
    assert not wrong, f"name: (ours, kernel's) {wrong}"


def test_no_number_is_listed_twice_under_two_names():
    assert len(set(seccomp.DENIED.values())) == len(seccomp.DENIED)
    assert not set(seccomp.DENIED.values()) & set(seccomp.NO_SYSCALL.values())


def test_the_program_is_a_whole_number_of_instructions_and_within_the_kernels_limit():
    code = seccomp.program()
    assert len(code) % 8 == 0 and len(code) // 8 <= 4096  # BPF_MAXINSNS


# -- the bytecode, answered the way the kernel would ------------------------------------------------------------
@pytest.mark.parametrize("name, nr", sorted(seccomp.DENIED.items()))
def test_every_denied_call_is_refused_with_a_plain_error(name, nr):
    assert verdict(nr) == EPERM, name


def test_clone3_is_answered_as_missing_so_the_c_library_falls_back_to_clone():
    assert verdict(seccomp.NO_SYSCALL["clone3"]) == ENOSYS


@pytest.mark.parametrize("flag", [0x00020000, 0x02000000, 0x04000000, 0x08000000, 0x10000000, 0x20000000, 0x40000000])
def test_clone_that_asks_for_any_new_namespace_is_refused(flag):  # NEWNS NEWCGROUP NEWUTS NEWIPC NEWUSER NEWPID NEWNET
    assert verdict(seccomp.CLONE, flags=flag | 17) == EPERM


@pytest.mark.parametrize("flags", [17, 0x003D0F00])  # a plain fork (SIGCHLD); a thread
def test_clone_for_an_ordinary_process_or_thread_is_not_the_filters_business(flags):
    assert verdict(seccomp.CLONE, flags=flags) == ALLOW


@pytest.mark.parametrize("nr", [0, 1, 2, 3, 5, 9, 10, 11, 12, 13, 16, 28, 39, 59, 157, 202, 217, 231, 257, 262, 273, 318, 334])
def test_the_calls_a_reader_needs_are_left_alone(nr):  # read write open close fstat mmap ... futex getdents64 openat getrandom
    assert verdict(nr) == ALLOW


def test_another_architecture_and_the_x32_number_space_are_killed_not_trusted():
    assert verdict(39, arch=AUDIT_ARCH_I386) == KILL
    assert verdict(seccomp.X32_SYSCALL_BIT | 41) == KILL  # "socket", asked for through the other number space
    assert verdict(seccomp.X32_SYSCALL_BIT | 39) == KILL


# -- what the kernel does ---------------------------------------------------------------------------------------
PROBE = r"""
import ctypes, json, os, socket, sys
mode, repo = sys.argv[1:3]
sys.path.insert(0, repo)
sys.dont_write_bytecode = True
from cygnus.helper import seccomp
libc = ctypes.CDLL(None, use_errno=True)

def raw(number, *args):
    ctypes.set_errno(0)
    result = libc.syscall(ctypes.c_long(number), *[ctypes.c_long(a) for a in args])
    return -ctypes.get_errno() if result == -1 else result

def process_call(number, *args):
    # a call that makes a child when it works: the child ends at once, and the parent says what happened
    ctypes.set_errno(0)
    result = libc.syscall(ctypes.c_long(number), *[ctypes.c_long(a) for a in args])
    if result == 0:
        os._exit(0)  # this is the child
    if result > 0:
        os.waitpid(result, 0)
        return "made_a_process"
    return -ctypes.get_errno()

def errno_of(fn):
    try:
        fn()
    except OSError as exc:
        return exc.errno
    return 0

open("/proc/self/status").close()
if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS: what makes the filter installable without privileges
    raise SystemExit("no_new_privs")
filtered = seccomp.install() if mode in ("filtered", "x32") else False
result = {
    "installed": filtered,
    "status": [l for l in open("/proc/self/status").read().splitlines() if l.startswith("Seccomp:")],
    "socket": errno_of(lambda: socket.socket(socket.AF_UNIX)),
    "socketpair": errno_of(socket.socketpair),
    "unshare_user": errno_of(lambda: os.unshare(os.CLONE_NEWUSER)),
    "fork_syscall": process_call(57),
    "clone_new_user": process_call(56, 0x10000000 | 17, 0, 0, 0, 0),
    "clone_plain": process_call(56, 17, 0, 0, 0, 0),
    "clone3": raw(435, 0, 0),
    "still_works": [os.getpid() > 0, len(open("/proc/self/status").read()) > 0, os.stat("/").st_mode > 0],
}
print(json.dumps(result))
if mode == "x32":
    libc.syscall(ctypes.c_long(0x40000000 | 39))  # getpid, through the x32 number space
"""


def _probe(mode):
    return subprocess.run([sys.executable, "-B", "-I", "-c", PROBE, mode, str(REPO)], capture_output=True, text=True,
                          timeout=60)


@x86_64_only
def test_the_kernel_applies_the_filter_to_a_normal_user_process_and_only_to_what_it_names():
    control, filtered = _probe("control"), _probe("filtered")
    assert control.returncode == 0 and filtered.returncode == 0, (control.stderr, filtered.stderr)
    before, after = json.loads(control.stdout), json.loads(filtered.stdout)
    assert after["installed"] is True and after["status"] == ["Seccomp:\t2"] and before["status"] == ["Seccomp:\t0"]
    EPERM_, ENOSYS_ = 1, 38
    # each probe must have been possible before the filter (the control), or the answer afterwards proves nothing
    for name in ("socket", "socketpair", "unshare_user"):
        if before[name] != 0:
            pytest.skip(f"this machine refuses {name} to a normal user anyway, so the filter cannot be told apart")
        assert after[name] == EPERM_, name
    assert before["fork_syscall"] == "made_a_process" and after["fork_syscall"] == -EPERM_
    assert before["clone_new_user"] in ("made_a_process", -EPERM_) and after["clone_new_user"] == -EPERM_
    assert before["clone3"] != -ENOSYS_ and after["clone3"] == -ENOSYS_
    # and what is not named still works: ordinary files, an ordinary process-creating clone
    assert after["clone_plain"] == "made_a_process" and all(after["still_works"])


@x86_64_only
def test_a_process_that_uses_the_x32_number_space_is_killed():
    out = _probe("x32")
    assert out.returncode == -signal.SIGSYS, (out.returncode, out.stderr)
