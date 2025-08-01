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
* interface of the PushProvider
"""

import logging

log = logging.getLogger(__name__)


class IPushProvider:
    """
    An abstract class that has to be implemented by ever e-mail provider class
    """

    provider_type = "push"

    # set the default connection and request timeouts

    DEFAULT_TIMEOUT = (3, 5)

    def __init__(self):
        pass

    @staticmethod
    def getConfigMapping():
        """
        for dynamic, adaptive config entries we provide the abilty to
        have dedicated config entries

        entries should look like:
        {
          key: (ConfigName, ConfigType)
        }
        """
        config_mapping = {
            "timeout": ("Timeout", None),
            "config": ("Config", "encrypted_data"),
        }

        return config_mapping

    def push_notification(self, challenge, gda, transactionId):
        """
        Sends out the push notification message.

        :param challenge: The push notification message / challenge
        :param gda: alternative to the token_info, the gda could be provided
                    directly
        :param transactionId: The push notification transaction reference
        :return: A tuple of success and result message
        """
        msg = "Every subclass of IPushProvider has to implement this method."
        raise NotImplementedError(msg)

    def loadConfig(self, configDict):
        """
        Loads the configuration for this push notification provider

        :param configDict: A dictionary that contains all configuration entries
                          you defined (e.g. in a linotp.cfg file)
        """
