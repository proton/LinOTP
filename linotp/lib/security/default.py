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
"""default SecurityModules which takes the enc keys from a file"""

import binascii
import hmac
import logging
import os
from hashlib import sha256

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import pad, unpad

from linotp.lib.crypto.utils import zerome
from linotp.lib.security import SecurityModule

TOKEN_KEY = 0
CONFIG_KEY = 1
VALUE_KEY = 2
DEFAULT_KEY = 2


log = logging.getLogger(__name__)


class DefaultSecurityModule(SecurityModule):
    """
    the default security provider
    - provides the default implementation to all semantic security
      interface to all LinOTP operations
    """

    # Add schema for validating configuration in settings.py
    schema = {
        "type": "object",
        "properties": {
            "module": {"type": "string"},
            "tokenHandle": {"type": "number"},
            "configHandle": {"type": "number"},
            "valueHandle": {"type": "number"},
            "defaultHandle": {"type": "number"},
            "poolsize": {"type": "number"},
            "crypted": "FALSE",
        },
        "required": [
            "module",
            "tokenHandle",
            "configHandle",
            "configHandle",
            "valueHandle",
            "defaultHandle",
        ],
    }

    def __init__(self, config=None, add_conf=None):
        """
        initialsation of the security module

        :param config:  contains the configuration definition
        :type  config:  - dict -

        :return -
        """

        self.name = "Default"
        self.config = config
        self.crypted = False
        self.is_ready = True
        self._id = binascii.hexlify(os.urandom(3))

        if "crypted" in config:
            crypt = config.get("crypted").lower()
            if crypt == "true":
                self.crypted = True
                self.is_ready = False

        if "file" not in config:
            log.error(
                "[getSecret] no secret file defined. The SECRET_FILE "
                " parameter is missing in your linotp.cfg."
            )
            msg = "no secret file defined: linotpSecretFile!"
            raise Exception(msg)

        self.secFile = config.get("file")
        self.secrets = {}

    def isReady(self):
        """
        provides the status, if the security module is fully initializes
        this is required especially for the runtime confi like set password ++

        :return:  status, if the module is fully operational
        :rtype:   boolean

        """
        return self.is_ready

    def getSecret(self, id=0):
        """
        internal function, which acceses the key in the defined slot

        :param id: slot id of the key array
        :type  id: int - slotId

        :return: key or secret
        :rtype:  binary string

        """
        id = int(id)

        if self.crypted and id in self.secrets:
            return self.secrets.get(id)

        secret = ""
        try:
            with open(self.secFile, "rb") as f:
                for _i in range(id + 1):
                    secret = f.read(32)
            if not secret:
                # secret = setupKeyFile(secFile, id+1)
                msg = "No secret key defined for index: %r !\nPlease extend your %s !"
                raise Exception(
                    msg,
                    id,
                    self.secFile,
                )
        except Exception as exx:
            msg = f"Exception: {exx!r}"
            raise Exception(msg) from exx

        if self.crypted:
            self.secrets[id] = secret

        return secret

    def setup_module(self, params):
        """
        callback, which is called during the runtime to
        initialze the security module

        :param params: all parameters, which are provided by the http request
        :type  params: dict

        :return: -

        """
        if self.crypted is False:
            return
        if "password" not in params:
            msg = "missing password"
            raise Exception(msg)

        # if we have a crypted file and a password, we take all keys
        # from the file and put them in a hash
        # #
        # After this we do not require the password anymore

        handles = ["tokenHandle", "passHandle", "valueHandle", "defaultHandle"]
        for handle in handles:
            self.getSecret(self.config.get(handle, "0"))

        self.is_ready = True
        return

    # the real interfaces: random, encrypt, decrypt '''
    def random(self, len: int = 32) -> bytes:
        """
        security module methods: random

        :param len: length of the random byte array
        :type  len: int

        :return: random bytes
        :rtype:  byte string
        """

        return os.urandom(len)

    def encrypt(self, data: bytes, iv: bytes, id: int = DEFAULT_KEY) -> bytes:
        """
        security module methods: encrypt

        This module performs the following operations on
        the input data, which is a string:
            * convert data to hexidcimal representation
            * add termination string
            * pad with null to a multiple of 16 bytes
            * aes encrypt

        :param data: the to be encrypted data
        :type  data:byte string

        :param iv: initialisation vector (salt)
        :type  iv: random bytes

        :param  id: slot of the key array
        :type   id: int - slotid

        :return: encrypted data
        :rtype:  byte string
        """

        if self.is_ready is False:
            msg = "setup of security module incomplete"
            raise Exception(msg)

        key = self.getSecret(id)
        input_data = binascii.b2a_hex(data)
        input_data = self.padd_data(input_data)

        aes = AES.new(key, AES.MODE_CBC, iv)

        res = aes.encrypt(input_data)

        if self.crypted is False:
            zerome(key)
            del key
        return res

    def decrypt(self, value: bytes, iv: bytes, id: int = DEFAULT_KEY) -> bytes:
        """
        security module methods: decrypt

        :param data: the to be decrypted data
        :type  data:byte string

        :param iv: initialisation vector (salt)
        :type  iv: random bytes

        :param  id: slot of the key array
        :type   id: int

        :return: decrypted data
        :rtype:  byte string
        """

        if self.is_ready is False:
            msg = "setup of security module incomplete"
            raise Exception(msg)

        key = self.getSecret(id)
        aes = AES.new(key, AES.MODE_CBC, iv)
        output = aes.decrypt(value)

        data = self.unpadd_data(output)

        if self.crypted is False:
            zerome(key)
            del key

        return binascii.a2b_hex(data)

    @staticmethod
    def padd_data(input_data):
        """
        padd the given data to a blocksize of 16 according to pkcs7 padding

        :param input_data: the data, which should be padded
        :return: data with appended padding
        """
        return pad(data_to_pad=input_data, block_size=AES.block_size)

    @staticmethod
    def unpadd_data(input_data):
        """
        unpadd a given data from a blocksize of 16 according to pkcs7 padding

        :param input_data: the data with appended padding
        :return: stripped of data
        """
        return unpad(padded_data=input_data, block_size=AES.block_size)

    def decryptPassword(self, cryptPass: str) -> bytes:
        """
        dedicated security module methods: decryptPassword
        which used one slot id to decryt a string

        :param cryptPassword: the crypted password -
                              leading iv, seperated by the ':'
        :type cryptPassword: byte string

        :return: decrypted data
        :rtype:  byte string
        """
        return self._decryptValue(cryptPass, CONFIG_KEY)

    def decryptPin(self, cryptPin: str) -> bytes:
        """
        dedicated security module methods: decryptPin
        which used one slot id to decryt a string

        :param cryptPin: the crypted pin - - leading iv, seperated by the ':'
        :type cryptPin: byte string

        :return: decrypted data
        :rtype:  byte string
        """

        return self._decryptValue(cryptPin, TOKEN_KEY)

    def encryptPassword(self, cryptPass: bytes) -> str:
        """
        dedicated security module methods: encryptPassword
        which used one slot id to encrypt a string

        :param password: the to be encrypted password
        :type password: byte string

        :return: encrypted data - leading iv, seperated by the ':'
        :rtype:  byte string
        """
        return self._encryptValue(cryptPass, CONFIG_KEY)

    def encryptPin(self, cryptPin: bytes, iv: bytes | None = None) -> str:
        """
        dedicated security module methods: encryptPin
        which used one slot id to encrypt a string

        :param pin: the to be encrypted pin
        :type pin: byte string

        :param iv: initialisation vector (optional)
        :type iv: buffer (20 bytes random)

        :return: encrypted data - leading iv, seperated by the ':'
        :rtype:  byte string
        """
        return self._encryptValue(cryptPin, TOKEN_KEY, iv=iv)

    # base methods for pin and password
    def _encryptValue(self, value: bytes, keyNum, iv: bytes | None = None):
        """
        _encryptValue - base method to encrypt a value
        - uses one slot id to encrypt a string
        retrurns as string with leading iv, seperated by ':'

        :param value: the to be encrypted value
        :type value: byte string

        :param  keyNum: slot of the key array
        :type   keyNum: int

        :param iv: initialisation vector (optional)
        :type iv: buffer (20 bytes random)

        :return: encrypted data with leading iv and sepeartor ':'
        :rtype:  byte string
        """
        if not iv:
            iv = self.random(16)
        v = self.encrypt(value, iv, keyNum)

        value = iv.hex() + ":" + v.hex()
        return value

    def _decryptValue(self, cryptValue, keyNum):
        """
        _decryptValue - base method to decrypt a value
        - used one slot id to encrypt a string with
          leading iv, seperated by ':'

        :param cryptValue: the to be encrypted value
        :type cryptValue: byte string

        :param  keyNum: slot of the key array
        :type   keyNum: int

        :return: decrypted data
        :rtype:  byte string
        """
        # split at ":"
        pos = cryptValue.find(":")
        bIV = cryptValue[:pos]
        bData = cryptValue[pos + 1 : len(cryptValue)]

        iv = binascii.unhexlify(bIV)
        data = binascii.unhexlify(bData)

        password = self.decrypt(data, iv, keyNum)

        return password

    def signMessage(self, message, method=sha256, slot_id=DEFAULT_KEY):
        """
        create the hex mac for the message -

        :param message: the original message
        :param method: the hash method - we use by default sha256
        :param slot_id: which key should be used

        :return: hex mac
        """

        sign_key = None

        try:
            sign_key = self.getSecret(slot_id)
            hex_mac = hmac.new(sign_key, message.encode("utf-8"), method).hexdigest()
        finally:
            if sign_key:
                zerome(sign_key)
                del sign_key

        return hex_mac

    def verfiyMessageSignature(
        self, message, hex_mac, method=sha256, slot_id=DEFAULT_KEY
    ):
        """
        verify the hex mac is same for the message -
           the comparison is done in a constant time comparison

        :param message: the original message
        :param hex_mac: the to compared mac in hex
        :param method: the hash method - we use by default sha256
        :param slot_id: which key should be used

        :return: boolean
        """
        sign_key = None
        result = True

        try:
            sign_key = self.getSecret(slot_id)
            hmac_obj = hmac.new(sign_key, message.encode("utf-8"), method)
            sign_mac = hmac.new(sign_key, message.encode("utf-8"), method).hexdigest()

            res = 0
            # as we compare on hex, we have to multiply by 2
            digest_size = hmac_obj.digest_size * 2

            for x, y in zip(hex_mac, sign_mac, strict=True):
                res |= ord(x) ^ ord(y)

            if len(sign_mac) != digest_size:
                result = False

            if res:
                result = False

        except ValueError as err:
            log.error("Signature check: Mac Comparison failed! %r", err)

        except Exception as exx:
            log.error("Signature check: Unknown exception happened %r", exx)

        finally:
            if sign_key:
                zerome(sign_key)
                del sign_key

        return result

    def hmac_digest(self, bkey, data_input, hash_algo):
        """
        simple hmac with implicit digest

        :param bkey: the private shared secret
        :param data_input: the data
        :param hash_algo: one of the hashing algorithms
        """

        digest = hmac.new(bkey, data_input, hash_algo).digest()

        return digest

    def hash_digest(self, val, seed, hash_algo=None):
        """
        simple hash with implicit digest
        :param val: val - data part1
        :param seed: seed - data part2
        :param hash_algo: hashing function pointer
        """
        log.debug("hash_digest()")

        hash_obj = hash_algo()
        hash_obj.update(val)
        hash_obj.update(seed)

        return hash_obj.digest()
