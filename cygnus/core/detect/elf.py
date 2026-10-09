"""Minimal, bounds-checked ELF header/section reader (enough for AppImages)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from cygnus.core.errors import DetectionError

ELF_MAGIC = b"\x7fELF"
MACHINES = {0x03: "i686", 0x28: "armv7h", 0x3E: "x86_64", 0xB7: "aarch64", 0xF3: "riscv64"}
_MAX_SECTIONS = 4096


@dataclass(frozen=True, slots=True)
class Section:
    name: str
    offset: int
    size: int


@dataclass(frozen=True, slots=True)
class ElfInfo:
    bits: int
    little_endian: bool
    machine: int
    shoff: int
    shentsize: int
    shnum: int
    sections: tuple[Section, ...]

    @property
    def arch(self) -> str | None:
        return MACHINES.get(self.machine)

    @property
    def end_of_section_headers(self) -> int:
        """Where the AppImage runtime ends and the payload begins."""
        return self.shoff + self.shentsize * self.shnum

    def section(self, name: str) -> Section | None:
        return next((s for s in self.sections if s.name == name), None)


def read_elf(fh, file_size: int) -> ElfInfo:
    fh.seek(0)
    ident = fh.read(64)
    if len(ident) < 52 or ident[:4] != ELF_MAGIC:
        raise DetectionError("not an ELF file")
    bits = {1: 32, 2: 64}.get(ident[4])
    if bits is None or ident[5] not in (1, 2):
        raise DetectionError("unsupported ELF class or byte order")
    if len(ident) < (64 if bits == 64 else 52):
        raise DetectionError("truncated ELF header")
    le = ident[5] == 1
    e = "<" if le else ">"
    if bits == 64:
        machine = struct.unpack_from(e + "H", ident, 18)[0]
        shoff = struct.unpack_from(e + "Q", ident, 40)[0]
        shentsize, shnum, shstrndx = struct.unpack_from(e + "HHH", ident, 58)
        sh_fmt, sh_min = e + "IIQQQQIIQQ", 64
    else:
        machine = struct.unpack_from(e + "H", ident, 18)[0]
        shoff = struct.unpack_from(e + "I", ident, 32)[0]
        shentsize, shnum, shstrndx = struct.unpack_from(e + "HHH", ident, 46)
        sh_fmt, sh_min = e + "IIIIIIIIII", 40

    if shnum > _MAX_SECTIONS or (shnum and shentsize < sh_min):
        raise DetectionError("implausible ELF section header table")
    if shoff + shentsize * shnum > file_size:
        raise DetectionError("ELF section headers extend beyond end of file")

    raw: list[tuple[int, int, int]] = []  # (name_offset, offset, size)
    fh.seek(shoff)
    table = fh.read(shentsize * shnum)
    for i in range(shnum):
        fields = struct.unpack_from(sh_fmt, table, i * shentsize)
        name_off, offset, size = fields[0], fields[4], fields[5]
        raw.append((name_off, offset, size))

    sections: list[Section] = []
    if raw and shstrndx < len(raw):
        _, str_off, str_size = raw[shstrndx]
        if str_off + str_size <= file_size and str_size < 1 << 20:
            fh.seek(str_off)
            strtab = fh.read(str_size)
            for name_off, offset, size in raw:
                end = strtab.find(b"\0", name_off)
                name = strtab[name_off:end].decode("ascii", "replace") if 0 <= name_off < len(strtab) else ""
                if offset + size <= file_size:
                    sections.append(Section(name, offset, size))
    return ElfInfo(bits, le, machine, shoff, shentsize, shnum, tuple(sections))


def read_section(fh, section: Section, limit: int = 1 << 20) -> bytes:
    if section.size > limit:
        raise DetectionError(f"section {section.name} is unexpectedly large")
    fh.seek(section.offset)
    return fh.read(section.size)
