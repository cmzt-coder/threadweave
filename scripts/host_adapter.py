"""宿主适配层接口与默认拒绝实现。

真实适配器由具体宿主提供；纯逻辑模块与控制器只依赖本接口，不直接调用宿主 API。
"""
from abc import ABC, abstractmethod


class HostActionPending(RuntimeError):
    """外部动作尚未取得结果；排队等待不能当作调用失败或成功。"""


class HostAdapter(ABC):
    """控制器依赖的宿主能力接口。"""

    # 可选能力声明；具体适配器可覆盖，控制器据此选择降级路径。
    capabilities = {
        'update_plan': True,
        'wait_threads': True,
        'create_timeout': False,
    }

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

    def capability_snapshot(self):
        """返回当前适配器声明的能力快照。"""
        return dict(self.capabilities)

    @abstractmethod
    def create_worktree(self, name):
        """创建隔离 worktree。"""

    @abstractmethod
    def archive_worktree(self, root):
        """归档 worktree。"""

    def attach_worktree(self, path):
        """重新附加既有 managed worktree；旧适配器保持拒绝。"""
        raise NotImplementedError('attach_worktree')

    def worktree_creation_status(self, operation_id):
        """查询异步 worktree 操作；operationId 不是目录或 threadId。"""
        raise NotImplementedError('worktree_creation_status')

    def archive_thread(self, thread_id):
        """归档明确指定的聊天，不代表已停止或已清理工作区。"""
        raise NotImplementedError('archive_thread')


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
