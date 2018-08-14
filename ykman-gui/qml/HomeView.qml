import QtQuick 2.5
import QtQuick.Controls 2.3
import QtQuick.Layouts 1.2

RowLayout {

    function getYubiKeyImageSource() {
        if (yubiKey.isYubiKey4()) {
            return "../images/yk4.png"
        }
        if (yubiKey.isSecurityKeyByYubico()) {
            return "../images/sky2.png"
        }
        if (yubiKey.isFidoU2fSecurityKey()) {
            return "../images/sky1.png"
        }
        if (yubiKey.isNEO()) {
            return "../images/neo.png"
        }
        if (yubiKey.isYubiKeyStandard()) {
            return "../images/standard.png"
        }
        return "../images/yk4.png" //default for now
    }

    RowLayout {
        Layout.fillHeight: true
        Layout.fillWidth: true
        Layout.margins: 20
        Layout.preferredHeight: app.height
        Layout.preferredWidth: app.width

        ColumnLayout {
            Layout.alignment: Qt.AlignHCenter | Qt.AlignVCenter
            Layout.fillWidth: true
            Heading1 {
                text: yubiKey.name
            }
            Label {
                visible: yubiKey.version
                color: yubicoBlue
                font.pointSize: constants.h2
                text: qsTr("Firmware: ") + yubiKey.version
            }
            Label {
                visible: yubiKey.serial
                color: yubicoBlue
                font.pointSize: constants.h2
                text: qsTr("Serial: ") + yubiKey.serial
            }
        }
        ColumnLayout {
            Layout.alignment: Qt.AlignHCenter | Qt.AlignVCenter
            Layout.fillWidth: true
            Image {
                fillMode: Image.PreserveAspectFit
                source: getYubiKeyImageSource()
            }
        }
    }
}