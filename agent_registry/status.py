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

"""
Card status policy.

One definition of "discoverable", shared by the persistence layer, the registry
core and every public read/change surface, so pending ('registered') cards
cannot leak through one path while another hides them.
"""

from typing import Optional

#: Only cards in this status are visible to discovery consumers.
DISCOVERABLE_STATUS = 'published'
#: Cards awaiting approval; stored, but not discoverable.
PENDING_STATUS = 'registered'


def is_discoverable_status(status: Optional[str]) -> bool:
    """Whether a card status is visible to discovery consumers.

    A missing status (records written before status tracking existed) counts as
    discoverable, matching the historical behaviour of every read path.
    """
    return (status or DISCOVERABLE_STATUS) == DISCOVERABLE_STATUS
