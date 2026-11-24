"""加载推演引擎：在同一进程语义下，按给定顺序模拟两个插件的依赖解析。

规则（与需求一一对应）：
  * 不执行任何插件 / 库，只读取 ELF 元数据；
  * 每种顺序都从“尚未装入任何私有库”的空状态独立推演；
  * 按 DT_NEEDED 的顺序逐层（广度优先）找库；
  * 引用方用自己的 DT_RUNPATH 找首个同名文件，且只支持 $ORIGIN 相对目录；
  * 同名私有库一旦装入，后续引用只能复用这一份；
  * 只支持文件名与 SONAME 一致的私有库；
  * 固定名单内的系统库直接跳过，不做任何核查；
  * 每条私有依赖边都核对引用方要求的 GNU 版本化强符号；
  * 核查优化：每种顺序里，每个实际装入的私有提供方库的动态符号表最多
    遍历一遍；核查索引只登记待核的（符号, 版本）对；
  * 安全约束：所有路径必须落在挂载目录内。
"""
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field

from .elfparse import ElfError, ElfFile

# 固定名单的系统库：命中即跳过，不找文件、不核查。
SYSTEM_LIBS = frozenset({
    "ld-linux-x86-64.so.2",
    "libc.so.6",
    "libm.so.6",
    "libdl.so.2",
    "librt.so.1",
    "libpthread.so.0",
    "libresolv.so.2",
    "libutil.so.1",
    "libgcc_s.so.1",
    "libstdc++.so.6",
    "libnss_files.so.2",
    "libnss_dns.so.2",
})


class Block(Exception):
    """推演阻断。携带依赖链、原因与（可选的）已命中库路径。"""

    def __init__(self, reason: str, chain: list[dict], hit_path: str | None = None,
                 missing: list[tuple[str, str]] | None = None,
                 edge_seq: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.chain = chain          # [{"name": ..., "path": ...或None}, ...]
        self.hit_path = hit_path
        self.missing = missing or []
        # 该阻断对应的依赖边在原核查顺序（BFS 出队顺序）中的序号，
        # 用于在多个候选阻断里挑出“首个失败”。
        self.edge_seq = edge_seq


@dataclass
class LoadedLib:
    name: str                      # SONAME（== 文件名）；根插件无 SONAME 时用文件名
    path: str                      # 绝对路径
    elf: ElfFile
    is_root: bool = False
    chain: list = field(default_factory=list)  # 从根插件到本库的链


@dataclass
class _PendingCheck:
    """登记在某个提供方库名下的核查索引（只含待核的符号版本对）。

    pairs 仅保存待核 (符号, 版本)；edges 记录引用边（可能多条），
    每条边记下它在原核查顺序中的序号，以便按原顺序报告首个阻断。
    """
    pairs: dict[str, set[str | None]] = field(default_factory=dict)
    edges: dict[tuple[str, str | None], list["_EdgeRef"]] = field(
        default_factory=dict)
    scan_count: int = 0            # 本提供方动态符号表的实际扫描次数（0 或 1）


@dataclass
class _EdgeRef:
    """一条私有依赖边的定位信息。"""
    seq: int                       # 原核查顺序中的边序号
    referrer: LoadedLib
    provider: LoadedLib


def _expand_origin(entry: str, origin_dir: str) -> str | None:
    """展开 $ORIGIN 相对目录；不支持的写法返回 None。"""
    for token in ("$ORIGIN", "${ORIGIN}"):
        if entry == token:
            return origin_dir
        if entry.startswith(token + "/"):
            return os.path.normpath(origin_dir + entry[len(token):])
    return None


def _within(root: str, path: str) -> bool:
    try:
        return os.path.commonpath((root, path)) == root
    except ValueError:
        return False


def _find_in_runpath(referrer: LoadedLib, needed: str, mount_root: str,
                     events: list[str], edge_seq: int) -> str:
    """用引用方自己的 DT_RUNPATH（仅 $ORIGIN 相对目录）找首个同名文件。"""
    origin_dir = os.path.dirname(referrer.path)
    searched: list[str] = []
    unsupported: list[str] = []
    for entry in referrer.elf.runpath:
        resolved = _expand_origin(entry, origin_dir)
        if resolved is None:
            unsupported.append(entry)
            continue
        if not _within(mount_root, os.path.realpath(resolved)):
            raise Block(
                f"DT_RUNPATH 条目 {entry!r} 展开后越出挂载目录，拒绝继续",
                chain=referrer.chain + [{"name": needed, "path": None}],
                edge_seq=edge_seq)
        searched.append(resolved)
        candidate = os.path.join(resolved, needed)
        if os.path.isfile(candidate):
            if unsupported:
                events.append(
                    f"忽略不支持的 DT_RUNPATH 条目（仅支持 $ORIGIN 相对目录）："
                    f"{', '.join(unsupported)}")
            return candidate
    detail = f"引用方 {referrer.name} 的 DT_RUNPATH 中找不到 {needed}"
    if searched:
        detail += "；已按序查找目录：" + "、".join(searched)
    else:
        detail += "；DT_RUNPATH 中没有可用的 $ORIGIN 相对目录"
    if unsupported:
        detail += "；不支持的条目（已跳过）：" + "、".join(unsupported)
    raise Block(detail, chain=referrer.chain + [{"name": needed, "path": None}],
                edge_seq=edge_seq)


def _load_private(referrer: LoadedLib, needed: str, path: str,
                  mount_root: str, edge_seq: int) -> LoadedLib:
    """解析候选私有库并做装入前检查（x86-64、SONAME==文件名）。"""
    try:
        elf = ElfFile(path)
    except ElfError as exc:
        raise Block(f"候选库 {path} 不是可用的 x86-64 ELF：{exc}",
                    chain=referrer.chain + [{"name": needed, "path": path}],
                    hit_path=path, edge_seq=edge_seq)
    if elf.soname != needed:
        raise Block(
            f"私有库 {path} 的 SONAME（{elf.soname or '无'}）与文件名 "
            f"{needed} 不一致，不在支持范围",
            chain=referrer.chain + [{"name": needed, "path": path}],
            hit_path=path, edge_seq=edge_seq)
    return LoadedLib(name=needed, path=path, elf=elf,
                     chain=referrer.chain + [{"name": needed, "path": path}])


def simulate_order(plugin_paths: list[str], mount_root: str) -> dict:
    """从空状态推演一种装入顺序。plugin_paths 为绝对路径。"""
    mount_root = os.path.realpath(mount_root)
    loaded: dict[str, LoadedLib] = {}
    loads: list[dict] = []     # 实际装入的库（含根插件），按装入先后
    events: list[str] = []     # 过程日志（复用 / 跳过系统库等）
    # 提供方库名 -> 核查索引（只登记待核的符号版本对与引用边）
    pending: dict[str, _PendingCheck] = {}
    edge_seq = 0               # 私有依赖边的原核查顺序序号
    find_block: Block | None = None  # 找库 / 装入阶段的首个阻断候选（缺库等）
    root_block: Block | None = None

    def record_load(lib: LoadedLib, via: str | None, reused: bool) -> None:
        loads.append({"name": lib.name, "path": lib.path, "via": via,
                      "reused": reused, "is_root": lib.is_root})

    def earlier_block(first: Block | None, candidate: Block) -> Block:
        """保留原核查顺序（边序号）更早的阻断；同序号时缺符号优先。"""
        if first is None:
            return candidate
        if candidate.edge_seq is None:
            return first
        if first.edge_seq is None or candidate.edge_seq < first.edge_seq:
            return candidate
        return first

    def register_requirements(referrer: LoadedLib, provider: LoadedLib,
                              seq: int) -> None:
        """把这条边要求的版本化强符号对登记进提供方的核查索引。

        不在这里扫描提供方符号表；同一提供方的所有边登记完后只扫一遍。
        """
        reqs = referrer.elf.versioned_requirements(provider.name)
        if not reqs:
            return
        index = pending.setdefault(provider.name, _PendingCheck())
        ref = _EdgeRef(seq=seq, referrer=referrer, provider=provider)
        for sym, ver in reqs:
            index.pairs.setdefault(sym, set()).add(ver)
            index.edges.setdefault((sym, ver), []).append(ref)

    try:
        for plugin_path in plugin_paths:
            try:
                elf = ElfFile(plugin_path)
            except ElfError as exc:
                raise Block(f"插件 {plugin_path} 不是可用的 x86-64 ELF：{exc}",
                            chain=[{"name": os.path.basename(plugin_path),
                                    "path": plugin_path}],
                            hit_path=plugin_path)
            root_name = elf.soname or os.path.basename(plugin_path)
            root = LoadedLib(name=root_name, path=plugin_path, elf=elf,
                             is_root=True,
                             chain=[{"name": root_name, "path": plugin_path}])
            loaded.setdefault(root_name, root)
            record_load(root, via=None, reused=False)

            queue: deque[tuple[LoadedLib, str]] = deque(
                (root, n) for n in elf.needed)
            while queue:
                referrer, needed = queue.popleft()
                if needed in SYSTEM_LIBS:
                    events.append(f"{referrer.name} 需要 {needed}："
                                  f"固定名单内的系统库，跳过核查")
                    continue
                edge_seq += 1
                seq = edge_seq
                if needed in loaded:
                    provider = loaded[needed]
                    events.append(f"{referrer.name} 需要 {needed}："
                                  f"同名私有库已装入，复用 {provider.path}")
                    record_load(provider, via=referrer.name, reused=True)
                else:
                    # 找库 / 装入失败属于“缺库”类阻断：只登记首个候选，
                    # 跳过这条分支但不终止整体推演——更早边的符号缺失仍要
                    # 按原核查顺序优先报告。
                    try:
                        path = _find_in_runpath(referrer, needed, mount_root,
                                                events, seq)
                        provider = _load_private(referrer, needed, path,
                                                 mount_root, seq)
                    except Block as missing_lib:
                        events.append(
                            f"{referrer.name} 需要 {needed}：查找/装入失败，"
                            f"跳过该依赖分支：{missing_lib.reason}")
                        find_block = earlier_block(find_block, missing_lib)
                        continue
                    loaded[needed] = provider
                    events.append(f"{referrer.name} 需要 {needed}："
                                  f"装入 {path}")
                    record_load(provider, via=referrer.name, reused=False)
                    for n in provider.elf.needed:
                        queue.append((provider, n))
                register_requirements(referrer, provider, seq)
    except Block as b:
        # 根插件自身解析失败：没有依赖边可继续，直接作为本顺序结论。
        root_block = b

    block: Block | None = root_block

    # 符号核查：每个实际装入的提供方私有库只遍历一遍动态符号表。
    # 根插件已无法解析时不做后续核查。
    symbol_block: Block | None = None
    provider_scans: list[dict] = []
    if root_block is None:
        for name, index in pending.items():
            provider = loaded[name]
            # 关键优化：无论有多少条引用边、多少个待核对，整表只扫这一遍。
            missing_pairs = provider.elf.check_provided_pairs(index.pairs)
            index.scan_count = 1
            provider_scans.append({
                "name": name,
                "path": provider.path,
                "scan_count": 1,
                "checked_pairs": sum(len(v) for v in index.pairs.values()),
                "missing_pairs": sorted(
                    (s, v if v is not None else "")
                    for s, v in missing_pairs),
            })
            if not missing_pairs:
                continue
            # 每个缺失对可能由多条边引用：取原核查顺序里最早的那条边报告，
            # 再在多个缺失对之间取最早者，即“按原核查顺序的首个失败”。
            earliest_ref: _EdgeRef | None = None
            for pair in missing_pairs:
                for ref in index.edges.get(pair, ()):
                    if earliest_ref is None or ref.seq < earliest_ref.seq:
                        earliest_ref = ref
            if earliest_ref is None:
                continue  # 理论上不会发生：缺失对必然来自某条登记边
            missing_sorted = sorted(missing_pairs)
            pretty = "、".join(
                f"{s}@{v if v is not None else '(无版本)'}"
                for s, v in missing_sorted)
            ref = earliest_ref
            cand = Block(
                f"已装入的 {name} 缺少引用方 {ref.referrer.name} 要求的 "
                f"GNU 版本化强符号：{pretty}",
                chain=ref.referrer.chain + [{"name": name,
                                             "path": provider.path}],
                hit_path=provider.path,
                missing=[(s, v if v is not None else "")
                         for s, v in missing_sorted],
                edge_seq=ref.seq)
            symbol_block = earlier_block(symbol_block, cand)
        # 缺符号与缺库同时存在时，按原核查顺序报告首个失败的依赖链。
        block = earlier_block(find_block, symbol_block) \
            if symbol_block is not None else find_block

    total_scans = sum(idx.scan_count for idx in pending.values())

    return {
        "order": list(plugin_paths),
        "status": "blocked" if block else "ok",
        "loads": loads,
        "events": events,
        # 提供方符号表扫描统计：每个实际装入的私有库在本顺序内至多 1 次。
        "provider_scan_count": total_scans,
        "provider_scans": provider_scans,
        "block": None if block is None else {
            "reason": block.reason,
            "chain": block.chain,
            "hit_path": block.hit_path,
            "missing": block.missing,
            "edge_seq": block.edge_seq,
        },
    }


def simulate_both_orders(mount_root: str, rel_a: str, rel_b: str) -> dict:
    """对两个插件分别推演 A→B 与 B→A 两种顺序（各自从空状态开始）。"""
    path_a = resolve_inside(mount_root, rel_a)
    path_b = resolve_inside(mount_root, rel_b)
    return {
        "mount_root": os.path.realpath(mount_root),
        "plugin_a": rel_a,
        "plugin_b": rel_b,
        "orders": [
            simulate_order([path_a, path_b], mount_root),
            simulate_order([path_b, path_a], mount_root),
        ],
    }


def resolve_inside(mount_root: str, rel: str) -> str:
    """把挂载目录内的相对路径解析为绝对路径，拒绝越界。"""
    root = os.path.realpath(mount_root)
    path = os.path.realpath(os.path.join(root, rel))
    if not _within(root, path):
        raise ElfError(f"路径越出挂载目录：{rel}")
    if not os.path.isfile(path):
        raise ElfError(f"文件不存在：{rel}")
    return path
