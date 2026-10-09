import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// A progress bar with a line of text under it, for work that is going on.
// `value` is how far it REALLY is (0..1): a step of a known number, pacman's counter, the bytes of a download.
// When nothing real is known (a download without a size, a build, a lookup) it is -1 and the bar only shows that
// something is happening; it never makes a number up.
ColumnLayout {
    id: strip
    property bool running: false
    property real value: -1
    property string text: ""

    visible: running
    spacing: Kirigami.Units.smallSpacing

    QQC2.ProgressBar {
        Layout.fillWidth: true
        from: 0
        to: 1
        indeterminate: strip.value < 0
        value: strip.value < 0 ? 0 : strip.value
    }
    RowLayout {
        Layout.fillWidth: true
        QQC2.Label {
            textFormat: Text.PlainText
            Layout.fillWidth: true
            elide: Text.ElideRight
            text: strip.text
            opacity: 0.7
        }
        QQC2.Label {
            textFormat: Text.PlainText
            visible: strip.value >= 0
            text: Math.round(strip.value * 100) + "%"
            opacity: 0.7
        }
    }
}
