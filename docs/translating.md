# Translating Cygnus

Cygnus can show the text of its window in another language. It ships **no translation of its own** (nothing is translated by a machine, and a
wrong word in an installer is worse than an English one), but everything is in place for a person who speaks the language to add one.

## What can be translated, honestly

- **The text written on the screens**: menus, page titles, buttons, descriptions, hints, status lines and the plain sentences on each page. The template
  (`cygnus/gui/i18n/cygnus.ts`) lists them: 376 texts at the time of writing. Counting every piece of text on the screens that reads like text for a
  person (a sentence that is put together from several pieces at run time counts as several), **about seven in ten are translatable**
  (`packaging/translations.sh coverage` prints the exact figures for the version you have).
- **Not yet**: the text the screens put together from several pieces at run time (a sentence with a name, a count and a verb in the middle), which is
  most of the long explanations on the Install and Application pages, and a few single words used as codes; and everything the program produces in Python: error messages, what a plan
  will do ("Install these packages…"), notes about a package, the Arch news, the terminal commands and their output, and the polkit
  password prompts. Those stay in English whatever the language, so a translated window is still a mixed one. Converting a built-up sentence is
  small, careful work (`qsTr("Show more (%1 left)").arg(n)` instead of `"Show more (" + n + " left)"`) and welcome.
- Nothing about this changes what Cygnus does: translated text is only text. Every confirmation, plan and warning is the same.

## Adding a language

You need `lupdate6` and `lrelease6` (the Qt 6 tools; on Arch the package `qt6-tools`) and a tool for translating `.ts` files (Qt Linguist,
Lokalize or any text editor: they are plain XML).

1. From the repository folder, create your file: `packaging/translations.sh merge de` (use your language, for example `de`, `fr`, `pt_BR`, `zh_Hant_TW`;
   a hyphen is turned into an underscore). This makes `cygnus/gui/i18n/cygnus_de.ts` with the right number of forms for each plural of your language,
   and running it again later adds the new texts without touching what you already translated.
2. Open `cygnus/gui/i18n/cygnus_de.ts` and fill in the `<translation>` of each `<source>`. Keep `%1`, `%2` and `%n` exactly where they are
   needed in your sentence: they are replaced by names and numbers. A text with `%n` has one form per plural of your language.
3. Compile it: `packaging/translations.sh build` (makes `cygnus_de.qm`).
4. Try it without installing anything: `CYGNUS_TRANSLATIONS=$PWD/cygnus/gui/i18n CYGNUS_LANGUAGE=de cygnus-gui`.
5. Send the `.ts` file (not the `.qm`; it is made from it) to the project. The package recipes run `packaging/translations.sh build`, which makes the
   `.qm` files from the `.ts` files; once the first translation is added, `qt6-tools` (which provides `lrelease6`) has to be a build dependency of the package
   (the script says so, plainly, when it is missing).

## How Cygnus picks a language

The language of your desktop, or `CYGNUS_LANGUAGE` when it is set (for example `CYGNUS_LANGUAGE=de` or `de,fr`). For `de_AT` it looks for
`cygnus_de_AT.qm` and `cygnus_de.qm`: both are loaded, so a text the regional file lacks comes from the general one. A list that reaches English
before a language that has a file (a desktop set to "English, then German") stays English, because English is what the texts are written in. The files are looked for in the folder named by `CYGNUS_TRANSLATIONS` (an absolute path, to try a file
before it is installed), next to the program, and in `/usr/share/cygnus/translations`. Only a regular file (not a link) of a reasonable size is
loaded; a language with no file, or a damaged file, simply leaves the window in English. A translation file is data for Qt's translator and is
never run.

## For maintainers

- Write new screen text as `qsTr("…")`; a sentence with values is one `qsTr` with `%1`, `%2` markers filled by `root.fill(qsTr("… %1 … %2"), [a, b])` (a chain of
  `.arg()` would fill a marker that a value itself contains). `tests/test_i18n.py` fails when the number of texts that read like text and are not translatable
  grows, and when the template differs from the QML: run `packaging/translations.sh update`. Identifiers and example addresses are not text: keep them out of `qsTr`.
- The window's text is in English in the source, and the template's source language is `en`.
