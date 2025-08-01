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

"""
validate controller - to check the authentication request
"""

import logging

from flask import current_app, g
from flask_babel import gettext as _

from linotp.controllers.base import BaseController
from linotp.lib import deprecated_methods
from linotp.lib.auth.validate import ValidationHandler
from linotp.lib.challenges import Challenges
from linotp.lib.config import getFromConfig
from linotp.lib.context import request_context
from linotp.lib.error import ParameterError, ValidateError
from linotp.lib.pairing import decrypt_pairing_response
from linotp.lib.policy import (
    AuthorizeException,
    check_auth_serial,
    check_auth_tokentype,
    check_user_authorization,
    get_realm_for_setrealm,
    is_auth_return,
)
from linotp.lib.realm import getDefaultRealm
from linotp.lib.reply import apply_detail_policies, sendQRImageResult, sendResult
from linotp.lib.token import get_token, get_token_owner, get_tokens
from linotp.lib.user import User, getUserId, getUserInfo
from linotp.model import db

CONTENT_TYPE_PAIRING = 1

log = logging.getLogger(__name__)


class ValidateController(BaseController):
    """
    The linotp.controllers are the implementation of the web-API to talk to the LinOTP server.
    The ValidateController is used to validate the username with its given OTP value.
    An Authentication module like pam_linotp2 or rlm_linotp2 uses this ValidateController.
    The functions of the ValidateController are invoked like this

        https://server/validate/<functionname>

    The functions are described below in more detail.
    """

    jwt_exempt = True  # Don't do JWT auth in this controller

    def __before__(self, *args, **kwargs):
        """
        __before__ is called before every action

        :param args: the arguments of the action
        :param kwargs: the keyword arguments of the action
        :return: None
        """
        user = request_context["RequestUser"]
        if user:
            # we need to overwrite the user.realm in case the
            # user does not exist in the original realm (setrealm-policy)
            realm_to_set = get_realm_for_setrealm(user.login, user.realm)
            if realm_to_set != user.realm:
                user.realm = realm_to_set
                request_context["RequestUser"] = user
                g.audit["realm"] = realm_to_set

            try:
                # fetch user info for details_on_success
                uid, resId, resIdC = getUserId(user)
                user_info = getUserInfo(uid, resId, resIdC)
                if user_info:
                    user.info = user_info
                request_context["RequestUser"] = user
            except Exception:
                pass

    @staticmethod
    def __after__(response):
        """
        __after__ is called after every action

        :param response: the previously created response - for modification
        :return: return the response
        """
        apply_detail_policies(response)
        current_app.audit_obj.log(g.audit)
        return response

    def _check(self, param):
        """
        basic check function, that can be used by different controllers

        :param param: dict of all caller parameters
        :type param: dict

        :return: Tuple of True or False and opt
        :rtype: Tuple(boolean, opt)

        """
        user = request_context["RequestUser"]
        # AUTHORIZATION Pre Check
        check_user_authorization(user.login, user.realm, exception=True)

        passw = param.get("pass")

        # Handle challenge verification if present
        challenge = param.get("challenge")
        if challenge:
            options = {"challenge": challenge}
        else:
            # Extract validation options from parameters
            excluded_params = {"pass", "user", "init"}
            options = {k: v for k, v in param.items() if k not in excluded_params}

        vh = ValidationHandler()
        (ok, opt) = vh.checkUserPass(user, passw, options=options)

        g.audit.update(request_context.get("audit", {}))
        g.audit["success"] = ok

        if ok:
            # AUTHORIZATION post check
            check_auth_tokentype(g.audit["serial"], exception=True, user=user)
            check_auth_serial(g.audit["serial"], exception=True, user=user)

        return (ok, opt)

    # @profile_decorator(log_file="/tmp/validate.prof")
    @deprecated_methods(["GET"])
    def check(self):
        """
        This function is used to validate the username and the otp value/password.

        :param user: The username or loginname
        :param pass: The password that consist of a possible fixed password component and the OTP value
        :param realm: (optional) The realm to be used to match the user to a useridresolver
        :param challenge: (optional) This param indicates, that this request is a challenge request.
        :param data: (optional) Data to use to generate a challenge
        :param state: (optional) A state id of an existing challenge to respond to
        :param transactionid: (optional): A transaction id of an existing challenge to respond to
        :param serial: (optional) Serial of a token to use instead of the matching tokens found for the given user and pass

        :return:
            JSON response::

                {
                    "version": "LinOTP 2.4",
                    "jsonrpc": "2.0",
                    "result": {
                        "status": true,
                        "value": false
                    },
                    "id": 0
                }

            If ``status`` is ``true`` the request was handled successfully.

            If ``value`` is ``true`` the user was authenticated successfully.

        :raises Exception:
            if an error occurs the status in the json response is set to false
        """

        param = self.request_params.copy()
        ok = False
        opt = None

        try:
            # prevent the detection if a user exist
            # by sending a request w.o. pass parameter
            try:
                (ok, opt) = self._check(param)
            except (AuthorizeException, ParameterError) as exx:
                log.warning("[check] authorization failed for validate/check: %r", exx)
                g.audit["success"] = False
                g.audit["info"] = str(exx)
                ok = False
                if is_auth_return(ok):
                    if opt is None:
                        opt = {}
                    opt["error"] = g.audit.get("info")

            db.session.commit()

            qr = param.get("qr", None)
            if qr and opt and "message" in opt:
                try:
                    dataobj = opt.get("message")
                    param["alt"] = f"{opt}"
                    if "transactionid" in opt:
                        param["transactionid"] = opt["transactionid"]
                    return sendQRImageResult(dataobj, param)
                except Exception as exc:
                    log.warning("failed to send QRImage: %r ", exc)
                    return sendQRImageResult(opt, param)
            else:
                return sendResult(ok, 0, opt=opt)

        except Exception as exx:
            log.error("[check] validate/check failed: %r", exx)
            # If an internal error occurs or the SMS gateway did not send the
            # SMS, we write this to the detail info.
            g.audit["info"] = f"{exx!r}"
            db.session.rollback()
            return sendResult(False, 0)

        finally:
            db.session.close()

    @deprecated_methods(["GET"])
    def check_status(self):
        """
        check the status of a transaction - for polling support

        :param transactionid: the transaction id of the challenge we want to check status for
        :param state: alternative key to transactionid
        :param user: (optional) the user the token belongs to (necessary if the challenge was triggered in a user context)
        :param serial: (optional) or the serial we are searching for instead of user
        :param pass: the pin or password for authorization of the request
        :param use_offline: (optional) on success, the offline info is returned (applicable to token types that use `support_offline` policy)

        :return:
            a json result with a boolean status and request result

        :raises Exception:
            if an error occurs an exception is serialized and returned
        """

        try:
            param = self.request_params

            #
            # we require either state or transactionid as parameter

            transid = param.get("state", param.get("transactionid"))
            if not transid:
                raise ParameterError(
                    _('Missing required parameter "state" or "transactionid"!')
                )

            # serial is an optional parameter
            serial = param.get("serial", None)

            user = request_context["RequestUser"]

            passw = param.get("pass")
            if passw is None:
                raise ParameterError(_('Missing required parameter "pass"!'))

            use_offline = "use_offline" in param

            va = ValidationHandler()
            ok, opt = va.check_status(
                transid=transid,
                user=user,
                serial=serial,
                password=passw,
                use_offline=use_offline,
            )

            serials = []
            types = []
            owner = None
            challenges = Challenges.lookup_challenges(transid=transid)

            for ch in challenges:
                tokens = get_tokens(serial=ch.getTokenSerial())

                for token in tokens:
                    serials.append(token.getSerial())
                    types.append(token.getType())

                    if not owner:
                        owner = get_token_owner(token)

            if owner:
                request_context["RequestUser"] = owner
                g.audit["user"] = g.audit["user"] or owner.login
                g.audit["realm"] = g.audit["realm"] or owner.realm

            g.audit["serial"] = " ".join(serials)
            g.audit["token_type"] = " ".join(types)
            request_context["TokenSerial"] = " ".join(serials)
            request_context["TokenType"] = " ".join(types)

            g.audit["success"] = ok
            g.audit["info"] = str(opt)

            db.session.commit()
            return sendResult(ok, 0, opt=opt)

        except Exception as exx:
            log.error("check_status failed: %r", exx)
            g.audit["info"] = str(exx)
            db.session.rollback()
            return sendResult(False, 0)

    @deprecated_methods(["GET"])
    def check_yubikey(self):
        """
        This function is used to validate the output of a yubikey

        :param pass: The password that consist of the static yubikey prefix and the otp

        :return:
            JSON response::

                {
                    "version": "LinOTP 2.4",
                    "jsonrpc": "2.0",
                    "result": {
                        "status": true,
                        "value": false
                    },
                    "detail" : {
                        "username": username,
                        "realm": realm
                    },
                    "id": 0
                }
        :raises Exception:
            if an error occurs status in the response is set to false
        """

        try:
            try:
                passw = self.request_params["pass"]
            except KeyError as exx:
                msg = "Missing parameter: 'pass'"
                raise ParameterError(msg) from exx

            ok = False
            try:
                vh = ValidationHandler()
                ok, opt = vh.checkYubikeyPass(passw)
                g.audit["success"] = ok

            except AuthorizeException as exx:
                log.warning(
                    "[check_yubikey] authorization failed for validate/check_yubikey: %r",
                    exx,
                )
                g.audit["success"] = False
                g.audit["info"] = str(exx)
                ok = False

            db.session.commit()
            return sendResult(ok, 0, opt=opt)

        except Exception as exx:
            log.error("[check_yubikey] validate/check_yubikey failed: %r", exx)
            g.audit["info"] = str(exx)
            db.session.rollback()
            return sendResult(False, 0)

    @deprecated_methods(["GET"])
    def samlcheck(self):
        """
        This function is used to validate the username and the otp value/password
        in a SAML environment. If ``linotp.allowSamlAttributes = True``
        then the attributes of the authenticated users are also contained
        in the response.


        :param user: username / loginname
        :param pass: the password that consists of a possible fixes password component and the OTP value
        :param realm: (optional) realm to match the user to a useridresolver

        :raises Exception:
            if an error occurs status in the response is set to false
        """

        try:
            opt = None
            param = self.request_params
            (ok, opt) = self._check(param)
            attributes = {}

            if ok is True:
                allowSAML = False
                try:
                    allowSAML = getFromConfig("allowSamlAttributes")
                except BaseException:
                    log.warning(
                        "[samlcheck] Calling controller samlcheck. But allowSamlAttributes is False."
                    )
                if allowSAML == "True":
                    # Now we get the attributes of the user
                    user = request_context["RequestUser"]
                    (uid, resId, resIdC) = getUserId(user)
                    userInfo = getUserInfo(uid, resId, resIdC)
                    log.debug(
                        "[samlcheck] getting attributes for: %s@%s",
                        user.login,
                        user.realm,
                    )

                    for key in [
                        "username",
                        "surname",
                        "mobile",
                        "phone",
                        "givenname",
                        "email",
                    ]:
                        attributes[key] = userInfo.get(key)

                    log.debug("[samlcheck] %r", attributes)

            db.session.commit()
            return sendResult({"auth": ok, "attributes": attributes}, 0, opt)

        except Exception as exx:
            log.error("[samlcheck] validate/check failed: %r", exx)
            db.session.rollback()
            return sendResult(False, 0)

    @deprecated_methods(["GET"])
    def check_t(self):
        """
        check a session by transaction / state

        :param pass:
        :param transactionid or serial:

        :return:
            a json result with a boolean status and request result

        :raises Exception:
            if an error occurs status in the response is set to false
        """

        param = self.request_params.copy()
        value = {}
        ok = False
        opt = {}

        try:
            if "pass" not in param:
                msg = "Missing parameter: 'pass'"
                raise ParameterError(msg)

            passw = param["pass"]

            transid = param.get("state", None)
            if transid is not None:
                param["transactionid"] = transid
                del param["state"]

            if transid is None:
                transid = param.get("transactionid", None)

            if transid is None:
                msg = "missing parameter: state or transactionid!"
                raise Exception(msg)

            vh = ValidationHandler()
            (ok, opt) = vh.check_by_transactionid(
                transid=transid, passw=passw, options=param
            )

            value["value"] = ok
            value["failcount"] = int(opt.get("failcount", 0))

            g.audit["success"] = ok
            db.session.commit()

            qr = param.get("qr", None)
            if qr and opt and "message" in opt:
                try:
                    dataobj = opt.get("message")
                    param["alt"] = f"{opt}"
                    if "transactionid" in opt:
                        param["transactionid"] = opt["transactionid"]
                    return sendQRImageResult(dataobj, param)
                except Exception as exc:
                    log.warning("failed to send QRImage: %r ", exc)
                    return sendQRImageResult(opt, param)
            else:
                return sendResult(value, 1, opt=opt)

        except Exception as exx:
            log.error("[check_t] validate/check_t failed: %r", exx)
            g.audit["info"] = str(exx)
            db.session.rollback()
            return sendResult(False, 0)

    # ------------------------------------------------------------------------ -
    @deprecated_methods(["GET"])
    def accept_transaction(self):
        """
        confirms a transaction.
        - needs the mandatory url query parameters:

        :param transactionid: unique id for the transaction
        :param signature: signature for the confirmation

        :return:
            a json result with a boolean status and request result

        :raises Exception:
            if an error occurs status in the response is set to false
        """

        try:
            param = self.request_params.copy()

            # -------------------------------------------------------------- --

            # check the parameters

            if "signature" not in param:
                msg = "Missing parameter: 'signature'!"
                raise ParameterError(msg)

            if "transactionid" not in param:
                msg = "Missing parameter: 'transactionid'!"
                raise ParameterError(msg)

            # -------------------------------------------------------------- --

            # start the processing

            passw = {"accept": param["signature"]}
            transid = param["transactionid"]

            vh = ValidationHandler()
            ok, _opt = vh.check_by_transactionid(
                transid=transid, passw=passw, options=param
            )

            # -------------------------------------------------------------- --

            # finish the result

            if "serial" in _opt:
                g.audit["serial"] = _opt["serial"]

            if "token_type" in _opt:
                g.audit["token_type"] = _opt["token_type"]

            g.audit["info"] = f"accept transaction: {ok!r}"

            g.audit["success"] = ok
            db.session.commit()

            return sendResult(ok)

        except Exception as exx:
            log.error("validate/accept_transaction failed: %r", exx)
            g.audit["info"] = f"{exx!r}"
            db.session.rollback()

            return sendResult(False, 0)

    # ------------------------------------------------------------------------ -
    @deprecated_methods(["GET"])
    def reject_transaction(self):
        """
        rejects a transaction.
        - needs the mandatory url query parameters:

        :param transactionid: unique id for the transaction
        :param signature: signature for the rejection

        :return:
            a json result with a boolean status and request result

        :raises Exception:
            if an error occurs status in the response is set to false
        """

        try:
            param = self.request_params.copy()

            # -------------------------------------------------------------- --

            # check the parameters

            if "signature" not in param:
                msg = "Missing parameter: 'signature'!"
                raise ParameterError(msg)

            if "transactionid" not in param:
                msg = "Missing parameter: 'transactionid'!"
                raise ParameterError(msg)

            # -------------------------------------------------------------- --

            # start the processing

            passw = {"reject": param["signature"]}
            transid = param["transactionid"]

            vh = ValidationHandler()
            ok, _opt = vh.check_by_transactionid(
                transid=transid, passw=passw, options=param
            )

            # -------------------------------------------------------------- --

            # finish the result

            if "serial" in _opt:
                g.audit["serial"] = _opt["serial"]

            if "token_type" in _opt:
                g.audit["token_type"] = _opt["token_type"]

            g.audit["info"] = f"reject transaction: {ok!r}"

            g.audit["success"] = ok
            db.session.commit()

            return sendResult(ok)

        except Exception as exx:
            log.error("validate/reject_transaction failed: %r", exx)
            g.audit["info"] = f"{exx!r}"
            db.session.rollback()

            return sendResult(False, 0)

    @deprecated_methods(["GET"])
    def check_s(self):
        """
        This function is used to validate the serial and the otp value/password.
        If the otppin policy is set, the endpoint /validate/check_s does not work.

        :param serial:  the serial number of the token
        :param pass:    the password that consists of a possible fixes password component
                        and the OTP value

        :return:
            a json result with a boolean status and request result

        :raises Exception:
            if an error occurs status in the response is set to false
        """
        param = self.request_params

        options = {
            k: v
            for k, v in param.items()
            if k not in ["user", "serial", "pass", "init"]
        }

        try:
            passw = param.get("pass")
            serial = param.get("serial")
            if serial is None:
                user = param.get("user")
                if user is not None:
                    user = request_context["RequestUser"]
                    toks = get_tokens(user=user)
                    if len(toks) == 0:
                        msg = "No token found!"
                        raise Exception(msg)
                    elif len(toks) > 1:
                        msg = "More than one token found!"
                        raise Exception(msg)
                    else:
                        tok = toks[0].token
                        desc = tok.get()
                        realms = desc.get("LinOtp.RealmNames")
                        if realms is None or len(realms) == 0:
                            realm = getDefaultRealm()
                        elif len(realms) > 0:
                            realm = realms[0]

                        userInfo = getUserInfo(
                            tok.LinOtpUserid,
                            tok.LinOtpIdResolver,
                            tok.LinOtpIdResClass,
                        )
                        user = User(login=userInfo.get("username"), realm=realm)

                        serial = tok.getSerial()

            g.audit["serial"] = serial

            options["scope"] = {"check_s": True}
            vh = ValidationHandler()
            (ok, opt) = vh.checkSerialPass(serial, passw, options=options)
            g.audit["success"] = ok
            db.session.commit()

            qr = param.get("qr", None)
            if qr and opt and "message" in opt:
                try:
                    dataobj = opt.get("message")
                    param["alt"] = f"{opt}"
                    if "transactionid" in opt:
                        param["transactionid"] = opt["transactionid"]
                    return sendQRImageResult(dataobj, param)
                except Exception as exc:
                    log.warning("failed to send QRImage: %r ", exc)
                    return sendQRImageResult(opt, param)
            else:
                return sendResult(ok, 0, opt=opt)

        except Exception as exx:
            log.error("[check_s] validate/check_s failed: %r", exx)
            g.audit["info"] = str(exx)
            db.session.rollback()
            return sendResult(False, id=0, status=False)

    @deprecated_methods(["GET"])
    def simplecheck(self):
        """
        This function is used to validate the username and the otp value/password.

        :param user:    username / loginname
        :param pass:    the password that consists of a possible fixes password component
                        and the OTP value
        :param realm:   additional realm to match the user to a useridresolver

        :return:
            Simple ascii response:

            :-)
                in case of success
            :-(
                in case of failed authentication
            :-/
                in case of any error
        """
        opt = None
        param = self.request_params
        res = []

        try:
            try:
                (ok, opt) = self._check(param)
            except AuthorizeException as exx:
                log.warning("[simplecheck] validate/simplecheck: %r", exx)
                g.audit["success"] = False
                g.audit["action_detail"] = str(exx)
                ok = False

            db.session.commit()

            ret = ":-)" if ok is True else ":-("
            res.append(ret)

            if opt is not None:
                if "state" in opt or "transactionid" in opt:
                    stat = opt.get("transactionid") or opt.get("state")
                    res.append(stat)

                if "data" in opt or "message" in opt:
                    msg = opt.get("data") or opt.get("message")
                    res.append(msg)

            return " ".join(res).strip()

        except Exception as exx:
            log.error("[simplecheck] failed: %r", exx)
            db.session.rollback()
            return ":-("

    def ok(self):
        """
        return a success response

        :return:
            a json result with a status True and request result True

        :raises Exception:
            if an error occurs status in the response is set to false
        """
        return sendResult(True, 0)

    def fail(self):
        """
        return a failed response

        :return:
            a json result with a status True and request result False

        :raises Exception:
            if an error occurs status in the response is set to false
        """
        return sendResult(False, 0)

    @deprecated_methods(["GET"])
    def smspin(self):
        """
        This function is used in conjunction with an SMS token:
        the user authenticates with user and pin (pass) and
        will receive on his mobile an OTP as message

        :param user:  username / loginname
        :param pass:  the password that consists of a possible fixed password
        :param realm: additional realm to match the user to a useridresolver

        :return:
            a json result with a boolean status and request result

        :raises Exception:
            if an error occurs status in the response is set to false

        """
        ret = False
        param = self.request_params
        state = ""
        message = "No sms message defined!"

        try:
            g.audit["success"] = 0

            (ret, opt) = self._check(param)

            # here we build some backward compatibility
            if isinstance(opt, dict):
                state = opt.get("state", "") or ""
                message = opt.get("message", "") or "No sms message defined!"

            # sucessfull submit
            if (
                message in ["sms with otp already submitted", "sms submitted"]
                and len(state) > 0
            ):
                ret = True
                g.audit["success"] = 1

            # sending sms failed should be an error
            elif message in ["sending sms failed"]:
                ret = True
                g.audit["success"] = 0

            # anything else is an exception
            else:
                raise Exception(message)

            db.session.commit()
            return sendResult(ret, opt)

        except Exception as exx:
            log.error("[smspin] validate/smspin failed: %r", exx)
            # If an internal error occurs or the SMS gateway did not send
            # the SMS, we write this to the detail info.
            g.audit["info"] = str(exx)
            db.session.rollback()
            return sendResult(False, 0)

    @deprecated_methods(["GET"])
    def pair(self):
        """
        for the enrollment of qr and push token

        :param pairing_response: the result from the token pairing request

        :return:
            a json result with a boolean status and request result

        :raises Exception:
            if an error occurs status in the response is set to false

        """

        try:
            # -------------------------------------------------------------- --

            enc_response = self.request_params.get("pairing_response")

            if enc_response is None:
                msg = "Parameter missing"
                raise Exception(msg)

            # -------------------------------------------------------------- --

            dec_response = decrypt_pairing_response(enc_response)
            token_type = dec_response.token_type
            pairing_data = dec_response.pairing_data

            if not hasattr(pairing_data, "serial") or pairing_data.serial is None:
                msg = (
                    "Pairing responses with no serial attached"
                    " are currently not implemented."
                )
                raise ValidateError(msg)

            # --------------------------------------------------------------- -

            # TODO: pairing policy
            token = get_token(pairing_data.serial)

            # prepare some audit entries
            t_owner = token.getUser()

            realms = token.getRealms()
            realm = ""
            if realms:
                realm = realms[0]

            g.audit["user"] = t_owner or ""
            g.audit["realm"] = realm

            # --------------------------------------------------------------- --

            if token.type != token_type:
                msg = "Serial in pairing response doesn't match supplied token_type"
                raise Exception(msg)

            # --------------------------------------------------------------- --

            token.pair(pairing_data)
            g.audit["success"] = 1
            g.audit["serial"] = token.getSerial()

            db.session.commit()
            return sendResult(False)

        # ------------------------------------------------------------------- --

        except Exception as exx:
            log.error("validate/pair failed: %r", exx)
            g.audit["info"] = str(exx)
            db.session.rollback()
            return sendResult(False, 0, status=False)


# eof #########################################################################
