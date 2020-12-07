#!/usr/bin/env python
# -*- coding: utf-8 -*-


import datetime
import json
import logging
import os
import sys
import pyotherside
import smartcard
import struct
import types
import getpass
import urllib.parse
import ykman.logging_setup

from base64 import b32decode
from binascii import b2a_hex, a2b_hex
from fido2.ctap import CtapError
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from threading import Timer

from ykman import connect_to_device, scan_devices, get_name, get_connection_types
from ykman.piv import (
    get_pivman_data, list_certificates, generate_self_signed_certificate,
    generate_csr, OBJECT_ID, generate_chuid)
from ykman.otp import PrepareUploadFailed, prepare_upload_key, generate_static_pw
from ykman.scancodes import KEYBOARD_LAYOUT, encode
from ykman.util import (
    parse_certificates, parse_private_key, get_leaf_certificates)

from yubikit.core import CommandError
from yubikit.core.otp import modhex_encode, modhex_decode
from yubikit.core.smartcard import ApduError, SW
from yubikit.management import (
    USB_INTERFACE, Mode, ManagementSession, DeviceConfig,
    APPLICATION, TRANSPORT)
from yubikit.piv import (
    PivSession, SLOT, KEY_TYPE, check_key_support, NotSupportedError,
    PIN_POLICY, TOUCH_POLICY, InvalidPinError)
from yubikit.yubiotp import (
    YubiOtpSession, YubiOtpSlotConfiguration,
    StaticPasswordSlotConfiguration, HotpSlotConfiguration, HmacSha1SlotConfiguration)

from fido2.ctap2 import Ctap2, ClientPin

logger = logging.getLogger(__name__)


def as_json(f):
    def wrapped(*args, **kwargs):
        return json.dumps(f(*args, **kwargs))
    return wrapped


def catch_error(f):
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)

        except smartcard.pcsc.PCSCExceptions.EstablishContextException:
            return failure('pcsc_establish_context_failed')

        except ValueError as e:
            logger.error('Failed to open device', exc_info=e)
            return failure('open_device_failed')

        except Exception as e:
            if str(e) == 'Incorrect padding':
                return failure('incorrect_padding')
            logger.error('Uncaught exception', exc_info=e)
            return unknown_failure(e)

    return wrapped


def success(result={}):
    result['success'] = True
    return result


def failure(err_id, result={}):
    result['success'] = False
    result['error_id'] = err_id
    return result


def unknown_failure(exception):
    return failure(None, {'error_message': str(exception)})

class Controller(object):
    _dev_info = None
    _state = None

    def __init__(self):
        # Wrap all return values as JSON.
        for f in dir(self):
            if not f.startswith('_'):
                func = getattr(self, f)
                if isinstance(func, types.MethodType):
                    setattr(self, f, as_json(catch_error(func)))

    def count_devices(self):
        devices, state = scan_devices()
        return sum(devices.values())

    def _open_device(self, interfaces=USB_INTERFACE(sum(USB_INTERFACE))):
        return connect_to_device(connection_types=get_connection_types(interfaces))[0]

    def refresh(self):
        devices, state = scan_devices()
        n_devs = sum(devices.values())
        if n_devs != 1:
            return failure('multiple_devices')

        if state != self._state:
            self._state = state
            try:
                connection, device, info = connect_to_device()
                connection.close()
            except:
                self._state = None
                self._dev_info = None
                return failure('no_device')

            self._dev_info = {
                'name': get_name(info, device.pid.get_type()),
                'version': '.'.join(str(x) for x in info.version) if info.version else "",
                'serial': info.serial or '',
                'usb_enabled': [
                    a.name for a in APPLICATION
                    if a in info.config.enabled_applications.get(TRANSPORT.USB)],
                'usb_supported': [
                    a.name for a in APPLICATION
                    if a in info.supported_applications.get(TRANSPORT.USB)],
                'usb_interfaces_supported': [
                    t.name for t in USB_INTERFACE
                    if t in device.pid.get_interfaces()],
                'nfc_enabled': [
                    a.name for a in APPLICATION
                    if a in info.config.enabled_applications.get(TRANSPORT.NFC, [])],
                'nfc_supported': [
                    a.name for a in APPLICATION
                    if a in info.supported_applications.get(TRANSPORT.NFC, [])],
                'usb_interfaces_enabled': str(Mode.from_pid(device.pid)).split('+'),
                'can_write_config': info.version and info.version >= (5,0,0),
                'configuration_locked': info.is_locked,
                'form_factor': info.form_factor
            }

        return success({'dev': self._dev_info})

    def write_config(self, usb_applications, nfc_applications, lock_code):
        usb_enabled = 0x00
        nfc_enabled = 0x00
        for app in usb_applications:
            usb_enabled |= APPLICATION[app]
        for app in nfc_applications:
            nfc_enabled |= APPLICATION[app]

        with self._open_device() as conn:

            if lock_code:
                lock_code = a2b_hex(lock_code)
                if len(lock_code) != 16:
                    return failure('lock_code_not_16_bytes')

            try:
                session = ManagementSession(conn)
                session.write_device_config(
                    DeviceConfig(
                        {TRANSPORT.USB: usb_enabled,
                        TRANSPORT.NFC: nfc_enabled},
                        None,
                        None,
                        None,
                    ),
                    True,
                    lock_code)

                self._state = None
            except ApduError as e:
                if (e.sw == SW.VERIFY_FAIL_NO_RETRY):
                    return failure('wrong_lock_code')
                raise
            except ValueError as e:
                if str(e) == 'Configuration locked!':
                    return failure('interface_config_locked')
                raise

            return success()

    def refresh_piv(self):
        with self._open_device() as conn:
            session = PivSession(conn)
            pivman = get_pivman_data(session)

            return success({
                'piv_data': {
                    'certs': self._piv_list_certificates(session),
                    'has_derived_key': pivman.has_derived_key,
                    'has_protected_key': pivman.has_protected_key,
                    'has_stored_key': pivman.has_stored_key,
                    'pin_tries': session.get_pin_attempts(),
                    'puk_blocked': pivman.puk_blocked,
                    'supported_algorithms': self._supported_algorithms(self._dev_info['version'].split('.')),
                },
            })

    def _supported_algorithms(self, version):
        supported = []
        for key_type in KEY_TYPE:
            try:
                check_key_support(tuple(map(int, version)), key_type, PIN_POLICY.DEFAULT, TOUCH_POLICY.DEFAULT)
                supported.append(key_type.name)
            except NotSupportedError:
                pass
        return supported

    def set_mode(self, interfaces):
        with self._open_device() as dev:
            transports = sum([TRANSPORT[i] for i in interfaces])
            dev.mode = Mode(transports & TRANSPORT.usb_transports())
        return success()

    def get_username(self):
        username = getpass.getuser()
        return success({'username': username})

    def is_macos(self):
        return success({'is_macos': sys.platform == 'darwin'})

    def slots_status(self):
        with self._open_device(USB_INTERFACE.OTP) as conn:
            session = YubiOtpSession(conn)
            state = session.get_config_state()
            slot1 = state.is_configured(1)
            slot2 = state.is_configured(2)
            ans = [slot1, slot2]
            return success({'status': ans})

    def erase_slot(self, slot):
        with self._open_device(USB_INTERFACE.OTP) as conn:
            session = YubiOtpSession(conn)
            session.delete_slot(slot)
        return success()

    def swap_slots(self):
        with self._open_device(USB_INTERFACE.OTP) as conn:
            session = YubiOtpSession(conn)
            session.swap_slots()
        return success()

    def serial_modhex(self):
        with self._open_device(USB_INTERFACE.OTP) as conn:
            session = YubiOtpSession(conn)
            return modhex_encode(b'\xff\x00' + struct.pack(b'>I', session.get_serial()))

    def generate_static_pw(self, keyboard_layout):
        return success({
            'password': generate_static_pw(
                38, KEYBOARD_LAYOUT[keyboard_layout])
        })

    def random_uid(self):
        return b2a_hex(os.urandom(6)).decode('ascii')

    def random_key(self, bytes):
        return b2a_hex(os.urandom(int(bytes))).decode('ascii')

    def program_otp(self, slot, public_id, private_id, key, upload=False,
                    app_version='unknown'):
        key = a2b_hex(key)
        public_id = modhex_decode(public_id)
        private_id = a2b_hex(private_id)

        upload_url = None

        with self._open_device(USB_INTERFACE.OTP) as conn:
            if upload:
                try:
                    upload_url = prepare_upload_key(
                        key, public_id, private_id,
                        serial=self._dev_info['serial'],
                        user_agent='ykman-qt/' + app_version)
                except PrepareUploadFailed as e:
                    logger.debug('YubiCloud upload failed', exc_info=e)
                    return failure('upload_failed',
                                   {'upload_errors': [err.name
                                                      for err in e.errors]})
            try:
                session = YubiOtpSession(conn)
                session.put_configuration(
                    slot,
                    YubiOtpSlotConfiguration(public_id, private_id, key)
                )
            except CommandError as e:
                return failure("write error")

        logger.debug('YubiOTP successfully programmed.')
        if upload_url:
            logger.debug('Upload url: %s', upload_url)

        return success({'upload_url': upload_url})

    def program_challenge_response(self, slot, key, touch):
        key = a2b_hex(key)
        with self._open_device(USB_INTERFACE.OTP) as conn:
            session = YubiOtpSession(conn)
            try:
                session.put_configuration(
                    slot,
                    HmacSha1SlotConfiguration(key).require_touch(touch),
                )
            except CommandError as e:
                return failure("write error")
        return success()

    def program_static_password(self, slot, key, keyboard_layout):
        with self._open_device(USB_INTERFACE.OTP) as conn:
            session = YubiOtpSession(conn)
            scan_codes = encode(key, KEYBOARD_LAYOUT[keyboard_layout])
            try:
                session.put_configuration(slot, StaticPasswordSlotConfiguration(scan_codes))
            except CommandError as e:
                return failure("write error")
            return success()

    def program_oath_hotp(self, slot, key, digits):
        unpadded = key.upper().rstrip('=').replace(' ', '')
        key = b32decode(unpadded + '=' * (-len(unpadded) % 8))

        with self._open_device(USB_INTERFACE.OTP) as conn:
            session = YubiOtpSession(conn)
            try:
                session.put_configuration(
                    slot,
                    HotpSlotConfiguration(key)
                    .digits8(int(digits) == 8),
                )
            except CommandError as e:
                return failure("write error")
            return success()

    def fido_has_pin(self):
        with self._open_device(USB_INTERFACE.FIDO) as conn:
            ctap2 = Ctap2(conn)
            return success({'hasPin': ctap2.info.options.get("clientPin")})

    def fido_pin_retries(self):
        try:
            with self._open_device(USB_INTERFACE.FIDO) as conn:
                ctap2 = Ctap2(conn)
                client_pin = ClientPin(ctap2)
                return success({'retries': client_pin.get_pin_retries()[0]})
        except CtapError as e:
            if e.code == CtapError.ERR.PIN_AUTH_BLOCKED:
                return failure('PIN authentication is currently blocked. '
                               'Remove and re-insert the YubiKey.')
            if e.code == CtapError.ERR.PIN_BLOCKED:
                return failure('PIN is blocked.')
            raise

    def fido_set_pin(self, new_pin):
        try:
            with self._open_device(USB_INTERFACE.FIDO) as conn:
                ctap2 = Ctap2(conn)
                client_pin = ClientPin(ctap2)
                client_pin.set_pin(new_pin)
                return success()
        except CtapError as e:
            if e.code == CtapError.ERR.INVALID_LENGTH or \
                    e.code == CtapError.ERR.PIN_POLICY_VIOLATION:
                return failure('too long')
            raise

    def fido_change_pin(self, current_pin, new_pin):
        try:
            with self._open_device(USB_INTERFACE.FIDO) as conn:
                ctap2 = Ctap2(conn)
                client_pin = ClientPin(ctap2)
                client_pin.change_pin(current_pin, new_pin)
                return success()
        except CtapError as e:
            if e.code == CtapError.ERR.INVALID_LENGTH or \
                    e.code == CtapError.ERR.PIN_POLICY_VIOLATION:
                return failure('too long')
            if e.code == CtapError.ERR.PIN_INVALID:
                return failure('wrong pin')
            if e.code == CtapError.ERR.PIN_AUTH_BLOCKED:
                return failure('currently blocked')
            if e.code == CtapError.ERR.PIN_BLOCKED:
                return failure('blocked')
            raise

    def fido_reset(self):
        try:
            with self._open_device(USB_INTERFACE.FIDO) as conn:
                ctap2 = Ctap2(conn)
                ctap2.reset()
                return success()
        except CtapError as e:
            if e.code == CtapError.ERR.NOT_ALLOWED:
                return failure('not allowed')
            if e.code == CtapError.ERR.ACTION_TIMEOUT:
                return failure('touch timeout')
            raise

    def piv_reset(self):
        with self._open_device() as conn:
            session = PivSession(conn)
            session.reset()
            return success()

    def _piv_list_certificates(self, session):
        return {
            SLOT(slot).name: _piv_serialise_cert(slot, cert) for slot, cert in list_certificates(session).items()  # noqa: E501
        }

    def piv_delete_certificate(self, slot_name, pin=None, mgm_key_hex=None):
        logger.debug('piv_delete_certificate %s', slot_name)

        with self._open_device() as conn:
            session = PivSession(conn)
            with PromptTimeout():
                auth_failed = self._piv_ensure_authenticated(
                    session, pin=pin, mgm_key_hex=mgm_key_hex)
                if auth_failed:
                    return auth_failed

                session.delete_certificate(SLOT[slot_name])
                session.put_object(OBJECT_ID.CHUID, generate_chuid())
                return success()

    def piv_generate_certificate(
            self, slot_name, algorithm, common_name, expiration_date,
            self_sign=True, csr_file_url=None, pin=None, mgm_key_hex=None):
        logger.debug('slot_name=%s algorithm=%s common_name=%s '
                     'expiration_date=%s self_sign=%s csr_file_url=%s',
                     slot_name, algorithm, common_name, expiration_date,
                     self_sign, csr_file_url)
        if csr_file_url:
            file_path = self._get_file_path(csr_file_url)

        with self._open_device() as conn:
            session = PivSession(conn)
            with PromptTimeout():
                auth_failed = self._piv_ensure_authenticated(
                    session, pin=pin, mgm_key_hex=mgm_key_hex)
            if auth_failed:
                return auth_failed

            pin_failed = self._piv_verify_pin(session, pin)
            if pin_failed:
                return pin_failed

            if self_sign:
                now = datetime.datetime.utcnow()
                try:
                    year = int(expiration_date[0:4])
                    month = int(expiration_date[(4+1):(4+1+2)])
                    day = int(expiration_date[(4+1+2+1):(4+1+2+1+2)])
                    valid_to = datetime.datetime(year, month, day)
                except ValueError as e:
                    logger.debug(
                        'Failed to parse date: ' + expiration_date,
                        exc_info=e)
                    return failure(
                        'invalid_iso8601_date',
                        {'date': expiration_date})
            public_key = session.generate_key(
                SLOT[slot_name], KEY_TYPE[algorithm])

            pin_failed = self._piv_verify_pin(session, pin)
            if pin_failed:
                return pin_failed

            try:
                if self_sign:
                    cert = generate_self_signed_certificate(session,
                        SLOT[slot_name], public_key, common_name, now,
                        valid_to)
                    session.put_certificate(SLOT[slot_name], cert)
                    session.put_object(OBJECT_ID.CHUID, generate_chuid())

                else:
                    csr = generate_csr(session,
                        SLOT[slot_name], public_key, common_name)

                    with open(file_path, 'w+b') as csr_file:
                        csr_file.write(csr.public_bytes(
                            encoding=serialization.Encoding.PEM))

            except ApduError as e:
                if e.sw == SW.SECURITY_CONDITION_NOT_SATISFIED:
                    return failure('pin_required')
                raise

            return success()

    def piv_change_pin(self, old_pin, new_pin):
        with self._open_device() as conn:
            session = PivSession(conn)
            try:
                session.change_pin(old_pin, new_pin)
                logger.debug('PIN change successful!')
                return success()
            except InvalidPinError as e:
                logger.debug('Invalid PIN', exc_info=e)
                return failure('wrong_pin', {'tries_left': session.get_pin_attempts()})
            except ApduError as e:
                if e.sw == SW.INCORRECT_PARAMETERS:
                    return failure('incorrect_parameters')

                tries_left = session.get_pin_attempts()
                logger.debug('PIN change failed. %s tries left.',
                             tries_left, exc_info=e)
                return {
                    'success': False,
                    'tries_left': tries_left,
                }

    def piv_change_puk(self, old_puk, new_puk):
        with self._open_device() as conn:
            session = PivSession(conn)
            session.change_puk(old_puk, new_puk)
            return success()

    def piv_generate_random_mgm_key(self):
        return b2a_hex(ykman.piv.generate_random_management_key()).decode(
            'utf-8')

    def piv_change_mgm_key(self, pin, current_key_hex, new_key_hex,
                           store_on_device=False):
        with self._open_piv() as piv_controller:

            if piv_controller.has_protected_key or store_on_device:
                pin_failed = self._piv_verify_pin(
                    piv_controller, pin=pin)
                if pin_failed:
                    return pin_failed

            auth_failed = self._piv_ensure_authenticated(
                piv_controller, pin=pin, mgm_key_hex=current_key_hex)
            if auth_failed:
                return auth_failed

            try:
                new_key = a2b_hex(new_key_hex) if new_key_hex else None
            except Exception as e:
                logger.debug('Failed to parse new management key', exc_info=e)
                return failure('new_mgm_key_bad_hex')

            if new_key is not None and len(new_key) != 24:
                logger.debug('Wrong length for new management key: %d',
                             len(new_key))
                return failure('new_mgm_key_bad_length')

            piv_controller.set_mgm_key(
                new_key, touch=False, store_on_device=store_on_device)
            return success()

    def piv_unblock_pin(self, puk, new_pin):
        with self._open_device() as conn:
            session = PivSession(conn)

            session.unblock_pin(puk, new_pin)
            return success()

    def piv_can_parse(self, file_url):
        file_path = self._get_file_path(file_url)
        with open(file_path, 'r+b') as file:
            data = file.read()
            try:
                parse_certificates(data, password=None)
                return success()
            except (ValueError, TypeError):
                pass
            try:
                parse_private_key(data, password=None)
                return success()
            except (ValueError, TypeError):
                pass
        raise ValueError('Failed to parse certificate or key')

    # TODO test more
    def piv_import_file(self, slot, file_url, password=None,
                        pin=None, mgm_key=None):
        is_cert = False
        is_private_key = False
        file_path = self._get_file_path(file_url)
        if password:
            password = password.encode()
        with open(file_path, 'r+b') as file:
            data = file.read()
            try:
                certs = parse_certificates(data, password)
                is_cert = True
            except (ValueError, TypeError):
                pass
            try:
                private_key = parse_private_key(data, password)
                is_private_key = True
            except (ValueError, TypeError):
                pass

            if not (is_cert or is_private_key):
                return failure('failed_parsing')

            with self._open_device() as conn:
                session = PivSession(conn)
                with PromptTimeout():
                    auth_failed = self._piv_ensure_authenticated(
                        session, pin, mgm_key)
                    if auth_failed:
                        return auth_failed
                    if is_private_key:
                        session.put_key(SLOT[slot], private_key)
                    if is_cert:
                        if len(certs) > 1:
                            leafs = get_leaf_certificates(certs)
                            cert_to_import = leafs[0]
                        else:
                            cert_to_import = certs[0]

                        session.put_certificate(
                                SLOT[slot], cert_to_import)
                        session.put_object(OBJECT_ID.CHUID, generate_chuid())
        return success({
            'imported_cert': is_cert,
            'imported_key': is_private_key
        })

    def piv_export_certificate(self, slot, file_url):
        file_path = self._get_file_path(file_url)
        with self._open_device() as conn:
            session = PivSession(conn)
            cert = session.get_certificate(SLOT[slot])
            with open(file_path, 'wb') as file:
                file.write(
                    cert.public_bytes(
                        encoding=serialization.Encoding.PEM))
        return success()

    def _get_file_path(self, file_url):
        file_path = urllib.parse.urlparse(file_url).path
        return file_path[1:] if os.name == 'nt' else file_path

    def _piv_verify_pin(self, session, pin=None):
        if pin:
            try:
                session.verify_pin(pin)

            except:
                pass

        else:
            return failure('pin_required')

    def _piv_ensure_authenticated(self, session, pin=None,
                                  mgm_key_hex=None):
        pivman = get_pivman_data(session)
        if pivman.has_protected_key:
            return self._piv_verify_pin(session, pin)
        else:
            if mgm_key_hex:
                if len(mgm_key_hex) != 48:
                    return failure('mgm_key_bad_format')

                try:
                    mgm_key_bytes = a2b_hex(mgm_key_hex)
                except Exception:
                    return failure('mgm_key_bad_format')

                try:
                    session.authenticate(
                        mgm_key_bytes
                    )

                except:
                    pass

            else:
                return failure('mgm_key_required')


controller = None


def _piv_serialise_cert(slot, cert):
    if cert:
        # Try reading out issuer and subject,
        # may throw ValueError if malformed
        malformed = False
        try:
            issuer_cns = cert.issuer.get_attributes_for_oid(
                x509.NameOID.COMMON_NAME)
        except ValueError:
            malformed = True
            issuer_cns = None
        try:
            subject_cns = cert.subject.get_attributes_for_oid(
                x509.NameOID.COMMON_NAME)
        except ValueError:
            malformed = True
            subject_cns = None
        try:
            valid_from = cert.not_valid_before.date().isoformat()
        except ValueError:
            valid_from = None
        try:
            valid_to = cert.not_valid_after.date().isoformat()
        except ValueError:
            valid_to = None
    else:
        malformed = True
        issuer_cns = None
        subject_cns = None
        valid_from = None
        valid_to = None

    return {
        'slot': SLOT(slot).name,
        'malformed': malformed,
        'issuedFrom': issuer_cns[0].value if issuer_cns else '',
        'issuedTo': subject_cns[0].value if subject_cns else '',
        'validFrom': valid_from if valid_from else '',
        'validTo': valid_to if valid_to else ''
    }



def _touch_prompt():
    pyotherside.send('touchRequired')


def _close_touch_prompt():
    pyotherside.send('touchNotRequired')


def init_with_logging(log_level, log_file=None):
    logging_setup = as_json(ykman.logging_setup.setup)
    logging_setup(log_level, log_file)

    init()


def init():
    global controller
    controller = Controller()

class PromptTimeout:
    def __init__(self, timeout=0.5):
        self.timer = Timer(timeout, _touch_prompt)

    def __enter__(self):
        self.timer.start()

    def __exit__(self, typ, value, traceback):
        _close_touch_prompt()
        self.timer.cancel()
