"""
容器实例管理子系统 — 借鉴 MCSM 的 InstanceSubsystem 模式

核心设计：
  - 所有容器实例保存在内存 Dict 中（_instances）
  - 查询全部走内存读，零 Docker API 调用
  - 提供分页查询（MCSM instance/select 模式）
  - 单例 instance_subsystem 全局访问
"""
from typing import Dict, List, Optional

from services.container_instance import ContainerInstance


class InstanceSubsystem:
    """容器实例子系统 — 等价于 MCSM 的 InstanceSubsystem。"""

    def __init__(self):
        self._instances: Dict[str, ContainerInstance] = {}

    # ============ 基础 CRUD ============

    def get_all(self) -> List[ContainerInstance]:
        """返回所有实例列表。"""
        return list(self._instances.values())

    def get(self, name: str) -> Optional[ContainerInstance]:
        """按名称获取单个实例。"""
        return self._instances.get(name)

    def exists(self, name: str) -> bool:
        return name in self._instances

    def upsert(self, name: str, **kwargs) -> ContainerInstance:
        """新增或更新实例。

        容器列表刷新时调用 — 存在则更新属性，不存在则创建。
        """
        if name in self._instances:
            inst = self._instances[name]
            for k, v in kwargs.items():
                if hasattr(inst, k) and not k.startswith("_"):
                    setattr(inst, k, v)
            return inst
        inst = ContainerInstance(name=name, **kwargs)
        self._instances[name] = inst
        return inst

    def remove(self, name: str) -> None:
        """移除实例。"""
        self._instances.pop(name, None)

    def cleanup(self, active_names: set) -> List[str]:
        """清理已不存在的容器，返回被清理的名称列表。"""
        stale = [n for n in self._instances if n not in active_names]
        for n in stale:
            self._instances.pop(n, None)
        return stale

    @property
    def count(self) -> int:
        return len(self._instances)

    # ============ 批量读接口（兼容旧 state_engine 接口） ============

    def get_containers_list(self) -> List[Dict]:
        """兼容 state_engine.get_containers() — 返回 List[Dict]。"""
        return [inst.to_public_dict() for inst in self._instances.values()]

    def get_qr_states(self) -> Dict[str, Dict]:
        """兼容 state_engine.get_qr_states() — 返回 {name: qr_dict}。"""
        result: Dict[str, Dict] = {}
        for inst in self._instances.values():
            if inst.status != "running":
                continue
            result[inst.name] = inst.to_qr_dict()
        return result

    def get_all_stats(self) -> Dict[str, Dict]:
        """兼容 state_engine.get_all_stats() — 返回 {name: stats_dict}。"""
        result: Dict[str, Dict] = {}
        for inst in self._instances.values():
            if inst.stats_ts > 0:
                result[inst.name] = inst.to_stats_dict()
        return result

    # ============ 分页查询（MCSM instance/select 模式） ============

    def query(self, status: Optional[str] = None, keyword: Optional[str] = None,
              page: int = 1, page_size: int = 20) -> Dict:
        """服务端分页查询。

        Args:
            status: 状态过滤 (running / exited / ...)
            keyword: 关键词搜索 (匹配 name 或 uin)
            page: 页码 (从 1 开始)
            page_size: 每页数量
        """
        result = self.get_all()
        if status:
            result = [i for i in result if i.status == status]
        if keyword:
            kw = keyword.lower()
            result = [i for i in result
                      if kw in i.name.lower() or kw in i.uin.lower()]
        total = len(result)
        start = (page - 1) * page_size
        page_data = result[start:start + page_size]
        return {
            "page": page,
            "page_size": page_size,
            "total": total,
            "max_page": max(1, (total + page_size - 1) // page_size),
            "data": [i.to_public_dict() for i in page_data],
        }


# ============ 单例 ============
instance_subsystem = InstanceSubsystem()

