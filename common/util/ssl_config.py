# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# ssl_config.py — SSL/TLS certificate configuration
import asyncio
import errno
import os
import platform
import stat

from loguru import logger

from common.custom.custom_handle import HandlerRegistry
from common.custom.interface_type import InterfaceType
from common.util.app_config import get_root_path, load_conf_as_dict, apply_env_overrides
from common.util.conf_obj import ConfObj
from common.util.constant_param import CONFIG_FILE_PATH, SSL_PATH

decrypt_handle = HandlerRegistry.get_handler(InterfaceType.DECRYPT)


def load_conf_obj(conf_file: str) -> ConfObj:
    config_dict = load_conf_as_dict(conf_file)
    apply_env_overrides(config_dict)
    return ConfObj.as_object(config_dict)


def load_cert_password(password_path: str) -> bytes:
    if not os.path.exists(password_path):
        return b""
    with open(password_path, 'r', encoding='utf-8') as f:
        str_content = f.read().strip()
        return asyncio.run(decrypt_handle.handle(str_content))


def set_ssl_folder_permissions():
    if platform.system().lower() != "linux":
        logger.info(f"current system type is: {platform.system().lower()}")
        return
    _restrict_ssl_permissions(SSL_PATH, 0o700)
    for root, _, files in os.walk(SSL_PATH):
        for file_name in files:
            file_path = os.path.join(root, file_name)
            _restrict_ssl_permissions(file_path, 0o600)


def _restrict_ssl_permissions(path, desired_mode):
    """Respect secure read-only secret mounts, reject unsafe ones.

    Mounts can be owned by the deployment controller rather than the app UID.
    Group read/traversal permits an explicitly provisioned secret-reader group;
    world access and group/other writes are never accepted after chmod fails.
    """
    current = stat.S_IMODE(os.stat(path).st_mode)
    if current == desired_mode:
        return
    try:
        os.chmod(path, desired_mode)
    except OSError as exc:
        if exc.errno not in (errno.EROFS, errno.EPERM, errno.EACCES):
            raise
        if current & (stat.S_IRWXO | stat.S_IWGRP):
            raise PermissionError(f'Unsafe permissions on TLS mount: {path}') from exc
        logger.info(f'Using deployment-managed TLS permissions on {path}')


# Singleton instance
conf_singleton_obj = load_conf_obj(CONFIG_FILE_PATH)
