"""宿主适配层接口与默认拒绝实现。

真实适配器由具体宿主提供；纯逻辑模块与控制器只依赖本接口，不直接调用宿主 API。
"""
from abc import ABC, abstractmethod


class HostAdapter(ABC):
    """控制器依赖的宿主能力接口。"""

    @abstractmethod
    def create_worker(self, prompt, project_id):
        """创建真实 worker 线程，返回含 threadId 的结果。"""

    @abstractmethod
    def send_message(self, thread_id, message):
        """向真实线程发送消息。"""

    @abstractmethod
    def read_thread(self, thread_id):
        """读取线程状态。"""

    @abstractmethod
    def wait_threads(self, thread_ids, timeout_seconds):
        """等待线程事件，返回完成/超时结果。"""

    @abstractmethod
    def create_worktree(self, name):
        """创建隔离 worktree。"""

    @abstractmethod
    def archive_worktree(self, root):
        """归档 worktree。"""


class UnsupportedHostAdapter(HostAdapter):
    """默认拒绝实现；未接入真实宿主时所有调用都抛 NotImplementedError。"""

    def create_worker(self, prompt, project_id):
        raise NotImplementedError('create_worker')

    def send_message(self, thread_id, message):
        raise NotImplementedError('send_message')

    def read_thread(self, thread_id):
        raise NotImplementedError('read_thread')

    def wait_threads(self, thread_ids, timeout_seconds):
        raise NotImplementedError('wait_threads')

    def create_worktree(self, name):
        raise NotImplementedError('create_worktree')

    def archive_worktree(self, root):
        raise NotImplementedError('archive_worktree')
