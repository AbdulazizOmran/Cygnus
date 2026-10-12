#!/bin/sh
# Translations of the window's text (see docs/translating.md).
#   packaging/translations.sh update   - rewrite the template cygnus/gui/i18n/cygnus.ts from the qsTr() texts in the QML (needs lupdate6)
#   packaging/translations.sh build    - compile every cygnus/gui/i18n/cygnus_<language>.ts into a .qm file next to it (needs lrelease6)
#   packaging/translations.sh merge L  - start cygnus/gui/i18n/cygnus_L.ts, or add the texts that are new to it (needs lupdate6); L is a language tag such as de or pt_BR
#   packaging/translations.sh coverage - how much of the screen text can be translated
set -eu
cd "$(dirname "$0")/.."
folder=cygnus/gui/i18n
need() { command -v "$1" >/dev/null 2>&1 || { echo "$1 is not installed (package qt6-tools)"; exit 1; }; }
case "${1:-}" in
  update)
    need lupdate6
    lupdate6 -no-obsolete -extensions qml -source-language en cygnus/gui/qml -ts "$folder/cygnus.ts" ;;
  build)
    for ts in "$folder"/cygnus_*.ts; do
      [ -e "$ts" ] || { echo "no translations yet (start one with: packaging/translations.sh merge <language>)"; exit 0; }
      need lrelease6
      lrelease6 -silent "$ts" -qm "${ts%.ts}.qm"
    done ;;
  merge)
    need lupdate6
    tag=$(printf '%s' "${2:-}" | tr '-' '_')
    case "$tag" in
      [A-Za-z][A-Za-z]|[A-Za-z][A-Za-z][A-Za-z]|[A-Za-z][A-Za-z]_[A-Za-z0-9][A-Za-z0-9]*|[A-Za-z][A-Za-z][A-Za-z]_[A-Za-z0-9][A-Za-z0-9]*) ;;
      *) echo "usage: $0 merge <language>   (for example de, fr, pt_BR, zh_Hant_TW)"; exit 2 ;;
    esac
    case "$tag" in *[!A-Za-z0-9_]*) echo "usage: $0 merge <language>   (for example de, fr, pt_BR, zh_Hant_TW)"; exit 2 ;; esac
    # lupdate makes the file itself when it is missing, and with the target language it knows how many forms of a plural this language has
    lupdate6 -no-obsolete -extensions qml -source-language en -target-language "$tag" cygnus/gui/qml -ts "$folder/cygnus_$tag.ts" ;;
  coverage)
    exec python3 packaging/translation_coverage.py ;;
  *)
    echo "usage: $0 update | build | merge <language> | coverage"; exit 2 ;;
esac
