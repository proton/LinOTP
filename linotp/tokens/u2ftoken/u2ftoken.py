#
#    LinOTP - the open source solution for two factor authentication
#    Copyright (C) 2010-2019 KeyIdentity GmbH
#    Copyright (C) 2019-     netgo software GmbH
#
#    This file is part of LinOTP server.
#
#    This program is free software: you can redistribute it and/or
#    modify it under the terms of the GNU Affero General Public
#    License, version 3, as published by the Free Software Foundation.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the
#               GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#
#    E-mail: info@linotp.de
#    Contact: www.linotp.org
#    Support: www.linotp.de
#

import base64
import binascii
import json
import logging
import struct
from hashlib import sha256

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from linotp.lib.auth.validate import check_otp, check_pin
from linotp.lib.error import ParameterError
from linotp.lib.policy import getPolicy
from linotp.lib.policy.action import get_action_value
from linotp.tokens import tokenclass_registry
from linotp.tokens.base import TokenClass

# x509 certificate support and elliptic signature verification


"""
    This file contains the U2F V2 token implementation as specified by the FIDO Alliance
"""


log = logging.getLogger(__name__)


@tokenclass_registry.class_entry("u2f")
@tokenclass_registry.class_entry("linotp.tokens.u2ftoken.U2FTokenClass")
class U2FTokenClass(TokenClass):
    """
    U2F token class implementation

    The U2F protocol as specified by the FIDO Alliance uses public key cryptography
    to perform second factor authentications. On registration the U2F compatible token
    creates a public/private key pair and sends the public key to the relying party
    (i.e. this LinOTP class). On authentication the U2F compatible token uses the
    private key to sign a challenge received from the relying party. This signature
    can be checked by the relying party using the public key received during
    registration.
    """

    def __init__(self, aToken):
        """
        constructor - create a token object

        :param aToken: instance of the orm db object
        :type aToken:  orm object

        """
        TokenClass.__init__(self, aToken)
        self.setType("u2f")
        self.mode = ["challenge"]  # This is a challenge response token
        self.supports_offline_mode = True

    @classmethod
    def getClassType(cls):
        """
        getClassType - return the token type shortname

        :return: 'U2F'
        :rtype: string

        """
        return "u2f"

    @classmethod
    def getClassPrefix(cls):
        return "u2f"

    @classmethod
    def getClassInfo(cls, key=None, ret="all"):
        """
        getClassInfo - returns a subtree of the token definition

        :param key: subsection identifier
        :type key: string

        :param ret: default return value, if nothing is found
        :type ret: user defined

        :return: subsection if key exists or user defined
        :rtype: s.o.

        """
        res = {
            "type": "u2f",
            "title": "U2F FIDO Token",
            "description": (
                "A U2F V2 token as specified by the FIDO Alliance. \
                Can be combined with the OTP PIN."
            ),
            "init": {},
            "config": {},
            "selfservice": {
                "enroll": {
                    "title": {
                        "html": "u2ftoken/u2ftoken.mako",
                        "scope": "selfservice.title.enroll",
                    },
                    "page": {
                        "html": "u2ftoken/u2ftoken.mako",
                        "scope": "selfservice.enroll",
                    },
                }
            },
            "policy": {
                "enrollment": {
                    "u2f_valid_facets": {"type": "str"},
                    "u2f_app_id": {"type": "str"},
                }
            },
        }

        if key is not None and key in res:
            ret = res.get(key)
        elif ret == "all":
            ret = res
        return ret

    def update(self, param, reset_failcount=False):
        self.setSyncWindow(0)
        self.setOtpLen(32)
        self.setCounterWindow(0)

        tdesc = param.get("description")
        if tdesc is not None:
            self.token.setDescription(tdesc)

        # requested_phase must be either "registration1" or "registration2"
        # current_phase is either "registration" or "authentication"
        requested_phase = param.get("phase")
        current_phase = self.getFromTokenInfo("phase", None)

        if requested_phase == "registration1" and current_phase is None:
            # This initial registration phase triggers a challenge
            # which is sent to the FIDO U2F compatible client device

            # Set the optional token pin in this first phase
            pin = param.get("pin")
            if pin is not None:
                TokenClass.setPin(self, pin)

            # preserve the registration state
            self.addToTokenInfo("phase", "registration")
            self.token.LinOtpIsactive = False
        elif requested_phase == "registration2" and current_phase == "registration":
            # Check the token pin
            pin = param.get("pin")
            if pin is None:
                pin = ""
            if check_pin(self, pin) is False:
                msg = "Wrong token pin!"
                raise ValueError(msg)
        # check for set phases which are not "registration1" or "registration2"
        elif (requested_phase != "registration2" and requested_phase is not None) or (
            current_phase != "authentication" and requested_phase is None
        ):
            msg = "Wrong phase parameter!"
            raise Exception(msg)
        # only allow "registration2" if the token already completed
        # "registration1"
        elif current_phase != "registration" and requested_phase == "registration2":
            msg = "Phase 'registration2' requested but we are not in the correct phase to process the request."
            raise Exception(msg)
        else:
            msg = 'Unknown "phase" and "current_phase" parameter combination!'
            raise Exception(msg)

    def splitPinPass(self, passw):
        """
        Split pin and otp given in the passw parameter

        :param passw: string representing pin+otp
        :return: returns tuple true or false for res, the pin value for pin
            and the otp value for otpval
        """
        # Split OTP from pin
        # Since we know that the OTP has to be valid JSON with format {"a":"b", "b":"c", ...}
        # we can parse the OTP for '{' beginning at the end of the OTP string
        splitIndex = passw.rfind("{")
        if splitIndex != -1:
            pin = passw[:splitIndex]
            otpval = passw[splitIndex:]
        else:
            # no valid JSON format - assume we got no otpval
            pin = passw
            otpval = ""

        return pin, otpval

    def is_challenge_request(self, passw, user, options=None):
        """
        check if the request would start a challenge

        - default: if the passw contains only the pin, this request would
        trigger a challenge

        - in this place as well the policy for a token is checked

        :param passw: password, which might be pin or pin+otp
        :param options: dictionary of additional request parameters

        :return: returns true or false
        """
        return check_pin(self, passw, user=user, options=options)

    def createChallenge(self, transactionid, options=None):
        """
        create a challenge, which is submitted to the user

        :param state: the state/transaction id
        :param options: the request context parameters / data
        :return: tuple of (bool, message and data)
                 message is submitted to the user
                 data is preserved in the challenge
                 attributes are additional attributes, which could be returned
        """
        # Create an otp key (from urandom) which is used as challenge, 32 bytes
        # long
        challenge = base64.urlsafe_b64encode(binascii.unhexlify(self._genOtpKey_(32)))

        # We delete all '=' symbols we added during registration to ensure that the
        # challenge object is sent to exact the same keyHandle we received in the
        # registration. Otherwise some U2F tokens won't respond.
        keyHandle = self.getFromTokenInfo("keyHandle")
        keyHandleIndex = 1
        while keyHandle[-keyHandleIndex] == "=":
            keyHandleIndex = keyHandleIndex + 1
        if keyHandleIndex > 1:
            keyHandle = keyHandle[: -(keyHandleIndex - 1)]

        appId = self._get_app_id()

        data = {
            "challenge": challenge.decode("ascii"),
            "version": "U2F_V2",
            "keyHandle": keyHandle,
            "appId": appId,
        }
        message = "U2F challenge"
        attributes = {"signrequest": data}

        return (True, message, data, attributes)

    def _is_valid_facet(self, origin):
        """
        check if origin is in the valid facets if the u2f_valid_facets policy is set.
        Otherwise check if the origin matches the previously saved origin

        :return:          boolean - True if supported, False if unsupported
        """
        is_valid = False

        # Get the valid facets as specified in the enrollment policy 'u2f_valid_facets'
        # for the specific realm
        valid_facets_action_value = ""
        realms = self.token.getRealmNames()
        if len(realms) > 0:
            get_policy_params = {
                "action": "u2f_valid_facets",
                "scope": "enrollment",
                "realm": realms[0],
            }
            policies = getPolicy(get_policy_params)
            valid_facets_action_value = get_action_value(
                policies,
                scope="enrollment",
                action="u2f_valid_facets",
                default="",
            )

        if valid_facets_action_value != "":
            # 'u2f_valid_facets' policy is set - check if origin is in valid facets list
            valid_facets = valid_facets_action_value.split(";")
            for facet in valid_facets:
                facet = facet.strip()
            if origin in valid_facets:
                is_valid = True
        else:
            # 'u2f_valid_facets' policy is empty or not set
            # check if origin matches the origin stored in the token info or save it if no origin
            # is stored yet
            appId = self._get_app_id()
            if appId == origin:
                is_valid = True

        return is_valid

    def _get_app_id(self):
        """
        Get the appId saved in the TokenInfo.
        :return: appId
        """
        # Get the appId from TokenInfo
        appId = self.getFromTokenInfo("appId", "")
        if appId == "":
            msg = "appId could not be determined."
            raise Exception(msg)

        return appId

    @staticmethod
    def _handle_client_errors(client_response):
        """
        Check the U2F client response for U2F client errors.
        Raises an Exception if an U2F client error code was found.
        :param client_response: U2F client response object
        :return:
        """
        error_codes = {
            0: "OK",
            1: "OTHER_ERROR",
            2: "BAD_REQUEST",
            3: "CONFIGURATION_UNSUPPORTED",
            4: "DEVICE_INELIGIBLE",
            5: "TIMEOUT",
        }

        if "errorCode" in client_response:
            error_code = client_response["errorCode"]
            error_text = error_codes.get(error_code, "")
            error_msg = client_response.get("errorMessage", "")
            msg = f"U2F client error code: {error_text} ({error_code}): {error_msg}"
            raise Exception(msg)

    def _checkClientData(self, clientData, clientDataType, challenge):
        """
        checkClientData - checks whether the clientData object retrieved
        from the U2F token is valid

        :param clientData:        the stringified JSON clientData object
        :param clientDataType:    either 'registration' or 'authentication'
        :param challenge:         the challenge this clientData object belongs to
        :return:                  the origin as extracted from the clientData object
        """
        try:
            clientData = json.loads(clientData)
        except ValueError as exx:
            msg = "Invalid client data JSON format"
            raise Exception(msg) from exx

        try:
            cdType = clientData["typ"]
            cdChallenge = clientData["challenge"]
            cdOrigin = clientData["origin"]
            # TODO: Check for optional cid_pubkey
        except KeyError as exx:
            msg = "Wrong client data format!"
            raise Exception(msg) from exx

        # validate typ
        if clientDataType == "registration":
            if cdType != "navigator.id.finishEnrollment":
                msg = "Incorrect client data object received!"
                raise Exception(msg)
        elif clientDataType == "authentication":
            if cdType != "navigator.id.getAssertion":
                msg = "Incorrect client data object received!"
                raise Exception(msg)
        else:
            # Wrong function call
            msg = "Wrong validClientData function call."
            raise Exception(msg)

        # validate challenge
        if cdChallenge != challenge:
            log.debug(
                "Challenge mismatch - The received challenge in the received client \
                       data object does not match the sent challenge!"
            )
            return False

        # validate origin
        if not self._is_valid_facet(cdOrigin):
            log.debug('Facet "%s" is not in valid_facets.', cdOrigin)
            return False

        return True

    def _parseSignatureData(self, signatureData):
        """
        Internal helper function to parse the signatureData received on authentication
        according to the U2F specification

        :param signatureData: Raw signature data as sent from the U2F token
        :return:              Tuple of (userPresenceByte, counter, signature)
        """

        FIRST_BIT_MASK = 0b00000001
        COUNTER_LEN = 4

        # first bit has to be 1 in the current FIDO U2F_V2 specification
        # since authentication responses without requiring user presence
        # are not yet supported by the U2F specification
        if FIRST_BIT_MASK & ord(signatureData[:1]) != 0b00000001:
            log.error("Wrong signature data format: User presence bit must be set")
            msg = "Wrong signature data format"
            raise ValueError(msg)
        userPresenceByte = signatureData[:1]
        signatureData = signatureData[1:]

        # next 4 bytes refer to the counter
        if len(signatureData) < COUNTER_LEN:
            log.error("Wrong signature data format: signature data too short")
            msg = "Wrong signature data format"
            raise ValueError(msg)
        counter = signatureData[:COUNTER_LEN]
        signatureData = signatureData[COUNTER_LEN:]

        # the remaining part of the signatureData is the signature itself
        # We do not allow an empty string as a signature
        if len(signatureData) == 0:
            log.error("Wrong signature data format: signature data too short")
            msg = "Wrong signature data format"
            raise ValueError(msg)
        signature = signatureData

        return (userPresenceByte, counter, signature)

    @staticmethod
    def _checkCounterOverflow(counter, prevCounter):
        """
        Internal helper function to check the counter in the range of an overflow

        :param counter:       the received counter value
        :param prevCounter:   the previously saved counter value
        :return:              boolean, True on legal overflow, False on illegal counter decrease
        """
        # TODO: Create Policy to adjust the OVERFLOW_RANGE
        OVERFLOW_RANGE = 1000
        res = False
        if prevCounter >= (256**4) - OVERFLOW_RANGE and counter <= OVERFLOW_RANGE:
            # This is the range of a legal overflow
            res = True
        return res

    def _verifyCounterValue(self, counter):
        """
        Internal helper function to verify the counter value received on an authentication response.
        This counter value MUST increase on every authentication event (except for an overflow to 0)
        as outlined in the FIDO U2F specification.
        However, this counter is allowed to be 'global' on the token device, i.e. one counter for
        ALL applications used with this token. Therefore we cannot check for a wrap around to
        exactly 0.
        Since we know that the maximum counter value is exactly 256 ** 4 (4 bytes counter), we can
        implement a range where a wrap around of the counter value is allowed.

        :param counter: the received counter value
        :return:
        """
        prevCounter = int(self.getFromTokenInfo("counter", None))

        # Did the counter not increase?
        if not counter > prevCounter:  # noqa: SIM102
            # Is this a legal overflow?
            if self._checkCounterOverflow(counter, prevCounter) is False:
                # Since a decreasing counter value is a hint to a device cloning, we
                # deactivate the token. This could also happen if you use the token
                # A LOT with other applications and very seldom with LinOTP.
                self.token.LinOtpIsactive = False
                msg = "Counter not increased! Possible device cloning!"
                raise ValueError(msg)

        # save the new counter
        self.addToTokenInfo("counter", counter)

    def _validateAuthenticationSignature(
        self,
        applicationParameter,
        userPresenceByte,
        counter,
        challengeParameter,
        publicKey,
        signature,
    ):
        """
        Internal helper function to validate the authentication signature received after parsing
        the token authentication response according to the U2F specification

        :param applicationParameter: SHA-256 hash of the application identity.
        :param userPresenceByte:     The user presence byte as received in the authentication
                                     response
        :param challengeParameter:   SHA-256 hash of the Client Data, a stringified JSON data
                                     structure prepared by the FIDO Client.
        :param publicKey:            The user public key retrieved on parsing the registration data
        :param signature:            The signature to be verified as retrieved on parsing the
                                     authentication response
        """

        # ------------------------------------------------------------------ --

        # we require an ASN1 prefix in front of the public key so that it
        # could be imported

        PUB_KEY_ASN1_PREFIX = bytes.fromhex(
            "3059301306072a8648ce3d020106082a8648ce3d030107034200"
        )

        asn1_publicKey = PUB_KEY_ASN1_PREFIX + publicKey

        # ------------------------------------------------------------------ --

        # According to the FIDO U2F specification the signature is a ECDSA
        # signature on the NIST P-256 curve over the SHA256 hash of the
        # following byte string:

        message = applicationParameter + userPresenceByte + counter + challengeParameter

        # ------------------------------------------------------------------ --

        # verify with the asn1, der encoded public key

        ecc_pub = serialization.load_der_public_key(asn1_publicKey, default_backend())

        try:
            ecc_pub.verify(signature, message, ec.ECDSA(hashes.SHA256()))
            return True

        except InvalidSignature:
            log.debug("Signature verification failed!")
            return False

        except Exception as exx:
            log.error("Signature verification failed! %r", exx)
            raise

    def checkResponse4Challenge(self, user, passw, options=None, challenges=None):
        """
        This method verifies if the given ``passw`` matches any existing ``challenge``
        of the token.

        It then returns the new otp_counter of the token and the
        list of the matching challenges.

        In case of success the otp_counter needs to be > 0.
        The matching_challenges is passed to the method
        :py:meth:`~linotp.tokens.base.TokenClass.challenge_janitor`
        to clean up challenges.

        :param user: the requesting user
        :type user: User object
        :param passw: the password (pin+otp)
        :type passw: string
        :param options:  additional arguments from the request, which could be token specific
        :type options: dict
        :param challenges: A sorted list of valid challenges for this token.
        :type challenges: list
        :return: tuple of (otpcounter and the list of matching challenges)
        """
        if not challenges:
            return -1, []

        otp_counter = -1
        matching_challenges = []

        for challenge in challenges:
            # Split pin from otp and check the resulting pin and otpval
            (pin, otpval) = self.splitPinPass(passw)
            if not check_pin(self, pin, user=user, options=options):
                otpval = passw
            # The U2F checkOtp functions needs to know the saved challenge
            # to compare the received challenge value to the saved one,
            # thus we add the transactionid to the options
            options["transactionid"] = challenge.transid
            options["challenges"] = challenges

            _otp_counter = check_otp(self, otpval, options=options)
            if _otp_counter >= 0:
                matching_challenges.append(challenge)

                # ensure that a positive otp_counter is preserved
                otp_counter = _otp_counter

        return otp_counter, matching_challenges

    def checkOtp(self, passw, counter, window, options=None):
        """
        checkOtp - standard callback of linotp to verify the token

        :param passw:      the passw / otp, which has to be checked
        :type passw:       string
        :param counter:    the start counter
        :type counter:     int
        :param window:     the window, in which the token is valid
        :type window:      int
        :param options:    options
        :type options:     dict

        :return:           verification counter or -1
        :rtype:            int (-1)
        """
        ret = -1

        challenges = []
        serial = self.getSerial()
        transid = options.get("transactionid", None)
        if transid is None:
            msg = "Could not checkOtp due to missing transaction id"
            raise Exception(msg)

        # get all challenges with a matching trasactionid
        if "challenges" in options:
            challs = options["challenges"]
        else:
            challs = []
            log.debug("Could not find a challenge")

        for chall in challs:
            (rec_tan, rec_valid) = chall.getTanStatus()
            if rec_tan is False:
                # add all untouched challenges
                challenges.append(chall)
            elif rec_valid is False:
                # don't allow touched but failed challenges
                pass

        if len(challenges) == 0:
            err = f"No open transaction found for token {serial} and transactionid {transid}"
            raise Exception(err)

        # decode the retrieved passw object
        try:
            authResponse = json.loads(passw)
        except ValueError as exx:
            msg = "Invalid JSON format"
            raise Exception(msg) from exx

        self._handle_client_errors(authResponse)

        try:
            signatureData = authResponse.get("signatureData", None)
            clientData = authResponse["clientData"]
            keyHandle = authResponse["keyHandle"]
        except AttributeError as exx:
            msg = "Couldn't find keyword in JSON object"
            raise Exception(msg) from exx

        # Does the keyHandle match the saved keyHandle created on registration?
        # Remove trailing '=' on the saved keyHandle
        savedKeyHandle = self.getFromTokenInfo("keyHandle", None)
        while savedKeyHandle.endswith("="):
            savedKeyHandle = savedKeyHandle[:-1]
        if keyHandle is None or keyHandle != savedKeyHandle:
            return -1

        # signatureData and clientData are urlsafe base64 encoded
        # correct padding errors (length should be multiples of 4)
        # fill up the signatureData and clientData with '=' to the correct
        # padding
        signatureData = signatureData + ("=" * (4 - (len(signatureData) % 4)))
        clientData = clientData + ("=" * (4 - (len(clientData) % 4)))
        signatureData = base64.urlsafe_b64decode(signatureData.encode("ascii"))
        clientData = base64.urlsafe_b64decode(clientData.encode("ascii"))

        # now check the otp for each challenge
        for ch in challenges:
            challenge = {}

            # we saved the 'real' challenge in the data
            data = ch.get("data", None)
            if data is not None:
                challenge["challenge"] = data.get("challenge")

            if challenge.get("challenge") is None:
                log.debug(
                    "could not checkOtp due to missing challenge in request: %r",
                    ch,
                )
                continue

            # prepare the applicationParameter and challengeParameter needed for
            # verification of the registration signature

            appId = self._get_app_id()
            applicationParameter = sha256(appId.encode("utf-8")).digest()
            challengeParameter = sha256(clientData).digest()
            publicKey = base64.urlsafe_b64decode(
                self.getFromTokenInfo("publicKey", None).encode("ascii")
            )

            # parse the received signatureData object
            (userPresenceByte, counter, signature) = self._parseSignatureData(
                signatureData
            )

            # verify the authentication signature
            if not self._validateAuthenticationSignature(
                applicationParameter,
                userPresenceByte,
                counter,
                challengeParameter,
                publicKey,
                signature,
            ):
                continue

            # check the received clientData object and retrieve the appId
            if not self._checkClientData(
                clientData, "authentication", challenge["challenge"]
            ):
                continue

            # the counter is interpreted as big-endian according to the U2F
            # specification
            counterInt = struct.unpack(">I", counter)[0]

            # verify that the counter value increased - prevent token device
            # cloning
            self._verifyCounterValue(counterInt)

            # U2F does not need an otp count
            ret = 0

        return ret

    def _parseRegistrationData(self, registrationData):
        """
        Parse U2F registration data according to FIDO U2F specification.

        Format:
        [1 byte] Reserved (must be 0x05)
        [65 bytes] User public key
        [1 byte] Key handle length
        [variable] Key handle
        [variable] X.509 certificate
        [variable] Signature

        :param registrationData: Raw registration data bytes
        :return: Tuple of (userPublicKey, keyHandle, cert, signature)
        :raises ValueError: If data format is invalid
        """
        offset = 0

        # Reserved byte (0x05)
        if len(registrationData) < 1 or registrationData[0] != 0x05:
            log.error("Wrong registration data format: Reserved byte does not match")
            msg = "Invalid reserved byte"
            raise ValueError(msg)
        offset += 1

        # User public key (65 bytes)
        USER_PUBLIC_KEY_LEN = 65
        if len(registrationData) < offset + USER_PUBLIC_KEY_LEN:
            log.error("Wrong registration data format: User public key is missing")
            msg = "Data too short for public key"
            raise ValueError(msg)
        userPublicKey = registrationData[offset : offset + USER_PUBLIC_KEY_LEN]
        offset += USER_PUBLIC_KEY_LEN

        # Key handle length and data
        if len(registrationData) < offset + 1:
            log.error("Wrong registration data format: Key handle length is missing")
            msg = "Data too short for key handle length"
            raise ValueError(msg)
        keyHandleLength = registrationData[offset]
        offset += 1

        if len(registrationData) < offset + keyHandleLength:
            log.error("Wrong registration data format: Key handle is missing")
            msg = "Data too short for key handle"
            raise ValueError(msg)
        keyHandle = registrationData[offset : offset + keyHandleLength]
        offset += keyHandleLength

        # Certificate (find ASN.1 SEQUENCE)
        cert_start = registrationData[offset:].find(b"\x30\x82")
        if cert_start == -1:
            log.error(
                "Wrong registration data format: Certificate start marker not found"
            )
            msg = "Certificate start marker not found"
            raise ValueError(msg)
        cert_start += offset

        # Get certificate length from ASN.1 length bytes
        cert_len = (registrationData[cert_start + 2] << 8) + registrationData[
            cert_start + 3
        ]
        # Add 4 for SEQUENCE tag and length bytes
        cert_end = cert_start + cert_len + 4

        if len(registrationData) < cert_end:
            log.error("Wrong registration data format: Certificate data is missing")
            msg = "Data too short for certificate"
            raise ValueError(msg)

        # Extract and parse certificate
        cert_data = registrationData[cert_start:cert_end]
        cert = x509.load_der_x509_certificate(cert_data, default_backend())

        # Remaining data is the signature
        signature = registrationData[cert_end:]
        if not signature:
            log.error("Wrong registration data format: No signature data found")
            msg = "No signature data found"
            raise ValueError(msg)

        return (userPublicKey, keyHandle, cert, signature)

    def _validateRegistrationSignature(
        self,
        applicationParameter,
        challengeParameter,
        keyHandle,
        userPublicKey,
        cert,
        signature,
    ):
        """
        Internal helper function to validate the registration signature received after parsing the
        token registration data according to the U2F specification

        :param applicationParameter: SHA-256 hash of the application identity.
        :param challengeParameter:   SHA-256 hash of the Client Data, a stringified JSON data
                                     structure prepared by the FIDO Client.
        :param keyHandle:            The key handle retrieved on parsing the registration data
        :param userPublicKey:        The user public key retrieved on parsing the registration data
        :param cert:                 X.509 certificate retrieved on parsing the registration data
        :param signature:            The signature to be verified as retrieved on parsing the
                                     registration data
        """

        # ------------------------------------------------------------------ --

        # compose the message from its parts

        message = (
            b"\x00"
            + applicationParameter
            + challengeParameter
            + keyHandle
            + userPublicKey
        )

        # ------------------------------------------------------------------ --

        # verify the attestation ECDSA signature

        pubkey = cert.public_key()

        try:
            pubkey.verify(signature, message, ec.ECDSA(hashes.SHA256()))

        except InvalidSignature as exx:
            log.info("Failed to verify signature %r", exx)
            msg = "Attestation signature is invalid"
            raise ValueError(msg) from exx

        except Exception as exx:
            log.error("Failed to verify signature %r", exx)
            raise

    def getInitDetail(self, params, user=None):
        """
        to complete the token normalisation, the response of the initialisation
        should be built by the token specific method, the getInitDetails
        """
        response_detail = {}

        info = self.getInfo()
        response_detail.update(info)
        response_detail["serial"] = self.getSerial()

        # get requested phase
        try:
            requested_phase = params["phase"]
        except KeyError as exx:
            msg = "Missing parameter: 'phase'"
            raise ParameterError(msg) from exx

        if requested_phase == "registration1":
            # We are in registration phase 1
            # We create a 32 bytes otp key (from urandom)
            # which is used as the registration challenge
            challenge = base64.urlsafe_b64encode(
                binascii.unhexlify(self._genOtpKey_(32))
            )
            self.addToTokenInfo("challenge", challenge.decode("ascii"))

            # save the appId to the TokenInfo
            # An appId passed as parameter is preferred over an appId defined
            # in a policy
            appId = ""
            if "appid" in params:
                appId = params.get("appid")
            else:
                # No appId passed as parameter - fall back to the policy
                # Get the appId as specified in the enrollment policy 'u2f_app_id'
                # for the specific realm
                # If the token has multiple realms, the appIds are checked for conflicts.
                # It could be discussed whether the token should use the appId of the default
                # realm, when the token is not attached to any realms
                realms = self.token.getRealmNames()
                for realm in realms:
                    get_policy_params = {
                        "action": "u2f_app_id",
                        "scope": "enrollment",
                        "realm": realm,
                    }
                    policies = getPolicy(get_policy_params)
                    policy_value = get_action_value(
                        policies,
                        scope="enrollment",
                        action="u2f_app_id",
                        default="",
                    )

                    # Check for appId conflicts
                    if appId and policy_value and appId != policy_value:
                        msg = "Conflicting appId values in u2f policies."
                        raise Exception(msg)
                    appId = policy_value

            if not appId:
                msg = "No appId defined."
                raise Exception(msg)
            self.addToTokenInfo("appId", appId)

            # create U2F RegisterRequest object and append it to the response
            # as 'message'
            appId = self._get_app_id()
            register_request = {
                "challenge": challenge.decode("ascii"),
                "version": "U2F_V2",
                "appId": appId,
            }
            response_detail["registerrequest"] = register_request

        elif requested_phase == "registration2":
            # We are in registration phase 2
            # process the data generated by the u2f compatible token device
            registerResponse = ""

            otpkey = None
            if "otpkey" in params:
                otpkey = params.get("otpkey")

            if otpkey is not None:
                # otpkey holds the JSON RegisterResponse object as specified by
                # the FIDO Alliance
                try:
                    registerResponse = json.loads(otpkey)
                except ValueError as exx:
                    msg = "Invalid JSON format"
                    raise Exception(msg) from exx

                self._handle_client_errors(registerResponse)

                try:
                    registrationData = registerResponse["registrationData"]
                    clientData = registerResponse["clientData"]
                except AttributeError as exx:
                    msg = "Couldn't find keyword in JSON object"
                    raise Exception(msg) from exx

                # registrationData and clientData are urlsafe base64 encoded
                # correct padding errors (length should be multiples of 4)
                # fill up the registrationData with '=' to the correct padding
                registrationData = registrationData + (
                    "=" * (4 - (len(registrationData) % 4))
                )
                clientData = clientData + ("=" * (4 - (len(clientData) % 4)))
                registrationData = base64.urlsafe_b64decode(
                    registrationData.encode("ascii")
                )
                clientData = base64.urlsafe_b64decode(clientData.encode("ascii"))

                # parse the raw registrationData according to the specification
                (
                    userPublicKey,
                    keyHandle,
                    x509cert,
                    signature,
                ) = self._parseRegistrationData(registrationData)

                # check the received clientData object
                if not self._checkClientData(
                    clientData,
                    "registration",
                    self.getFromTokenInfo("challenge", None),
                ):
                    msg = "Received invalid clientData object. Aborting..."
                    raise ValueError(msg)

                # prepare the applicationParameter and challengeParameter needed for
                # verification of the registration signature
                appId = self._get_app_id()
                applicationParameter = sha256(appId.encode("utf-8")).digest()
                challengeParameter = sha256(clientData).digest()

                # verify the registration signature
                self._validateRegistrationSignature(
                    applicationParameter,
                    challengeParameter,
                    keyHandle,
                    userPublicKey,
                    x509cert,
                    signature,
                )

                # save the key handle and the user public key in the Tokeninfo field for
                # future use
                self.addToTokenInfo(
                    "keyHandle",
                    base64.urlsafe_b64encode(keyHandle).decode("ascii"),
                )
                self.addToTokenInfo(
                    "publicKey",
                    base64.urlsafe_b64encode(userPublicKey).decode("ascii"),
                )
                self.addToTokenInfo("counter", "0")
                self.addToTokenInfo("phase", "authentication")
                # remove the registration challenge from the token info
                self.removeFromTokenInfo("challenge")
                # Activate the token
                self.token.LinOtpIsactive = True
            else:
                msg = "No otpkey set"
                raise ValueError(msg)
        else:
            msg = "Unsupported phase: %s"
            raise Exception(msg, requested_phase)

        return response_detail

    def getOfflineInfo(self):
        public_key = self.getFromTokenInfo("publicKey")
        key_handle = self.getFromTokenInfo("keyHandle")
        counter = self.getFromTokenInfo("counter")
        app_id = self.getFromTokenInfo("appId")

        return {
            "public_key": public_key,
            "key_handle": key_handle,
            "counter": counter,
            "app_id": app_id,
        }
