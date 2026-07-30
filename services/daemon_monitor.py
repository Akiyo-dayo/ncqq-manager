"""宿主机资源监控 — 定时采样 CPU/内存，维护历史数据供节点状态面板使用。"""
import psutil
from collections import deque
from typing import Dict, List

class DaemonMonitor:
    def __init__(self, history_length: int = 20):
        self.history_length = history_length
        self.cpu_history = deque(maxlen=history_length)
        self.mem_history = deque(maxlen=history_length)
        # Initialize the baseline for cpu_percent
        psutil.cpu_percent()

    def record_tick(self):
        """Called every X seconds (e.g., 30s) to record average CPU over that period."""
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        self.cpu_history.append(cpu)
        self.mem_history.append(mem)

    def get_chart_data(self) -> Dict[str, List[float]]:
        return {
            "cpu": list(self.cpu_history),
            "mem": list(self.mem_history)
        }

    def get_instance_status(self, node_id: str = "") -> Dict[str, int]:
        """读 state_engine 内存快照（零阻塞），不再同步调 docker_manager。

        传 node_id 时只统计该节点 —— 本地节点卡片过去显示的是全集群总和，
        而远程节点回传的又是它自己的全集群总和，数字层层放大且互相矛盾。
        """
        from services.instance_subsystem import instance_subsystem
        instances = instance_subsystem.get_all()
        if node_id:
            instances = [inst for inst in instances if inst.node_id == node_id]
        total = len(instances)
        running = sum(1 for inst in instances if inst.status == "running")
        return {"total": total, "running": running}

    @property
    def current_cpu(self) -> float:
        if self.cpu_history:
            return self.cpu_history[-1]
        return psutil.cpu_percent(interval=None)

    @property
    def current_mem(self) -> float:
        if self.mem_history:
            return self.mem_history[-1]
        return psutil.virtual_memory().percent

daemon_monitor = DaemonMonitor()

