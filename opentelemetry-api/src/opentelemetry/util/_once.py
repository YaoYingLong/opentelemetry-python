# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from collections.abc import Callable
from threading import Lock


class Once:
    """Execute a function exactly once and block all callers until the function returns

    Same as golang's `sync.Once <https://pkg.go.dev/sync#Once>`_
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._done = False

    def do_once(self, func: Callable[[], None]) -> bool:
        """Execute ``func`` if it hasn't been executed or return.

        Will block until ``func`` has been called by one thread.

        Returns:
            Whether or not ``func`` was executed in this call
        """

        # fast path, try to avoid locking
        if self._done:
            return False
        # 如果获取锁成功执行里面的内容
        with self._lock:
            # 如果没有完成执行里面的内容
            if not self._done:
                func()
                self._done = True
                return True
        return False
