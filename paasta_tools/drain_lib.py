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
import re
import time
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Type
from typing import TypeVar

import aiohttp
from mypy_extensions import TypedDict

from paasta_tools.utils import get_user_agent

_drain_methods: Dict[str, Type["DrainMethod"]] = {}
HACHECK_CONN_TIMEOUT = 3
HACHECK_READ_TIMEOUT = 1


_RegisterDrainMethod_T = TypeVar('_RegisterDrainMethod_T', bound=Type["DrainMethod"])


def register_drain_method(name: str) -> Callable[[_RegisterDrainMethod_T], _RegisterDrainMethod_T]:
    """Returns a decorator that registers a DrainMethod subclass at a given name
    so get_drain_method/list_drain_methods can find it."""
    def outer(drain_method: _RegisterDrainMethod_T) -> _RegisterDrainMethod_T:
        _drain_methods[name] = drain_method
        return drain_method
    return outer


def get_drain_method(
    name: str,
    service: str,
    instance: str,
    nerve_ns: str,
    drain_method_params: Optional[Dict]=None,
) -> "DrainMethod":
    return _drain_methods[name](service, instance, nerve_ns, **(drain_method_params or {}))


def list_drain_methods() -> List[str]:
    return sorted(_drain_methods.keys())


DrainTask = TypeVar('DrainTask', bound=Any)


class DrainMethod(object):
    """A drain method is a way of stopping new traffic to tasks without killing them. For example, you might take a task
    out of a load balancer by causing its healthchecks to fail.

    A drain method must have the following methods:
     - drain(task): Begin draining traffic from a task. This should be idempotent.
     - stop_draining(task): Stop draining traffic from a task. This should be idempotent.
     - is_draining(task): Whether a task has already been marked as downed. Note that this state should be stored out of
                          process, because a bounce may take multiple runs of setup_marathon_job to complete.
     - is_safe_to_kill(task): Return True if this task is safe to kill, False otherwise.

    When implementing a drain method, be sure to decorate with @register_drain_method(name).
    """

    def __init__(self, service: str, instance: str, nerve_ns: str, **kwargs: Dict) -> None:
        self.service = service
        self.instance = instance
        self.nerve_ns = nerve_ns

    async def drain(self, task: DrainTask) -> None:
        """Make a task stop receiving new traffic."""
        raise NotImplementedError()

    async def stop_draining(self, task: DrainTask) -> None:
        """Make a task that has previously been downed start receiving traffic again."""
        raise NotImplementedError()

    async def is_draining(self, task: DrainTask) -> bool:
        """Return whether a task is being drained."""
        raise NotImplementedError()

    async def is_safe_to_kill(self, task: DrainTask) -> bool:
        """Return True if a task is drained and ready to be killed, or False if we should wait."""
        raise NotImplementedError()


@register_drain_method('noop')
class NoopDrainMethod(DrainMethod):
    """This drain policy does nothing and assumes every task is safe to kill."""

    async def drain(self, task: DrainTask) -> None:
        pass

    async def stop_draining(self, task: DrainTask) -> None:
        pass

    async def is_draining(self, task: DrainTask) -> bool:
        return False

    async def is_safe_to_kill(self, task: DrainTask) -> bool:
        return True


@register_drain_method('test')
class TestDrainMethod(DrainMethod):
    """This drain policy is meant for integration testing. Do not use."""

    # These are variables on the class for ease of use in testing.
    downed_task_ids: Set[str] = set()
    safe_to_kill_task_ids: Set[str] = set()

    async def drain(self, task: DrainTask) -> None:
        if task.id not in self.safe_to_kill_task_ids:
            self.downed_task_ids.add(task.id)

    async def stop_draining(self, task: DrainTask) -> None:
        self.downed_task_ids -= {task.id}
        self.safe_to_kill_task_ids -= {task.id}

    async def is_draining(self, task: DrainTask) -> bool:
        return task.id in (self.downed_task_ids | self.safe_to_kill_task_ids)

    async def is_safe_to_kill(self, task: DrainTask) -> bool:
        return task.id in self.safe_to_kill_task_ids

    @classmethod
    def mark_arbitrary_task_as_safe_to_kill(cls) -> None:
        cls.safe_to_kill_task_ids.add(cls.downed_task_ids.pop())


@register_drain_method('crashy_drain')
class CrashyDrainDrainMethod(NoopDrainMethod):
    async def drain(self, task: DrainTask) -> None:
        raise Exception("Intentionally crashing for testing purposes")


@register_drain_method('crashy_is_safe_to_kill')
class CrashySafeToKillDrainMethod(NoopDrainMethod):
    async def is_safe_to_kill(self, task: DrainTask) -> bool:
        raise Exception("Intentionally crashing for testing purposes")


SpoolInfo = TypedDict(
    'SpoolInfo',
    {
        'service': str,
        'state': str,
        'since': float,
        'until': float,
        'reason': str,
    },
    total=False,
)


@register_drain_method('hacheck')
class HacheckDrainMethod(DrainMethod):
    """This drain policy issues a POST to hacheck's /spool/{service}/{port}/status endpoint to cause healthchecks to
    fail. It considers tasks safe to kill if they've been down in hacheck for more than a specified delay."""

    def __init__(
        self,
        service: str,
        instance: str,
        nerve_ns: str,
        delay: float=120,
        hacheck_port: int=6666,
        expiration: float=0,
        **kwargs: Dict,
    ) -> None:
        super(HacheckDrainMethod, self).__init__(service, instance, nerve_ns)
        self.delay = float(delay)
        self.hacheck_port = hacheck_port
        self.expiration = float(expiration) or float(delay) * 10

    def spool_url(self, task: DrainTask) -> str:
        if task.ports == []:
            return None
        else:
            return 'http://%(task_host)s:%(hacheck_port)d/spool/%(service)s.%(nerve_ns)s/%(task_port)d/status' % {
                'task_host': task.host,
                'task_port': task.ports[0],
                'hacheck_port': self.hacheck_port,
                'service': self.service,
                'nerve_ns': self.nerve_ns,
            }

    async def post_spool(self, task: DrainTask, status: str) -> None:
        spool_url = self.spool_url(task)
        if spool_url is not None:
            data: Dict[str, str] = {'status': status}
            if status == 'down':
                data.update({
                    'expiration': str(time.time() + self.expiration),
                    'reason': 'Drained by Paasta',
                })
            async with aiohttp.ClientSession(
                conn_timeout=HACHECK_CONN_TIMEOUT,
                read_timeout=HACHECK_READ_TIMEOUT,
            ) as session:
                async with session.post(
                    self.spool_url(task),
                    data=data,
                    headers={'User-Agent': get_user_agent()},
                ) as resp:
                    resp.raise_for_status()

    async def get_spool(self, task: DrainTask) -> SpoolInfo:
        """Query hacheck for the state of a task, and parse the result into a dictionary."""
        spool_url = self.spool_url(task)
        if spool_url is None:
            return None

        # TODO: aiohttp says not to create a session per request. Fix this.
        async with aiohttp.ClientSession(
            conn_timeout=HACHECK_CONN_TIMEOUT,
            read_timeout=HACHECK_READ_TIMEOUT,
        ) as session:
            response = await session.get(
                self.spool_url(task),
                headers={'User-Agent': get_user_agent()},
            )
            if response.status == 200:
                return {
                    'state': 'up',
                }

            regex = ''.join([
                "^",
                r"Service (?P<service>.+)",
                r" in (?P<state>.+) state",
                r"(?: since (?P<since>[0-9.]+))?",
                r"(?: until (?P<until>[0-9.]+))?",
                r"(?:: (?P<reason>.*))?",
                "$",
            ])

            response_text = await response.text()
            match = re.match(regex, response_text)
            groupdict = match.groupdict()
            info: SpoolInfo = {}
            info['service'] = groupdict['service']
            info['state'] = groupdict['state']
            if 'since' in groupdict:
                info['since'] = float(groupdict['since'] or 0)
            if 'until' in groupdict:
                info['until'] = float(groupdict['until'] or 0)
            if 'reason' in groupdict:
                info['reason'] = groupdict['reason']
            return info

    async def drain(self, task: DrainTask) -> None:
        return await self.post_spool(task, 'down')

    async def stop_draining(self, task: DrainTask) -> None:
        return await self.post_spool(task, 'up')

    async def is_draining(self, task: DrainTask) -> bool:
        info = await self.get_spool(task)
        if info is None or info["state"] == "up":
            return False
        else:
            return True

    async def is_safe_to_kill(self, task: DrainTask) -> bool:
        info = await self.get_spool(task)
        if info is None or info["state"] == "up":
            return False
        else:
            return info.get("since", 0) < (time.time() - self.delay)


class StatusCodeNotAcceptableError(Exception):
    pass


UrlSpec = TypedDict(
    'UrlSpec',
    {
        'url_format': str,
        'method': str,
        'success_codes': str,
    },
    total=False,
)


@register_drain_method('http')
class HTTPDrainMethod(DrainMethod):
    """This drain policy issues arbitrary HTTP calls to arbitrary URLs specified by the parameters. The URLs are
    specified as format strings, and will have variables such as {host}, {port}, etc. filled in."""

    def __init__(
        self,
        service: str,
        instance: str,
        nerve_ns: str,
        drain: UrlSpec,
        stop_draining: UrlSpec,
        is_draining: UrlSpec,
        is_safe_to_kill: UrlSpec,
    ) -> None:
        super(HTTPDrainMethod, self).__init__(service, instance, nerve_ns)
        self.drain_url_spec = drain
        self.stop_draining_url_spec = stop_draining
        self.is_draining_url_spec = is_draining
        self.is_safe_to_kill_url_spec = is_safe_to_kill

    def get_format_params(self, task: DrainTask) -> Dict[str, Any]:
        return {
            'host': task.host,
            'port': task.ports[0],
            'service': self.service,
            'instance': self.instance,
            'nerve_ns': self.nerve_ns,
        }

    def format_url(self, url_format: str, format_params: Dict[str, Any]) -> str:
        return url_format.format(**format_params)

    def parse_success_codes(self, success_codes_str: str) -> Set[int]:
        """Expand a string like 200-399,407-409,500 to a set containing all the integers in between."""
        acceptable_response_codes: Set[int] = set()
        for series_str in str(success_codes_str).split(','):
            if '-' in series_str:
                start, end = series_str.split('-')
                acceptable_response_codes.update(range(int(start), int(end) + 1))
            else:
                acceptable_response_codes.add(int(series_str))
        return acceptable_response_codes

    def check_response_code(self, status_code: int, success_codes_str: str) -> None:
        acceptable_response_codes = self.parse_success_codes(success_codes_str)
        if status_code not in acceptable_response_codes:
            raise StatusCodeNotAcceptableError("Status code %d not in %s", status_code, success_codes_str)

    async def issue_request(self, url_spec: UrlSpec, task: DrainTask) -> None:
        """Issue a request to the URL specified by url_spec regarding the task given."""
        format_params = self.get_format_params(task)
        url = self.format_url(url_spec['url_format'], format_params)
        method = url_spec.get('method', 'GET').upper()

        async with aiohttp.ClientSession() as session:
            response = await session.request(
                method=method,
                url=url,
                headers={'User-Agent': get_user_agent()},
                timeout=15,
            )
            self.check_response_code(response.status, url_spec['success_codes'])

    async def drain(self, task: DrainTask) -> None:
        return await self.issue_request(self.drain_url_spec, task)

    async def stop_draining(self, task: DrainTask) -> None:
        return await self.issue_request(self.stop_draining_url_spec, task)

    async def is_draining(self, task: DrainTask) -> bool:
        try:
            await self.issue_request(self.is_draining_url_spec, task)
        except StatusCodeNotAcceptableError:
            return False
        else:
            return True

    async def is_safe_to_kill(self, task: DrainTask) -> bool:
        try:
            await self.issue_request(self.is_safe_to_kill_url_spec, task)
        except StatusCodeNotAcceptableError:
            return False
        else:
            return True
