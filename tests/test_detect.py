import struct

import pytest

import builders
from cygnus.core.detect import detect_file, sniff_format
from cygnus.core.detect.appimage import parse_update_info
from cygnus.core.detect.deb import parse_dependencies
from cygnus.core.errors import DetectionError
from cygnus.core.models import PackageFormat


def codes(cand):
    return {f.code for f in cand.findings}


# -- Arch packages --------------------------------------------------------------------------------
@pytest.mark.parametrize("compression", ["zst", "xz", "gz"])
def test_pkg_metadata(tmp_path, compression):
    path = builders.build_pkg(tmp_path, compression=compression, depends=("glibc", "libpcap>=1.10"))
    cand = detect_file(path)
    assert cand.format is PackageFormat.LOCAL_PKG
    assert (cand.name, cand.version, cand.arch) == ("hello", "1.0-1", "x86_64")
    assert cand.depends == ["glibc", "libpcap>=1.10"]
    assert cand.provides == ["libhello.so=1-64"]
    assert cand.optional_depends == ["foo: optional foo support"]
    assert cand.installed_size == 12345
    assert "PKG_UNSIGNED_LOCAL" in codes(cand)
    assert "PKG_HAS_INSTALL_SCRIPT" not in codes(cand)


def test_pkg_install_script_and_signature(tmp_path):
    path = builders.build_pkg(tmp_path, install_script="post_install() { :; }\n")
    path.with_name(path.name + ".sig").write_bytes(b"sig")
    cand = detect_file(path)
    assert "PKG_HAS_INSTALL_SCRIPT" in codes(cand)
    assert "PKG_UNSIGNED_LOCAL" not in codes(cand)
    assert cand.metadata["detached_signature"] is True


def test_plain_compressed_tar_without_pkginfo_is_rejected(tmp_path):
    import io
    import tarfile

    path = tmp_path / "notapkg.tar.zst"
    with tarfile.open(path, "w:zst") as tar:
        info = tarfile.TarInfo("README")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"hi\n"))
    with pytest.raises(DetectionError, match="PKGINFO"):
        detect_file(path)


# -- Debian ---------------------------------------------------------------------------------------
def test_deb_metadata_and_scripts(tmp_path):
    path = builders.build_deb(tmp_path, scripts={"postinst": "#!/bin/sh\nchmod 4755 /opt/x/chrome-sandbox\n"})
    cand = detect_file(path)
    assert cand.format is PackageFormat.DEB
    assert (cand.name, cand.version, cand.arch) == ("hello", "1.2.3-1", "x86_64")
    assert cand.depends == ["libc6 (>= 2.34)", "libssl3 | libssl1.1"]
    assert cand.optional_depends == ["hello-doc"]
    assert cand.installed_size == 42 * 1024
    assert cand.summary == "Test package"
    assert "chrome-sandbox" in cand.metadata["maintainer_scripts"]["postinst"]
    assert {"DEB_MAINTAINER_SCRIPTS", "FOREIGN_PACKAGE_FORMAT"} <= codes(cand)


def test_deb_detected_by_content_not_extension(tmp_path):
    path = builders.build_deb(tmp_path)
    renamed = path.rename(tmp_path / "innocent.txt")
    assert detect_file(renamed).format is PackageFormat.DEB


def test_deb_member_beyond_eof_is_rejected(tmp_path):
    path = builders.build_deb(tmp_path)
    data = bytearray(path.read_bytes())
    # Inflate the size field of the first member far beyond the file.
    data[8 + 48 : 8 + 58] = b"9999999999"
    path.write_bytes(bytes(data))
    with pytest.raises(DetectionError):
        detect_file(path)


def test_parse_dependencies_alternatives_and_qualifiers():
    groups = parse_dependencies("libc6 (>= 2.34), python3:any | python3-minimal [amd64], foo")
    assert groups[0] == [{"name": "libc6", "op": ">=", "ver": "2.34"}]
    assert groups[1][0] == {"name": "python3", "qual": "any"}
    assert groups[1][1] == {"name": "python3-minimal", "arch": "amd64"}
    assert groups[2] == [{"name": "foo"}]


# -- RPM ------------------------------------------------------------------------------------------
def test_rpm_metadata(tmp_path):
    path = builders.build_rpm(tmp_path)
    cand = detect_file(path)
    assert cand.format is PackageFormat.RPM
    assert (cand.name, cand.version, cand.arch) == ("hello", "1.2.3-1", "x86_64")
    assert "rpmlib(PayloadIsZstd)" not in cand.depends
    assert "libc.so.6()(64bit)" in cand.depends and "/bin/sh" in cand.depends
    assert cand.metadata["scriptlets"]["postin"]["interpreter"] == "/bin/sh"
    assert {"RPM_SCRIPTLETS", "RPM_UNSIGNED"} <= codes(cand)
    assert cand.installed_size == 4096


def test_rpm_signed_and_noarch(tmp_path):
    cand = detect_file(builders.build_rpm(tmp_path, arch="noarch", postin=None, signed=True))
    assert cand.arch == "any"
    assert cand.metadata["signature_present"] is True
    assert "RPM_UNSIGNED" not in codes(cand) and "RPM_SCRIPTLETS" not in codes(cand)


def test_rpm_truncated_header_is_rejected(tmp_path):
    path = builders.build_rpm(tmp_path)
    data = path.read_bytes()
    path.write_bytes(data[:96 + 16 + 8])
    with pytest.raises(DetectionError):
        detect_file(path)


def test_rpm_implausible_index_is_rejected(tmp_path):
    path = builders.build_rpm(tmp_path)
    data = bytearray(path.read_bytes())
    struct.pack_into(">I", data, 96 + 8, 10_000_000)  # signature header nindex
    path.write_bytes(bytes(data))
    with pytest.raises(DetectionError):
        detect_file(path)


# -- AppImage -------------------------------------------------------------------------------------
def test_appimage_header_without_squashfs_tools(tmp_path):
    path = builders.build_appimage(tmp_path, with_squashfs=False)
    cand = detect_file(path)
    assert cand.format is PackageFormat.APPIMAGE
    assert cand.arch == "x86_64"
    assert cand.metadata["appimage_type"] == 2
    assert cand.metadata["payload_format"] == "squashfs"
    assert cand.metadata["update_info"] == {"type": "zsync", "url": "https://example.invalid/Hello.AppImage.zsync"}
    assert cand.metadata["signature_present"] is False
    assert "APPIMAGE_UNSIGNED" in codes(cand)


def test_appimage_signed_sections(tmp_path):
    cand = detect_file(builders.build_appimage(tmp_path, with_squashfs=False, signed=True))
    assert cand.metadata["signature_present"] is True
    assert "APPIMAGE_UNSIGNED" not in codes(cand)


def test_appimage_bad_section_table(tmp_path):
    path = builders.build_appimage(tmp_path, with_squashfs=False)
    data = bytearray(path.read_bytes())
    struct.pack_into("<Q", data, 40, len(data) * 4)  # e_shoff beyond EOF
    path.write_bytes(bytes(data))
    with pytest.raises(DetectionError):
        detect_file(path)


@pytest.mark.needs_tool("mksquashfs", "unsquashfs")
def test_appimage_metadata_from_squashfs(tmp_path):
    cand = detect_file(builders.build_appimage(tmp_path))
    assert cand.name == "Hello World"
    assert cand.version == "2.5.0"
    assert cand.summary == "Says hello"
    assert cand.identity["appstream_id"] == "org.example.Hello"
    assert cand.identity["desktop_id"] == "hello.desktop"
    assert cand.metadata["vendor"] == "Example Corp"
    assert cand.metadata["desktop_entry"]["MimeType"] == "x-scheme-handler/hello;text/plain;"
    assert cand.metadata["icon_kind"] == "png"


@pytest.mark.needs_tool("mksquashfs", "unsquashfs")
def test_appimage_symlink_escaping_the_image_is_not_followed(tmp_path):
    cand = detect_file(builders.build_appimage(tmp_path, escape_symlink=True))
    assert "desktop_entry" not in cand.metadata
    assert "root:" not in str(cand.metadata)


@pytest.mark.parametrize("text,expected", [
    ("zsync|https://x/y.zsync", {"type": "zsync", "url": "https://x/y.zsync"}),
    ("gh-releases-zsync|imputnet|helium-linux|latest|helium-*-x86_64.AppImage.zsync",
     {"type": "gh-releases-zsync", "owner": "imputnet", "repo": "helium-linux", "tag": "latest",
      "filename": "helium-*-x86_64.AppImage.zsync"}),
    ("pling-v1-zsync|123|app-*.AppImage", {"type": "pling-v1-zsync", "product_id": "123", "filename": "app-*.AppImage"}),
    ("\0\0\0", None),
    ("weird|thing", {"type": "unknown", "raw": "weird|thing"}),
])
def test_update_info_parsing(text, expected):
    assert parse_update_info(text) == expected


# -- Flatpak --------------------------------------------------------------------------------------
def test_flatpakref(tmp_path):
    path = tmp_path / "app.flatpakref"
    path.write_text(builders.FLATPAKREF)
    cand = detect_file(path)
    assert cand.format is PackageFormat.FLATPAK_REF_FILE
    assert cand.identity["flatpak_ref"] == "app/org.example.App//stable"
    assert cand.metadata["runtime_repo"] == "https://dl.example.invalid/example.flatpakrepo"
    assert cand.metadata["has_gpg_key"] is True


def test_flatpakref_without_gpg_key_warns(tmp_path):
    path = tmp_path / "app.flatpakref"
    path.write_text("\n".join(l for l in builders.FLATPAKREF.splitlines() if not l.startswith("GPGKey")))
    assert "FLATPAKREF_NO_GPG" in codes(detect_file(path))


@pytest.mark.needs_tool("ostree", "flatpak")
def test_flatpak_bundle(tmp_path):
    cand = detect_file(builders.build_flatpak_bundle(tmp_path))
    assert cand.format is PackageFormat.FLATPAK_BUNDLE
    assert cand.identity["flatpak_ref"] == "app/org.cygnus.TestApp/x86_64/stable"
    assert cand.metadata["runtime"] == "org.cygnus.TestPlatform/x86_64/1"
    assert cand.metadata["runtime_repo"] == "https://example.invalid/test.flatpakrepo"
    assert cand.metadata["permissions"]["shared"] == "network;"
    assert cand.depends == ["runtime/org.cygnus.TestPlatform/x86_64/1"]
    assert "FP_BUNDLE_NO_UPDATE_SOURCE" in codes(cand)


@pytest.mark.needs_tool("ostree", "flatpak")
def test_flatpak_bundle_with_origin(tmp_path):
    cand = detect_file(builders.build_flatpak_bundle(tmp_path, origin_url="https://example.invalid/repo"))
    assert cand.metadata["origin_url"] == "https://example.invalid/repo"
    assert "FP_BUNDLE_NO_UPDATE_SOURCE" not in codes(cand)


# -- general --------------------------------------------------------------------------------------
def test_unknown_file(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    with pytest.raises(DetectionError, match="unrecognised"):
        detect_file(path)


def test_directory_is_rejected(tmp_path):
    with pytest.raises(DetectionError, match="regular file"):
        detect_file(tmp_path)


def test_sniff_magic():
    assert sniff_format(b"\x7fELF\x02\x01\x01\x00AI\x02\x00") is PackageFormat.APPIMAGE
    assert sniff_format(b"\x7fELF\x02\x01\x01\x00\x00\x00\x00") is None  # plain ELF binary
    assert sniff_format(b"flatpak\x00\x01\x00\x89\xe5rest") is PackageFormat.FLATPAK_BUNDLE
    assert sniff_format(b"!<arch>\ndebian") is PackageFormat.DEB
    assert sniff_format(b"\xed\xab\xee\xdb") is PackageFormat.RPM
