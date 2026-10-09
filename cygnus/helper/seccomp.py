"""A syscall filter for the package reader: after it, a hostile package file that finds a bug in a parser can read
the file it was given and write its answer, and not much else.

The filter is a denylist (the reader is Python with libarchive and libalpm, whose needs are broad), but a denylist of
exactly the things an attacker wants: any new socket (so no talking to the system bus or any other service through a
filesystem socket, which a network namespace does not stop), new namespaces and mounts, tracing and loading code
into other processes or the kernel, keys, BPF, io_uring, and changing the clock or hostname.

It is one layer of several. In particular it does NOT stop a new process or thread: the C library's fork() and
thread creation use `clone`, which stays allowed (minus the namespace flags). What stops them is the process limit of
0 that the reader sets before it becomes the unprivileged user; an unprivileged process cannot raise it. Only the raw
`fork` and `vfork` calls are named here.

Details that matter, because filters like this are easy to get subtly wrong:
  * the numbers are the x86_64 ones, taken from the kernel header `asm/unistd_64.h` (a test compares them);
  * the architecture is checked first and any other is killed, and so is the 32-bit-on-64-bit "x32" number space
    (otherwise the same call could be made with a number the filter does not know);
  * `clone3` is answered with ENOSYS so the C library falls back to `clone`, whose flags the filter can inspect, and
    `clone` with any CLONE_NEW* flag is refused;
  * it is installed after "no new privileges", so it needs no privileges and cannot be removed or loosened.
On any other architecture it is not installed, and the reader says so truthfully (`seccomp: false`).
"""

from __future__ import annotations

import ctypes
import errno
import platform
import struct

AUDIT_ARCH_X86_64 = 0xC000003E
X32_SYSCALL_BIT = 0x40000000
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
# CLONE_NEWNS | NEWCGROUP | NEWUTS | NEWIPC | NEWUSER | NEWPID | NEWNET
CLONE_NEW_FLAGS = 0x00020000 | 0x02000000 | 0x04000000 | 0x08000000 | 0x10000000 | 0x20000000 | 0x40000000

# name -> x86_64 syscall number (checked against /usr/include/asm/unistd_64.h by the tests)
DENIED = {
    # talking to anything: new sockets of every kind (also the way to a filesystem socket such as the system bus)
    "socket": 41, "socketpair": 53, "connect": 42, "bind": 49, "listen": 50, "accept": 43, "accept4": 288,
    # namespaces, mounts, root changes
    "unshare": 272, "setns": 308, "mount": 165, "umount2": 166, "pivot_root": 155, "chroot": 161, "open_tree": 428,
    "move_mount": 429, "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433, "mount_setattr": 442,
    # looking into or changing other processes
    "ptrace": 101, "process_vm_readv": 310, "process_vm_writev": 311, "pidfd_open": 434, "pidfd_getfd": 438,
    # the kernel and its secrets
    "bpf": 321, "perf_event_open": 298, "userfaultfd": 323, "io_uring_setup": 425, "io_uring_enter": 426,
    "io_uring_register": 427, "add_key": 248, "request_key": 249, "keyctl": 250, "init_module": 175,
    "finit_module": 313, "delete_module": 176, "kexec_load": 246, "kexec_file_load": 320, "reboot": 169,
    "swapon": 167, "swapoff": 168, "acct": 163, "quotactl": 179, "lookup_dcookie": 212, "iopl": 172, "ioperm": 173,
    # the clock, the names, and ways around file permissions
    "settimeofday": 164, "clock_settime": 227, "adjtimex": 159, "clock_adjtime": 305, "sethostname": 170,
    "setdomainname": 171, "open_by_handle_at": 304, "name_to_handle_at": 303, "fanotify_init": 300, "mknod": 133,
    "mknodat": 259,
    # the raw fork calls (the C library does not use them; the process limit of 0 is what really stops new processes)
    "fork": 57, "vfork": 58,
}
NO_SYSCALL = {"clone3": 435}  # ENOSYS: the C library then uses clone, which the filter can look into
CLONE = 56


def available() -> bool:
    return platform.machine() == "x86_64"


def _insn(code: int, jt: int = 0, jf: int = 0, k: int = 0) -> bytes:
    return struct.pack("<HBBI", code, jt, jf, k)


BPF_LD_W_ABS, BPF_JEQ_K, BPF_JSET_K, BPF_RET_K = 0x20, 0x15, 0x45, 0x06


def program() -> bytes:
    """The filter as classic-BPF bytecode. Built with symbolic jump targets so every offset is computed, not counted."""
    deny, nosys = SECCOMP_RET_ERRNO | errno.EPERM, SECCOMP_RET_ERRNO | errno.ENOSYS
    ins: list[tuple] = []  # (code, jt_label | None, jf_label | None, k) with labels resolved below
    labels: dict[str, int] = {}

    def emit(code, k=0, jt=None, jf=None):
        ins.append((code, jt, jf, k))

    def label(name):
        labels[name] = len(ins)

    emit(BPF_LD_W_ABS, 4)  # seccomp_data.arch
    emit(BPF_JEQ_K, AUDIT_ARCH_X86_64, jt="start", jf="kill")
    label("start")
    emit(BPF_LD_W_ABS, 0)  # seccomp_data.nr
    emit(BPF_JSET_K, X32_SYSCALL_BIT, jt="kill", jf="names")
    label("names")
    for number in sorted(DENIED.values()):
        emit(BPF_JEQ_K, number, jt="deny", jf="")  # jf "" = the next instruction
    for number in NO_SYSCALL.values():
        emit(BPF_JEQ_K, number, jt="nosys", jf="")
    emit(BPF_JEQ_K, CLONE, jt="clone", jf="allow")
    label("clone")
    emit(BPF_LD_W_ABS, 16)  # seccomp_data.args[0]: clone's flags (the low 32 bits)
    emit(BPF_JSET_K, CLONE_NEW_FLAGS, jt="deny", jf="allow")
    label("allow")
    emit(BPF_RET_K, SECCOMP_RET_ALLOW)
    label("deny")
    emit(BPF_RET_K, deny)
    label("nosys")
    emit(BPF_RET_K, nosys)
    label("kill")
    emit(BPF_RET_K, SECCOMP_RET_KILL_PROCESS)

    out = b""
    for index, (code, jt, jf, k) in enumerate(ins):
        def offset(target):
            if target is None or target == "":
                return 0
            distance = labels[target] - index - 1
            if not 0 <= distance <= 255:
                raise ValueError(f"seccomp jump from {index} to {target} is out of range")
            return distance

        out += _insn(code, offset(jt), offset(jf), k)
    return out


def install() -> bool:
    """Install the filter on this process (it must already have set no-new-privileges). False where it does not apply."""
    if not available():
        return False
    code = program()
    buffer = ctypes.create_string_buffer(code, len(code))

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    prog = SockFprog(len(code) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot install the syscall filter")
    return True
