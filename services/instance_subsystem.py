"""
容器实例管理子系统 — 借鉴 MCSM 的 InstanceSubsystem 模式

核心设计：
  - 所有容器实例保存在内存 Dict 中（_instances）
  - 使用 "node_id:name" 复合键，支持多节点同名容器共存
  - 查询全部走内存读，零 Docker API 调用
  - 提供分页查询（MCSM instance/select 模式）
  - 单例 instance_subsystem 全局访问
"""
from typing import Dict, List, Optional

from services.container_instance import ContainerInstance


def _key(node_id: str, name: str) -> str:
    """生成字典复合键。"""
    return f"{node_id}:{name}"


class InstanceSubsystem:
    """容器实例子系统 — 等价于 MCSM 的 InstanceSubsystem。"""

    def __init__(self):
        self._instances: Dict[str, ContainerInstance] = {}

    # ============ 基础 CRUD ============

    def get_all(self) -> List[ContainerInstance]:
        """返回所有实例列表。"""
        return list(self._instances.values())

    def get(self, name: str, node_id: str = "local") -> Optional[ContainerInstance]:
        """按 (node_id, name) 获取单个实例。"""
        return self._instances.get(_key(node_id, name))

    def exists(self, name: str, node_id: str = "local") -> bool:
        return _key(node_id, name) in self._instances

    def upsert(self, name: str, *, node_id: str = "local", **kwargs) -> ContainerInstance:
        """新增或更新实例。

        容器列表刷新时调用 — 存在则更新属性，不存在则创建。
        使用 (node_id, name) 复合键，同名不同节点的容器各自独立。
        """
        k = _key(node_id, name)
        if k in self._instances:
            inst = self._instances[k]
            # 确保 node_id 也同步
            inst.node_id = node_id
            if "uin" in kwargs:
                logged_in = bool(kwargs.pop("logged_in", bool(kwargs.get("uin"))))
                uin = str(kwargs.pop("uin") or "")
                stage = str(kwargs.pop("login_stage", "logged_in" if logged_in else "waiting") or "waiting")
                method = str(kwargs.pop("login_method", inst.login_method or "") or "")
                reason = str(kwargs.pop("login_reason", inst.login_reason or "") or "")
                inst.update_login(logged_in=logged_in, uin=uin, stage=stage, method=method, reason=reason)
            for kk, v in kwargs.items():
                if hasattr(inst, kk) and not kk.startswith("_"):
                    # last_uin 是历史账号，空值不覆盖已有值。
                    if kk == "last_uin" and not v:
                        continue
                    setattr(inst, kk, v)
            return inst
        login_payload = None
        if "uin" in kwargs:
            logged_in = bool(kwargs.pop("logged_in", bool(kwargs.get("uin"))))
            login_payload = {
                "logged_in": logged_in,
                "uin": str(kwargs.pop("uin") or ""),
                "stage": str(kwargs.pop("login_stage", "logged_in" if logged_in else "waiting") or "waiting"),
                "method": str(kwargs.pop("login_method", "") or ""),
                "reason": str(kwargs.pop("login_reason", "") or ""),
            }
        inst = ContainerInstance(name=name, node_id=node_id, **kwargs)
        if login_payload:
            inst.update_login(**login_payload)
        self._instances[k] = inst
        return inst

    def remove(self, name: str, node_id: str = "local") -> None:
        """移除实例。"""
        self._instances.pop(_key(node_id, name), None)

    def remove_by_node(self, node_id: str) -> int:
        """移除某节点下的全部实例，返回数量 — 节点删除时的级联清理。"""
        stale = [k for k, inst in self._instances.items() if inst.node_id == node_id]
        for k in stale:
            self._instances.pop(k, None)
        return len(stale)

    def find_by_uin(self, uin: str) -> List[ContainerInstance]:
        """按 QQ 号查找全部匹配实例（跨节点）。

        心跳回写必须遍历所有匹配项：换号/迁移后多个节点可能残留同一个 uin，
        只更新第一个会让另一个永久停在「在线」。
        """
        if not uin:
            return []
        return [inst for inst in self._instances.values() if inst.uin == uin]

    def cleanup(self, active_keys: set, cleanable_nodes: set | None = None) -> List[str]:
        """清理已不存在的容器，返回被清理的复合键列表。

        active_keys: set of (node_id, name) 二元组。
        cleanable_nodes: 成功响应的节点 ID 集合。若为 None 则清理全部；
                         否则只清理属于这些节点且不在 active_keys 中的容器。
        """
        active_set = {_key(nid, n) for nid, n in active_keys}
        stale = []
        for k, inst in self._instances.items():
            if k in active_set:
                continue
            # 仅清理归属于成功响应节点的容器；未响应节点的容器保留
            if cleanable_nodes is not None and inst.node_id not in cleanable_nodes:
                continue
            stale.append(k)
        for k in stale:
            self._instances.pop(k, None)
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

    def get_qr_states_public(self) -> Dict[str, Dict]:
        """公开 QR 状态 — 不包含二维码图片数据。"""
        result: Dict[str, Dict] = {}
        for inst in self._instances.values():
            if inst.status != "running":
                continue
            result[inst.name] = inst.to_qr_dict_public()
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
                      if kw in i.name.lower() or kw in i.uin.lower() or kw in i.last_uin.lower()]
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

