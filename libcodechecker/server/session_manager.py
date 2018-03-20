# -------------------------------------------------------------------------
#                     The CodeChecker Infrastructure
#   This file is distributed under the University of Illinois Open Source
#   License. See LICENSE.TXT for details.
# -------------------------------------------------------------------------
"""
Handles the management of authentication sessions on the server's side.
"""

from datetime import datetime
import hashlib
import os
import uuid

from libcodechecker.logger import get_logger
from libcodechecker.util import check_file_owner_rw
from libcodechecker.util import load_json_or_empty
from libcodechecker.version import SESSION_COOKIE_NAME as _SCN

from database.config_db_model import Session as SessionRecord, SystemPermission

UNSUPPORTED_METHODS = []

try:
    from libcodechecker.libauth import cc_ldap
except ImportError:
    UNSUPPORTED_METHODS.append('ldap')

try:
    from libcodechecker.libauth import cc_pam
except ImportError:
    UNSUPPORTED_METHODS.append('pam')


LOG = get_logger("server")
SESSION_COOKIE_NAME = _SCN


class _Session(object):
    """A session for an authenticated, privileged client connection."""

    def __init__(self, token, username, groups,
                 session_lifetime, is_root=False, database=None,
                 last_access=None):

        self.token = token
        self.user = username
        self.groups = groups

        self.__session_lifetime = session_lifetime
        self.__root = is_root
        self.__database = database
        self.last_access = last_access if last_access else datetime.now()

    @property
    def is_root(self):
        """Returns whether or not the Session was created with the master
        superuser (root) credentials."""
        return self.__root

    @property
    def is_alive(self):
        """
        Returns if the session is alive and usable, that is, within its
        lifetime.
        """
        return (datetime.now() - self.last_access).total_seconds() <= \
            self.__session_lifetime

    def revalidate(self):
        """
        A session is only revalidated if it has yet to exceed its
        lifetime. After a session hasn't been used for this interval,
        it can NOT be resurrected at all --- the user needs to log in
        to a brand-new session.
        """

        if not self.is_alive:
            return

        self.last_access = datetime.now()

        if self.__database:
            # Update the timestamp in the database for the session's last
            # access.
            try:
                transaction = self.__database()
                record = transaction.query(SessionRecord) \
                    .filter(SessionRecord.token == self.token) \
                    .limit(1).one_or_none()

                if record:
                    record.last_access = self.last_access
                    transaction.commit()
            except Exception as e:
                LOG.error("Couldn't update usage timestamp of {0}"
                          .format(self.token))
                LOG.error(str(e))
            finally:
                transaction.close()


class SessionManager:
    """
    Provides the functionality required to handle user authentication on a
    CodeChecker server.
    """

    def __init__(self, configuration_file, session_salt,
                 root_sha, force_auth=False):
        """
        Initialise a new Session Manager on the server.

        :param configuration_file: The configuration file to read
            authentication backends from.
        :param session_salt: An initial salt that will be used in hashing
            the session to the database.
        :param root_sha: The SHA-256 hash of the root user's authentication.
        :param force_auth: If True, the manager will be enabled even if the
            configuration file disables authentication.
        """
        self.__database_connection = None
        self.__logins_since_prune = 0
        self.__sessions = []
        self.__session_salt = hashlib.sha1(session_salt).hexdigest()

        LOG.debug(configuration_file)
        scfg_dict = load_json_or_empty(configuration_file, {},
                                       'server configuration')
        if scfg_dict != {}:
            check_file_owner_rw(configuration_file)
        else:
            # If the configuration dict is empty, it means a JSON couldn't
            # have been parsed from it.
            raise ValueError("Server configuration file was invalid, or "
                             "empty.")

        # FIXME: Refactor this. This is irrelevant to authentication config,
        # so it should NOT be handled by session_manager. A separate config
        # handler for the server's stuff should be created, that can properly
        # instantiate SessionManager with the found configuration.
        self.__max_run_count = scfg_dict['max_run_count'] \
            if 'max_run_count' in scfg_dict else None

        self.__auth_config = scfg_dict['authentication']

        if force_auth:
            LOG.debug("Authentication was force-enabled.")
            self.__auth_config['enabled'] = True

        if 'soft_expire' in self.__auth_config:
            LOG.debug("Found deprecated argument 'soft_expire' in "
                      "server_config.authentication.")

        # Save the root SHA into the configuration (but only in memory!)
        self.__auth_config['method_root'] = root_sha

        # If no methods are configured as enabled, disable authentication.
        if scfg_dict['authentication'].get('enabled'):
            found_auth_method = False

            if 'method_dictionary' in self.__auth_config and \
                    self.__auth_config['method_dictionary'].get('enabled'):
                found_auth_method = True

            if 'method_ldap' in self.__auth_config and \
                    self.__auth_config['method_ldap'].get('enabled'):
                if 'ldap' not in UNSUPPORTED_METHODS:
                    found_auth_method = True
                else:
                    LOG.warning("LDAP authentication was enabled but "
                                "prerequisites are NOT installed on the system"
                                "... Disabling LDAP authentication.")
                    self.__auth_config['method_ldap']['enabled'] = False

            if 'method_pam' in self.__auth_config and \
                    self.__auth_config['method_pam'].get('enabled'):
                if 'pam' not in UNSUPPORTED_METHODS:
                    found_auth_method = True
                else:
                    LOG.warning("PAM authentication was enabled but "
                                "prerequisites are NOT installed on the system"
                                "... Disabling PAM authentication.")
                    self.__auth_config['method_pam']['enabled'] = False

            if not found_auth_method:
                if force_auth:
                    LOG.warning("Authentication was manually enabled, but no "
                                "valid authentication backends are "
                                "configured... The server will only allow "
                                "the master superuser (root) access.")
                else:
                    LOG.warning("Authentication is enabled but no valid "
                                "authentication backends are configured... "
                                "Falling back to no authentication.")
                    self.__auth_config['enabled'] = False

    @property
    def is_enabled(self):
        return self.__auth_config.get('enabled')

    def get_realm(self):
        return {
            "realm": self.__auth_config.get('realm_name'),
            "error": self.__auth_config.get('realm_error')
        }

    def set_database_connection(self, connection):
        """
        Set the instance's database connection to use in fetching
        database-stored sessions to the given connection.

        Use None as connection's value to unset the database.
        """
        self.__database_connection = connection

    def __handle_validation(self, auth_string):
        """
        Validate an oncoming authorization request
        against some authority controller.

        Returns False if no validation was done, or a validation object
        if the user was successfully authenticated.

        This validation object contains two keys: username and groups.
        """
        validation = self.__try_auth_dictionary(auth_string)
        if validation:
            return validation

        validation = self.__try_auth_pam(auth_string)
        if validation:
            return validation

        validation = self.__try_auth_ldap(auth_string)
        if validation:
            return validation

        return False

    def __is_method_enabled(self, method):
        return method not in UNSUPPORTED_METHODS and \
            'method_' + method in self.__auth_config and \
            self.__auth_config['method_' + method].get('enabled')

    def __try_auth_root(self, auth_string):
        """
        Try to authenticate the user against the root username:password's hash.
        """
        if 'method_root' in self.__auth_config and \
                hashlib.sha256(auth_string).hexdigest() == \
                self.__auth_config['method_root']:
            return {
                'username': SessionManager.get_user_name(auth_string),
                'groups': [],
                'root': True
            }

        return False

    def __try_auth_dictionary(self, auth_string):
        """
        Try to authenticate the user against the hardcoded credential list.

        Returns a validation object if successful, which contains the users'
        groups.
        """
        method_config = self.__auth_config.get('method_dictionary')
        if not method_config:
            return False

        valid = self.__is_method_enabled('dictionary') and \
            auth_string in method_config.get('auths')
        if not valid:
            return False

        username = SessionManager.get_user_name(auth_string)
        group_list = method_config['groups'][username] if \
            'groups' in method_config and \
            username in method_config['groups'] else []

        return {
            'username': username,
            'groups': group_list
        }

    def __try_auth_pam(self, auth_string):
        """
        Try to authenticate user based on the PAM configuration.
        """
        if self.__is_method_enabled('pam'):
            username, password = auth_string.split(':')
            if cc_pam.auth_user(self.__auth_config['method_pam'],
                                username, password):
                # PAM does not hold a group membership list we can reliably
                # query.
                return {'username': username}

        return False

    def __try_auth_ldap(self, auth_string):
        """
        Try to authenticate user to all the configured authorities.
        """
        if self.__is_method_enabled('ldap'):
            username, password = auth_string.split(':')

            ldap_authorities = self.__auth_config['method_ldap'] \
                .get('authorities')
            for ldap_conf in ldap_authorities:
                if cc_ldap.auth_user(ldap_conf, username, password):
                    groups = cc_ldap.get_groups(ldap_conf, username, password)
                    return {'username': username, 'groups': groups}

        return False

    @staticmethod
    def get_user_name(auth_string):
        return auth_string.split(':')[0]

    def get_db_session_tokens(self, user_name):
        """
        Get session token from the database for the given user.
        """
        if not self.__database_connection:
            return None

        try:
            # Try the database, if it is connected.
            transaction = self.__database_connection()
            session_tokens = transaction.query(SessionRecord.token) \
                .filter(SessionRecord.user_name == user_name) \
                .all()
            return [s[0] for s in session_tokens]
        except Exception as e:
            LOG.error("Couldn't check login in the database: ")
            LOG.error(str(e))
        finally:
            if transaction:
                transaction.close()

        return None

    @staticmethod
    def generate_session_token():
        """
        Returns a random session token.
        """
        return uuid.UUID(bytes=os.urandom(16)).__str__().replace('-', '')

    def create_or_get_session(self, auth_string):
        """
        Creates a new session for the given auth-string.

        If an existing, valid session is found for the auth_string, that will
        be used instead. Otherwise, a brand new token cookie will be generated.
        """
        if not self.__auth_config['enabled']:
            return None

        # Perform cleanup of session memory, if neccessary.
        self.__logins_since_prune += 1
        if self.__logins_since_prune >= \
                self.__auth_config['logins_until_cleanup']:
            self.__cleanup_sessions()

        # Checks that user can be authenticated as a 'root' user.
        validation = self.__try_auth_root(auth_string)
        if not validation:
            # Try to authenticate user with different authentication methods.
            validation = self.__handle_validation(auth_string)

            if not validation:
                return False

        # If the session is still valid and credentials are present,
        # return old token. This is fetched either locally or from the db.
        user_name = validation['username']
        tokens = self.get_db_session_tokens(user_name)
        token = tokens[0] if len(tokens) else self.generate_session_token()

        local_session = self.get_session(token)

        if local_session:
            local_session.revalidate()
        else:
            groups = validation.get('groups', [])
            is_root = validation.get('root', False)

            local_session = _Session(
                token, user_name, groups,
                self.__auth_config['session_lifetime'],
                is_root, self.__database_connection)
            self.__sessions.append(local_session)

            if self.__database_connection:
                try:
                    transaction = self.__database_connection()
                    record = SessionRecord(token, user_name,
                                           ';'.join(groups))
                    transaction.add(record)
                    transaction.commit()
                except Exception as e:
                    LOG.error("Couldn't store or update login record in "
                              "database:")
                    LOG.error(str(e))
                finally:
                    if transaction:
                        transaction.close()

        return local_session

    def get_max_run_count(self):
        """
        Returns the maximum storable run count. If the value is None it means
        we can upload unlimited number of runs.
        """
        return self.__max_run_count

    def _get_local_session_from_db(self, token):
        """
        Creates a local session if a valid session token can be found in the
        database.
        """

        if not self.__database_connection:
            return

        try:
            transaction = self.__database_connection()
            db_record = transaction.query(SessionRecord) \
                .filter(SessionRecord.token == token) \
                .limit(1).one_or_none()

            if db_record:
                user_name = db_record.user_name
                system_permission = transaction.query(SystemPermission) \
                    .filter(SystemPermission.name == user_name) \
                    .limit(1).one_or_none()

                is_root = True if system_permission else False
                local_session = _Session(
                    token, user_name,
                    db_record.groups.split(';'),
                    self.__auth_config['session_lifetime'],
                    is_root, self.__database_connection,
                    db_record.last_access)

                return local_session
        except Exception as e:
            LOG.error("Couldn't check login in the database: ")
            LOG.error(str(e))
        finally:
            if transaction:
                transaction.close()

    def get_session(self, token):
        """
        Retrieves the session for the given session cookie token from the
        server's memory backend, if such session exists or creates and returns
        a new one if the token exists in the database.

        :returns: The session object if one was found. None if authentication
        is not enabled, or if the cookie is not valid anymore.
        """

        if not self.is_enabled:
            return None

        for sess in self.__sessions:
            if sess.is_alive and sess.token == token:
                return sess

        # Try to get a local session from the database.
        local_session = self._get_local_session_from_db(token)
        if local_session and local_session.is_alive:
            self.__sessions.append(local_session)
        else:
            self.invalidate(token)

        return local_session

    def invalidate(self, token):
        """
        Remove a user's previous session from the store.
        """
        try:
            transaction = self.__database_connection() \
                if self.__database_connection else None

            # Remove local sessions.
            for session in self.__sessions[:]:
                if session.token == token:
                    self.__sessions.remove(session)

            # Remove sessions from the database.
            if transaction:
                transaction.query(SessionRecord) \
                    .filter(SessionRecord.token == token) \
                    .delete()
                transaction.commit()

            return True
        except Exception as e:
            LOG.error("Couldn't invalidate session for token {0}"
                      .format(token))
            LOG.error(str(e))
        finally:
            if transaction:
                transaction.close()

        return False

    def __cleanup_sessions(self):
        tokens = [s.token for s in self.__sessions
                  if not s.is_alive]
        self.__logins_since_prune = 0

        for token in tokens:
            self.invalidate(token)
