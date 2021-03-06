# Copyright 2015-2016 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import os.path
import random

import yaml


def _get_smartstack_proxy_ports_from_file(root, file):
    """Given a root and file (as from os.walk), attempt to return the highest
    smartstack proxy port number (int) from that file. Returns 0 if there is no
    smartstack proxy_port.
    """
    ports = set()
    with open(os.path.join(root, file)) as f:
        data = yaml.safe_load(f)

    if file.endswith('service.yaml') and 'smartstack' in data:
        # Specifying this in service.yaml is old and deprecated and doesn't
        # support multiple namespaces.
        ports = {int(data['smartstack'].get('proxy_port', 0))}
    elif file.endswith('smartstack.yaml'):
        for namespace in data.keys():
            ports.add(data[namespace].get('proxy_port', 0))

    return ports


def suggest_smartstack_proxy_port(yelpsoa_config_root, range_min=20000, range_max=21000):
    """Pick a random available port in the 20000-21000 block"""
    available_proxy_ports = set(range(range_min, range_max + 1))
    for root, dirs, files in os.walk(yelpsoa_config_root):
        for f in files:
            if f.endswith('smartstack.yaml'):
                try:
                    used_ports = _get_smartstack_proxy_ports_from_file(root, f)
                    for used_port in used_ports:
                        available_proxy_ports.discard(used_port)
                except Exception:
                    pass

    try:
        return random.choice(list(available_proxy_ports))
    except IndexError:
        raise Exception("There are no more ports available in the range [%s, %s]" % (range_min, range_max))


# vim: expandtab tabstop=4 sts=4 shiftwidth=4:
