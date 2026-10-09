"""Ordinary packaging scripts that must keep converting, and hostile ones that must never classify as safe."""

# What real debhelper / rpm macros generate (these must NOT block a conversion).
BENIGN = {
    "debhelper-postinst": """#!/bin/sh
# postinst script for hello
set -e

case "$1" in
    configure)
        if [ -x "/usr/bin/update-desktop-database" ]; then
            update-desktop-database -q /usr/share/applications || true
        fi
        if [ -d /run/systemd/system ]; then
            systemctl --system daemon-reload >/dev/null || true
        fi
        if [ -x /usr/bin/deb-systemd-helper ]; then
            deb-systemd-helper unmask 'hello.service' >/dev/null || true
            if deb-systemd-helper --quiet was-enabled 'hello.service'; then
                deb-systemd-helper enable 'hello.service' >/dev/null || true
            else
                deb-systemd-helper update-state 'hello.service' >/dev/null || true
            fi
        fi
    ;;
    abort-upgrade|abort-remove|abort-deconfigure)
    ;;
    *)
        echo "postinst called with unknown argument \\`$1'" >&2
        exit 1
    ;;
esac

# Automatically added by dh_installdeb
dpkg-maintscript-helper rm_conffile /etc/hello/old.conf 1.0 -- "$@"
# End automatically added section

exit 0
""",
    "debhelper-prerm": """#!/bin/sh
set -e
# Automatically added by dh_installsystemd
if [ -d /run/systemd/system ] && [ "$1" = remove ]; then
    deb-systemd-invoke stop 'hello.service' >/dev/null || true
fi
# End automatically added section
""",
    "debhelper-postrm": """#!/bin/sh
set -e
if [ -d /run/systemd/system ]; then
    systemctl --system daemon-reload >/dev/null || true
fi
if [ "$1" = "remove" ]; then
    if [ -x "/usr/bin/deb-systemd-helper" ]; then
        deb-systemd-helper mask 'hello.service' >/dev/null || true
    fi
fi
if [ "$1" = "purge" ]; then
    if [ -x "/usr/bin/deb-systemd-helper" ]; then
        deb-systemd-helper purge 'hello.service' >/dev/null || true
        deb-systemd-helper unmask 'hello.service' >/dev/null || true
    fi
fi
exit 0
""",
    "icon-and-mime-caches": """#!/bin/sh
set -e
if [ "$1" = "configure" ] || [ "$1" = "abort-upgrade" ]; then
    if which update-mime-database >/dev/null 2>&1; then
        update-mime-database /usr/share/mime
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
    fi
    glib-compile-schemas /usr/share/glib-2.0/schemas 2>/dev/null || :
fi
ldconfig
exit 0
""",
    "rpm-post": """/sbin/ldconfig
/usr/bin/update-desktop-database &> /dev/null || :
touch --no-create /usr/share/icons/hicolor &>/dev/null || :
if [ -x /usr/bin/gtk-update-icon-cache ] ; then
  /usr/bin/gtk-update-icon-cache --quiet /usr/share/icons/hicolor || :
fi
""",
    "rpm-postun": """/sbin/ldconfig
if [ $1 -eq 0 ] ; then
    touch --no-create /usr/share/icons/hicolor &>/dev/null
    gtk-update-icon-cache /usr/share/icons/hicolor &>/dev/null || :
fi
""",
    "systemd-rpm-macros": """if [ $1 -eq 1 ] ; then
        # Initial installation
        systemctl preset hello.service >/dev/null 2>&1 || :
fi
""",
    "ordinary-helpers": """#!/bin/sh
set -e
if getent group hello >/dev/null; then true; fi
rmdir /opt/hello/old 2>/dev/null || true
if grep -q hello /proc/cmdline; then true; fi
install-info --quiet /usr/share/info/hello.info.gz || true
udevadm trigger --subsystem-match=block || true
install -d -o root -g root -m 0755 /opt/hello/cache
chown root /opt/hello/cache
chmod 0755 /opt/hello/bin/run
id -u hello >/dev/null 2>&1 || true
""",
    "message-only": """#!/bin/sh
echo "Thank you for installing hello" >&2
echo 'Run hello --help to start'
exit 0
""",
}

# Each of these does something the classifier must NOT call safe ("unknown" or blocking).
HOSTILE = {
    "pipe-to-tee": "echo x | tee -a /etc/sudoers.d/evil",
    "pipe-to-sh": "echo x | /bin/sh",
    "background-rm": "echo x & rm -rf /usr",
    "process-substitution": "echo <(rm -rf /usr)",
    "quoted-redirect": 'echo x >"/etc/passwd"',
    "quoted-append": "echo x >>'/etc/sudoers'",
    "redirect-usr-quoted": 'echo x > "/usr/bin/evil"',
    "redirect-home": "echo x >> ~/.bashrc",
    "redirect-home2": "echo x > $HOME/.profile",
    "redirect-dev": "echo x > /dev/sda",
    "brace-group": "{ rm -rf /usr; }",
    "if-rm": "if rm -rf /usr; then :; fi",
    "case-rm": "case $1 in stop) rm -rf /usr;; esac",
    "elif-rm": "if false; then :; elif rm -rf /usr; then :; fi",
    "comment-continuation": "# note \\\nrm -rf /usr",
    "cp-target-option": "cp /tmp/evil -t/usr/bin",
    "install-target-directory": "install -m 755 x --target-directory=/etc/sudoers.d",
    "mv-target-directory-sep": "mv x --target-directory /etc/cron.d",
    "heredoc": "cat <<EOF\nrm -rf /usr\nEOF",
    "here-string": "cat <<< hello",
    "dollar-paren": "echo $(rm -rf /usr)",
    "backtick": "echo `id`",
    "arithmetic": "echo $((1+1))",
    "ansi-c-quote": "echo $'\\x72\\x6d'",
    "function": "f() { rm -rf /usr; }; f",
    "exec": "exec /usr/bin/evil",
    "env-wrapper": "env FOO=1 /usr/bin/evil",
    "xargs": "echo x | xargs rm -rf",
    "sh-c": "sh -c 'rm -rf /usr'",
    "nohup": "nohup /usr/bin/evil",
    "timeout": "timeout 5 /usr/bin/evil",
    "sudo": "sudo /usr/bin/evil",
    "find-exec": "find /usr -exec rm {} +",
    "chroot": "chroot /x /bin/sh",
    "source": ". /tmp/evil.sh",
    "eval": "eval $X",
    "python-inline": "python3 -c 'import os; os.system(\"id\")'",
    "var-then-cmd": "FOO=bar /usr/bin/evil",
    "arith-expansion": "x='a[$(id)]'; echo $[x]",
    "parameter-evaluation": "echo ${x@P}",
    "indirect-expansion": "echo ${!x}",
    "arithmetic-test": "x='a[$(id)]'; [[ x -lt 1 ]]",
    "arithmetic-command": "(( x ))",
    "double-slash-path": "rm -f //etc/ld.so.preload",
    "double-slash-copy": "cp x //usr/bin/y",
    "chmod-minus-x-critical": "chmod -x /usr/bin/foo",
}
