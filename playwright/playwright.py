# Copyright (c) Microsoft Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from playwright.connection import ChannelOwner, ConnectionScope, from_channel
from playwright.browser_type import BrowserType
from typing import Dict


class Playwright(ChannelOwner):
    def __init__(self, scope: ConnectionScope, guid: str, initializer: Dict) -> None:
        super().__init__(scope, guid, initializer)
        self.chromium: BrowserType = from_channel(initializer["chromium"])
        self.firefox: BrowserType = from_channel(initializer["firefox"])
        self.webkit: BrowserType = from_channel(initializer["webkit"])
        self.devices = initializer["deviceDescriptors"]
        self.browser_types: Dict[str, BrowserType] = dict(
            chromium=self.chromium, webkit=self.webkit, firefox=self.firefox
        )
