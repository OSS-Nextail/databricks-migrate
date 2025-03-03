from dbclient import *
import logging_utils
import logging
import os
import json
import wmconstants
import concurrent
from concurrent.futures import ThreadPoolExecutor
from thread_safe_writer import ThreadSafeWriter
from threading_utils import propagate_exceptions

class ScimClient(dbclient):
    def __init__(self, configs, checkpoint_service):
        super().__init__(configs)
        self._checkpoint_service = checkpoint_service
        self.groups_to_keep = configs.get("groups_to_keep", False)

    def get_active_users(self):
        users = self.get('/preview/scim/v2/Users').get('Resources', None)
        return users if users else None

    def log_all_users(self, log_file='users.log'):
        user_log = self.get_export_dir() + log_file
        users = self.get('/preview/scim/v2/Users').get('Resources', None)
        if users:
            with open(user_log, "w") as fp:
                for x in users:
                    fullname = x.get('name', None)

                    # if a group list has been passed, check to see if current user is part of groups
                    if self.groups_to_keep:
                        user_groups = [g['display'] for g in x.get('groups')]
                        if not set(user_groups).intersection(set(self.groups_to_keep)):
                            continue

                    if fullname:
                        given_name = fullname.get('givenName', None)
                        # if user is an admin, skip this user entry
                        if x['userName'] == 'admin' and given_name == 'Administrator':
                            continue
                    fp.write(json.dumps(x) + '\n')
        else:
            logging.info("Users returned an empty object")

    def log_all_service_principals(self, log_file='service_principals.log'):
        sp_log = self.get_export_dir() + log_file
        service_principals = self.get('/preview/scim/v2/ServicePrincipals').get('Resources', None)
        if service_principals:
            with open(sp_log, "w") as fp:
                for x in service_principals:
                    if x["active"] == True:
                        fp.write(json.dumps(x) + '\n')
                    else:
                        logging.info(f"Skipping inactive service principal {x['applicationId']} - {x['displayName']}")
        else:
            logging.info("ServicePrincipals returned an empty object")

    def log_single_user(self, user_email, log_file='single_user.log'):
        single_user_log = self.get_export_dir() + log_file
        users = self.get_active_users()
        found_user = False
        for user in users:
            current_email = user['emails'][0]['value']
            if user_email == current_email:
                found_user = True
                logging.info(user)
                with open(single_user_log, 'w') as fp:
                    fp.write(json.dumps(user) + '\n')
        if not found_user:
            logging.error("User not found. Emails are case sensitive. Please verify email address")

    def import_single_user(self, user_email, log_file='single_user.log', num_parallel=4):
        single_user_log = self.get_export_dir() + log_file
        checkpoint_users_set = self._checkpoint_service.get_checkpoint_key_set(
            wmconstants.WM_IMPORT, wmconstants.USER_OBJECT)
        resp = self.import_users(single_user_log, logging.getLogger(), checkpoint_users_set, num_parallel)

    def get_users_from_log(self, users_log='users.log'):
        """
        fetch a list of user names from the users log file
        meant to be used during group exports where the user list is a subset of users
        :param users_log:
        :return: a list of usernames that help identify their workspace paths
        """
        user_logfile = self.get_export_dir() + users_log
        username_list = []
        with open(user_logfile, 'r') as fp:
            for u in fp:
                user_json = json.loads(u)
                username_list.append(user_json.get('userName'))
        return username_list

    @staticmethod
    def is_member_a_user(member_json):
        if 'Users/' in member_json['$ref']:
            return True
        return False

    @staticmethod
    def is_member_a_group(member_json):
        if 'Groups/' in member_json['$ref']:
            return True
        return False

    @staticmethod
    def is_member_a_service_principal(member_json):
        if 'ServicePrincipals/' in member_json['$ref']:
            return True
        return False

    @staticmethod
    def are_service_principal_names_unique(service_principals_log_path):
        with open(service_principals_log_path, 'r') as fp:
            service_principals = {}
            for line in fp:
                sp = json.loads(line)
                name = sp['displayName']
                if name in service_principals:
                    return False
                service_principals[name] = sp
        return True

    def add_username_to_group(self, group_json):
        # add the userName field to json since ids across environments may not match
        members = group_json.get('members', [])
        new_members = []
        for m in members:
            m_id = m['value']
            if self.is_member_a_user(m):
                user_resp = self.get('/preview/scim/v2/Users/{0}'.format(m_id))
                m['userName'] = user_resp['userName']
                m['type'] = 'user'
            elif self.is_member_a_group(m):
                m['type'] = 'group'
            elif self.is_member_a_service_principal(m):
                m['type'] = 'service-principal'
            else:
                m['type'] = 'unknown'
            new_members.append(m)
        group_json['members'] = new_members
        return group_json

    def log_all_groups(self, group_log_dir='groups/'):
        group_dir = self.get_export_dir() + group_log_dir
        os.makedirs(group_dir, exist_ok=True)
        group_list = self.get("/preview/scim/v2/Groups").get('Resources', [])
        for x in group_list:
            group_name = x['displayName']

            # if groups_to_keep is defined, check to see if current group is a member
            if self.groups_to_keep:
                if group_name not in self.groups_to_keep:
                    continue

            with open(group_dir + group_name, "w") as fp:
                fp.write(json.dumps(self.add_username_to_group(x)))

    @staticmethod
    def build_group_dict(group_list):
        group_dict = {}
        for group in group_list:
            group_dict[group.get('displayName')] = group
        return group_dict

    def log_groups_from_list(self, group_name_list, group_log_dir='groups/', users_logfile='users.log'):
        """
        take a list of groups and log all the members
        :param group_name_list: a list obj of group names
        :param group_log_dir:
        :param users_logfile: logfile to store the user log data
        :return: return a list of userNames to export their notebooks for the next api call
        """
        group_dir = self.get_export_dir() + group_log_dir
        os.makedirs(group_dir, exist_ok=True)
        group_list = self.get("/preview/scim/v2/Groups").get('Resources', [])
        group_dict = self.build_group_dict(group_list)
        member_id_list = []
        for group_name in group_name_list:
            group_details = group_dict[group_name]
            members_list = group_details.get('members', [])
            filtered_users = list(filter(lambda y: 'Users' in y.get('$ref', None), members_list))
            filtered_sub_groups = list(filter(lambda y: 'Groups' in y.get('$ref', None), members_list))
            if filtered_sub_groups:
                sub_group_names = list(map(lambda z: z.get('display'), filtered_sub_groups))
                group_name_list.extend(sub_group_names)
            member_id_list.extend(list(map(lambda y: y['value'], filtered_users)))
            with open(group_dir + group_name, "w") as fp:
                group_details.pop('roles', None)  # removing the roles field from the groups arg
                fp.write(json.dumps(self.add_username_to_group(group_details)))
        users_log = self.get_export_dir() + users_logfile
        user_names_list = []
        with open(users_log, 'w') as u_fp:
            for mid in member_id_list:
                logging.info('Exporting', mid)
                api = f'/preview/scim/v2/Users/{mid}'
                user_resp = self.get(api)
                user_resp.pop('roles', None)  # remove roles since those can change during the migration
                user_resp.pop('http_status_code', None)  # remove unnecessary params
                user_names_list.append(user_resp.get('userName'))
                u_fp.write(json.dumps(user_resp) + '\n')
        return user_names_list

    def get_user_id_mapping(self):
        # return a dict of the userName to id mapping of the new env
        user_list = self.get('/preview/scim/v2/Users').get('Resources', None)
        if user_list:
            user_id_dict = {}
            for user in user_list:
                user_id_dict[user['userName']] = user['id']
            return user_id_dict
        return None

    @staticmethod
    def get_service_principal_id_mapping(export_dir, sp_mapping_logfile='service_principals_id_mapping.log'):
        # return a dict of the former service principal app mapping to the app id in the new env
        sp_app_id_dict = {}
        with open(export_dir + sp_mapping_logfile, 'r') as fp:
            for sp in fp:
                sp = json.loads(sp)
                sp_app_id_dict[sp['exported_id']] = sp['current_id']
        return sp_app_id_dict

    @staticmethod
    def get_service_principal_app_id_mapping(export_dir, sp_mapping_logfile='service_principals_id_mapping.log'):
        # return a dict of the former service principal app mapping to the app id in the new env
        sp_app_id_dict = {}
        with open(export_dir + sp_mapping_logfile, 'r') as fp:
            for sp in fp:
                sp = json.loads(sp)
                sp_app_id_dict[sp['exported_app_id']] = sp['current_app_id']
        return sp_app_id_dict

    @staticmethod
    def map_service_principals_in_acl(acl, service_principal_app_id_mapping, error_logger):
        for i, acl_entry in enumerate(acl):
            app_id = acl_entry.get('service_principal_name', None)
            if app_id:
                acl_entry['service_principal_name'] = service_principal_app_id_mapping.get(app_id, None)
                if not acl_entry['service_principal_name']:
                    error_logger.error(f"Service principal {app_id} not found in service principal mapping (not migrated?). Skipping ACL import.")
                    del acl[i]
        return acl

    def get_current_service_principals_by_name(self):
        # return a dict of the current service principal app mapping to the id and app id in the new env
        # raises an exception if there is a duplicate name
        sp_app_id_dict = {}
        sp_list = self.get('/preview/scim/v2/ServicePrincipals').get('Resources', {})
        for sp in sp_list:
            if sp['displayName'] in sp_app_id_dict:
                raise Exception(f"Duplicate service principal name {sp['displayName']} in destination workspace")
            sp_app_id_dict[sp['displayName']] = {'id': sp['id'], 'applicationId': sp['applicationId']}
        return sp_app_id_dict

    def get_old_user_emails(self, users_logfile='users.log'):
        # return a dictionary of { old_id : email } from the users log
        users_log = self.get_export_dir() + users_logfile
        email_dict = {}
        with open(users_log, 'r') as fp:
            for x in fp:
                user = json.loads(x)
                email_dict[user['id']] = user['userName']
        return email_dict

    @staticmethod
    def assign_roles_args(roles_list):
        # roles list passed from file, which is in proper patch arg format already
        # this method is used to patch the group IAM roles
        assign_args = {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                       "Operations": [{"op": "add",
                                       "path": "roles",
                                       "value": roles_list}]}
        return assign_args

    @staticmethod
    def assign_entitlements_args(entitlements_list):
        # roles list passed from file, which is in proper patch arg format already
        # this method is used to patch the group IAM roles
        assign_args = {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                       "Operations": [{"op": "add",
                                       "path": "entitlements",
                                       "value": entitlements_list}]}
        return assign_args

    def assign_group_entitlements(self, group_dir, error_logger):
        # assign group role ACLs, which are only available via SCIM apis
        group_ids = self.get_current_group_ids()
        if not os.path.exists(group_dir):
            logging.info("No groups defined. Skipping group entitlement assignment")
            return
        groups = self.listdir(group_dir)
        for group_name in groups:
            with open(group_dir + group_name, 'r') as fp:
                group_data = json.loads(fp.read())
                entitlements = group_data.get('entitlements', None)
                if entitlements:
                    g_id = group_ids[group_name]
                    update_entitlements = self.assign_entitlements_args(entitlements)
                    up_resp = self.patch(f'/preview/scim/v2/Groups/{g_id}', update_entitlements)
                    logging_utils.log_response_error(error_logger, up_resp)

    def assign_group_roles(self, group_dir, error_logger):
        # assign group role ACLs, which are only available via SCIM apis
        group_ids = self.get_current_group_ids()
        if not os.path.exists(group_dir):
            logging.info("No groups defined. Skipping group entitlement assignment")
            return
        groups = self.listdir(group_dir)
        for group_name in groups:
            with open(group_dir + group_name, 'r') as fp:
                group_data = json.loads(fp.read())
                roles = group_data.get('roles', None)
                if roles:
                    g_id = group_ids[group_name]
                    update_roles = self.assign_roles_args(roles)
                    up_resp = self.patch(f'/preview/scim/v2/Groups/{g_id}', update_roles)
                    logging_utils.log_response_error(error_logger, up_resp)
                entitlements = group_data.get('entitlements', None)
                if entitlements:
                    g_id = group_ids[group_name]
                    update_entitlements = self.assign_entitlements_args(entitlements)
                    up_resp = self.patch(f'/preview/scim/v2/Groups/{g_id}', update_entitlements)
                    logging_utils.log_response_error(error_logger, up_resp)

    def get_current_group_ids(self):
        # return a dict of group displayName and id mappings
        groups = self.get('/preview/scim/v2/Groups').get('Resources', None)
        group_ids = {}
        for group in groups:
            group_ids[group['displayName']] = group['id']
        return group_ids

    @staticmethod
    def add_roles_arg(roles_list):
        # this builds the args from a list of IAM roles. diff built from user logfile
        role_values = [{'value': x} for x in roles_list]
        patch_roles_arg = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "add",
                    "path": "roles",
                    "value": role_values
                }
            ]
        }
        return patch_roles_arg

    def assign_user_entitlements(self, current_user_ids, error_logger, user_log_file='users.log'):
        """
        assign user entitlements to allow cluster create, job create, sql analytics etc
        :param user_log_file:
        :param current_user_ids: dict of the userName to id mapping of the new env
        :return:
        """
        user_log = self.get_export_dir() + user_log_file
        if not os.path.exists(user_log):
            logging.info("Skipping user entitlement assignment. Logfile does not exist")
            return
        with open(user_log, 'r') as fp:
            # loop through each user in the file
            for line in fp:
                user = json.loads(line)
                userName = user['userName']
                if userName not in current_user_ids:
                    continue
                # add the users entitlements
                user_entitlements = user.get('entitlements', None)
                # get the current registered user id
                user_id = current_user_ids[user['userName']]
                if user_entitlements:
                    entitlements_args = self.assign_entitlements_args(user_entitlements)
                    update_resp = self.patch(f'/preview/scim/v2/Users/{user_id}', entitlements_args)
                    logging_utils.log_response_error(error_logger, update_resp)

    def assign_service_principal_entitlements(self, current_service_principal_ids, error_logger, service_principal_log_file='service_principals.log'):
        """
        assign service principal entitlements to allow cluster create, job create, sql analytics etc
        :param service_principal_log_file: exported service principal log file
        :param current_service_principal_app_ids: dict of the app id mapping to the new env
        """
        sp_log = self.get_export_dir() + service_principal_log_file
        if not os.path.exists(sp_log):
            logging.info("Skipping service principal entitlement assignment. Logfile does not exist")
            return
        with open(sp_log, 'r') as fp:
            # loop through each service principal in the file
            for line in fp:
                sp = json.loads(line)
                sp_id = sp['id']
                if sp_id not in current_service_principal_ids:
                    continue
                # add the service principal entitlements
                sp_entitlements = sp.get('entitlements', None)
                # get the current service principal app id
                new_id = current_service_principal_ids[sp_id]
                if sp_entitlements:
                    entitlements_args = self.assign_entitlements_args(sp_entitlements)
                    update_resp = self.patch(f'/preview/scim/v2/ServicePrincipals/{new_id}', entitlements_args)
                    logging_utils.log_response_error(error_logger, update_resp)

    def assign_user_roles(self, current_user_ids, error_logger, user_log_file='users.log'):
        """
        assign user roles that are missing after adding group assignment
        Note: There is a limitation in the exposed API. If a user is assigned a role permission & the permission
        is granted via a group, we can't distinguish the difference. Only group assignment will be migrated.
        :param user_log_file: logfile of all user properties
        :param current_user_ids: dict of the userName to id mapping of the new env
        :return:
        """
        user_log = self.get_export_dir() + user_log_file
        if not os.path.exists(user_log):
            logging.info("Skipping user entitlement assignment. Logfile does not exist")
            return
        # keys to filter from the user log to get the user / role mapping
        old_role_keys = ('userName', 'roles')
        cur_role_keys = ('schemas', 'userName', 'entitlements', 'roles', 'groups')
        # get current user id of the new environment, k,v = email, id
        with open(user_log, 'r') as fp:
            # loop through each user in the file
            for line in fp:
                user = json.loads(line)
                user_roles = {k: user[k] for k in old_role_keys if k in user}
                userName = user['userName']
                if userName not in current_user_ids:
                    continue
                # get the current registered user id
                user_id = current_user_ids[user['userName']]
                # get the current users settings
                cur_user = self.get('/preview/scim/v2/Users/{0}'.format(user_id))
                # get the current users IAM roles
                current_roles = cur_user.get('roles', None)
                if current_roles:
                    cur_role_values = set([x['value'] for x in current_roles])
                else:
                    cur_role_values = set()
                # get the users saved IAM roles from the export
                saved_roles = user_roles.get('roles', None)
                if saved_roles:
                    saved_role_values = set([y['value'] for y in saved_roles])
                else:
                    saved_role_values = set()
                roles_needed = list(saved_role_values - cur_role_values)
                if roles_needed:
                    # get the json to add the roles to the user profile
                    patch_roles = self.add_roles_arg(roles_needed)
                    update_resp = self.patch(f'/preview/scim/v2/Users/{user_id}', patch_roles)
                    logging_utils.log_response_error(error_logger, update_resp)

    def assign_service_principal_roles(self, current_service_principal_ids, error_logger, service_principal_log_file='service_principals.log'):
        """
        assign service principal roles that are missing after adding group assignment
        Note: There is a limitation in the exposed API. If a service principal is assigned a role permission & the permission
        is granted via a group, we can't distinguish the difference. Only group assignment will be migrated.
        :param service_principal_log_file: logfile of all service principal properties
        :param current_service_principal_ids: dict of the mapping from origin id to the id in the new env
        :return:
        """
        sp_log = self.get_export_dir() + service_principal_log_file
        if not os.path.exists(sp_log):
            logging.info("Skipping service principal entitlement assignment. Logfile does not exist")
            return
        with open(sp_log, 'r') as fp:
            for line in fp:
                sp = json.loads(line)
                if sp['id'] not in current_service_principal_ids:
                    continue
                sp_roles = sp.get("roles", {})
                role_values = set([y['value'] for y in sp_roles])
                cur_sp_id = current_service_principal_ids[sp['id']]
                cur_sp = self.get(f"/preview/scim/v2/ServicePrincipals/{cur_sp_id}")
                cur_roles = cur_sp.get('roles', {})
                cur_role_values = set([x['value'] for x in cur_roles])
                roles_needed = list(role_values - cur_role_values)
                if roles_needed:
                    # get the json to add the roles to the user profile
                    patch_roles = self.add_roles_arg(roles_needed)
                    update_resp = self.patch(f'/preview/scim/v2/ServicePrincipals/{cur_sp_id}', patch_roles)
                    logging_utils.log_response_error(error_logger, update_resp)

    @staticmethod
    def get_member_args(member_id_list):
        """
        helper function to form the json args to the patch request to update group memberships
        :param member_id_list: member ids to add to a specific group
        :return: dict args for the patch operation
        """
        member_id_list_json = []
        for m_id in member_id_list:
            member_id_list_json.append({'value': '{0}'.format(m_id)})

        add_members_args = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{
                "op": "add",
                "value": {"members": member_id_list_json}
                }
            ]
        }
        return add_members_args

    @staticmethod
    def is_user(member_json):
        # currently a workaround to get whether the member is a user or group
        # check the ref instead of the type field
        # once fixed, the type should be `user` or `group` in lowercase
        if 'Users/' in member_json['$ref']:
            return True
        return False

    @staticmethod
    def is_group(member_json):
        # currently a workaround to get whether the member is a user or group
        # check the ref instead of the type field
        # once fixed, the type should be `user` or `group` in lowercase
        if 'Groups/' in member_json['$ref']:
            return True
        return False

    def import_groups(self, group_dir, current_user_ids, error_logger):
        checkpoint_groups_set = self._checkpoint_service.get_checkpoint_key_set(
            wmconstants.WM_IMPORT, wmconstants.GROUP_OBJECT)
        # list all the groups and create groups first
        if not os.path.exists(group_dir):
            logging.info("No groups to import.")
            return
        groups = self.listdir(group_dir)
        for x in groups:
            if not checkpoint_groups_set.contains(x):
                logging.info('Creating group: {0}'.format(x))
                # set the create args displayName property aka group name
                create_args = {
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                    "displayName": x
                }
                group_resp = self.post('/preview/scim/v2/Groups', create_args)
                if not logging_utils.log_response_error(error_logger, group_resp):
                    checkpoint_groups_set.write(x)

        # dict of { group_name : group_id }
        groups = self.listdir(group_dir)
        current_group_ids = self.get_current_group_ids()
        current_service_principal_ids = self.get_service_principal_id_mapping(self.get_export_dir())
        # dict of { old_user_id : email }
        old_user_emails = self.get_old_user_emails()
        for group_name in groups:
            with open(group_dir + group_name, 'r') as fp:
                members = json.loads(fp.read()).get('members', None)
                logging.info(f"Importing group {group_name} :")
                if members:
                    # grab a list of ids to add either groups or users to this current group
                    member_id_list = []
                    for m in members:
                        if self.is_user(m):
                            try:
                                old_email = old_user_emails[m['value']]
                                this_user_id = current_user_ids.get(old_email, '')
                                if not this_user_id:
                                    error_logger.error(f'Unable to find user {old_email} in the new workspace. '
                                                     f'This users email case has changed and needs to be updated with '
                                                     f'the --replace-old-email and --update-new-email options')
                                member_id_list.append(this_user_id)
                            except KeyError:
                                error_logger.error(f"Error adding member {m} to group {group_name}")
                        elif self.is_group(m):
                            this_group_id = current_group_ids.get(m['display'])
                            member_id_list.append(this_group_id)
                        elif self.is_member_a_service_principal(m):
                            if m['value'] not in current_service_principal_ids:
                                error_logger.error(f"Service Principal {m['display']} ({m['value']}) has no mapping (not migrated) so it can't be added to group {group_name}")
                                continue
                            this_service_principal_id = current_service_principal_ids[m['value']]
                            member_id_list.append(this_service_principal_id)
                        else:
                            logging.info(
                                "Skipping other identities not within users/service_principal_users/groups")
                    add_members_json = self.get_member_args(member_id_list)
                    group_id = current_group_ids[group_name]
                    add_resp = self.patch('/preview/scim/v2/Groups/{0}'.format(group_id), add_members_json)
                    logging_utils.log_response_error(error_logger, add_resp)

    def import_users(self, user_log, error_logger, checkpoint_set, num_parallel):
        # first create the user identities with the required fields
        create_keys = ('emails', 'entitlements', 'displayName', 'name', 'userName')
        if not os.path.exists(user_log):
            logging.info("No users to import.")
            return
        with open(user_log, 'r') as fp:
            with ThreadPoolExecutor(max_workers=num_parallel) as executor:
                futures = [executor.submit(self._import_users_helper, user_data, create_keys, checkpoint_set, error_logger) for user_data in fp]
                concurrent.futures.wait(futures, return_when="FIRST_EXCEPTION")
                propagate_exceptions(futures)

        with open(self.get_export_dir() + "user_name_to_user_id.log", 'w') as fp:
            fp.write(json.dumps(self.get_user_id_mapping()))

    def _import_users_helper(self, user_data, create_keys, checkpoint_set, error_logger):
        user = json.loads(user_data)
        user_name = user['userName']
        if not checkpoint_set.contains(user_name):
            logging.info("Creating user: {0}".format(user_name))
            user_create = {k: user[k] for k in create_keys if k in user}
            create_resp = self.post('/preview/scim/v2/Users', user_create)
            if not logging_utils.log_response_error(error_logger, create_resp):
                checkpoint_set.write(user_name)

    def log_failed_users(self, current_user_ids, user_log, error_logger):
        with open(user_log, 'r') as fp:
            # loop through each user in the file
            for line in fp:
                user = json.loads(line)
                userName = user['userName']
                if userName not in current_user_ids:
                    error_logger.error(f"Failed to create user {user} in destination workspace \n")

    def import_service_principals(self, service_principal_log, error_logger, checkpoint_set, map_existing_by_name, num_parallel):
        # first create the service principal identities with the required fields
        create_keys = ('entitlements', 'displayName')
        sp_mapping_writer = ThreadSafeWriter(self.get_export_dir() + "service_principals_id_mapping.log", "w")
        try:
            if not os.path.exists(service_principal_log):
                logging.info("No service principals to import.")
                return
            if map_existing_by_name:
                if not self.are_service_principal_names_unique(service_principal_log):
                    msg = "Cannot map service principals by name if their names in origin are not unique"
                    error_logger.error(msg)
                    raise Exception(msg)
                existing_sps_by_name = self.get_current_service_principals_by_name()
            else:
                existing_sps_by_name = {}
                
            with open(service_principal_log, 'r') as fp:
                with ThreadPoolExecutor(max_workers=num_parallel) as executor:
                    futures = [executor.submit(self._import_service_principals_helper, sp_data, create_keys, sp_mapping_writer, checkpoint_set, error_logger, existing_sps_by_name) for sp_data in fp]
                    concurrent.futures.wait(futures, return_when="FIRST_EXCEPTION")
                    propagate_exceptions(futures)
        finally:
            sp_mapping_writer.close()

    def _import_service_principals_helper(self, sp_data, create_keys, sp_mapping_writer, checkpoint_set, error_logger, existing_sps_by_name):
        sp = json.loads(sp_data)
        display_name = sp['displayName']
        app_id = sp['applicationId']
        sp_id = sp['id']
        if not checkpoint_set.contains(app_id):
            if display_name in existing_sps_by_name:
                new_sp_id = existing_sps_by_name[display_name]['id']
                new_sp_app_id = existing_sps_by_name[display_name]['applicationId']
                logging.info(f"Skipping service principal creation: found existing service_principal {display_name} with id {new_sp_id}, app id {new_sp_app_id}")
            else:
                logging.info("Creating service_principal: {0} (app_id {1} id {2} in origin)".format(display_name, app_id, sp_id))
                sp_create = {k: sp[k] for k in create_keys if k in sp}
                create_resp = self.post('/preview/scim/v2/ServicePrincipals', sp_create)
                if logging_utils.log_response_error(error_logger, create_resp):
                    return
                new_sp_id = create_resp['id']
                new_sp_app_id = create_resp['applicationId']
            mapping = {"display_name": display_name, "exported_id": sp_id, "current_id": new_sp_id, "exported_app_id": app_id, "current_app_id": new_sp_app_id}
            sp_mapping_writer.write(json.dumps(mapping) + '\n')
            checkpoint_set.write(display_name)

    def log_failed_service_principals(self, current_service_principal_ids, sp_log, error_logger):
        with open(sp_log, 'r') as fp:
            # loop through each service principal in the file
            for line in fp:
                sp = json.loads(line)
                sp_id = sp['id']
                app_id = sp['applicationId']
                display_name = sp['displayName']
                if sp_id not in current_service_principal_ids:
                    error_logger.error(f"Failed to create service principal {app_id} - {display_name} in destination workspace \n")

    def import_all_users_service_principals_and_groups(self, user_log_file='users.log', service_principals_log_file='service_principals.log', group_log_dir='groups/'):
        self.import_all_users(user_log_file)
        self.import_all_service_principals(service_principals_log_file)
        self.import_all_groups(group_log_dir)

    def import_all_users(self, user_log_file='users.log', num_parallel=4):
        checkpoint_users_set = self._checkpoint_service.get_checkpoint_key_set(
            wmconstants.WM_IMPORT, wmconstants.USER_OBJECT)
        user_log = self.get_export_dir() + user_log_file
        user_error_logger = logging_utils.get_error_logger(
            wmconstants.WM_IMPORT, wmconstants.USER_OBJECT, self.get_export_dir())

        self.import_users(user_log, user_error_logger, checkpoint_users_set, num_parallel)
        current_user_ids = self.get_user_id_mapping()
        self.log_failed_users(current_user_ids, user_log, user_error_logger)
        # assign the users to IAM roles if on AWS
        if self.is_aws():
            logging.info("Update user role assignments")
            self.assign_user_roles(current_user_ids, user_error_logger, user_log_file)

        # need to separate role assignment and entitlements to support Azure
        logging.info("Updating users entitlements")
        self.assign_user_entitlements(current_user_ids, user_error_logger, user_log_file)

    def import_all_service_principals(self, service_principals_log_file='service_principals.log', map_existing_by_name=False, num_parallel=4):
        checkpoint_sp_set = self._checkpoint_service.get_checkpoint_key_set(
            wmconstants.WM_IMPORT, wmconstants.SERVICE_PRINCIPAL_OBJECT)
        sp_log = self.get_export_dir() + service_principals_log_file
        sp_error_logger = logging_utils.get_error_logger(
            wmconstants.WM_IMPORT, wmconstants.SERVICE_PRINCIPAL_OBJECT, self.get_export_dir())
        self.import_service_principals(sp_log, sp_error_logger, checkpoint_sp_set, map_existing_by_name, num_parallel)
        current_sp_ids = self.get_service_principal_id_mapping(self.get_export_dir())
        self.log_failed_service_principals(current_sp_ids, sp_log, sp_error_logger)

        # assign the service principals to IAM roles if on AWS
        if self.is_aws():
            logging.info("Update service principal role assignments")
            self.assign_service_principal_roles(current_sp_ids, sp_error_logger, service_principals_log_file)

        # need to separate role assignment and entitlements to support Azure
        logging.info("Updating service principal entitlements")
        self.assign_service_principal_entitlements(current_sp_ids, sp_error_logger, service_principals_log_file)

    def import_all_groups(self, group_log_dir='groups/'):
        group_error_logger = logging_utils.get_error_logger(
            wmconstants.WM_IMPORT, wmconstants.GROUP_OBJECT, self.get_export_dir())
        group_dir = self.get_export_dir() + group_log_dir
        current_user_ids = self.get_user_id_mapping()
        self.import_groups(group_dir, current_user_ids, group_error_logger)
        # assign the users to IAM roles if on AWS
        if self.is_aws():
            logging.info("Update group role assignments")
            self.assign_group_roles(group_dir, group_error_logger)

        # need to separate role assignment and entitlements to support Azure
        logging.info("Updating groups entitlements")
        self.assign_group_entitlements(group_dir, group_error_logger)
