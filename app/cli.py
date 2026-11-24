"""命令行入口：不依赖 Flask，直接对挂载目录里的两个插件推演两种顺序。

用法：
    python3 -I -m app.cli --dir samples plugin_alpha.so plugin_beta.so
"""
from __future__ import annotations

import argparse
import os
import sys

# 允许 `python3 app/cli.py` 直接运行
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "app"

from .service import list_plugins, run_simulation  # noqa: E402


def _show_order(result: dict, mount_root: str) -> None:
    order = " → ".join(os.path.relpath(p, mount_root) for p in result["order"])
    print(f"顺序：{order}")
    if result["status"] == "ok":
        print("  结论：可行。实际装入的库路径（按装入先后）：")
        seen = set()
        for load in result["loads"]:
            key = load["path"]
            if key in seen:
                continue
            seen.add(key)
            tag = "（根插件）" if load["is_root"] else ""
            print(f"    {load['path']}{tag}")
    else:
        block = result["block"]
        chain = " → ".join(c["name"] for c in block["chain"])
        print("  结论：阻断")
        print(f"    依赖链：{chain}")
        print(f"    原因：{block['reason']}")
        if block["hit_path"]:
            print(f"    已命中库：{block['hit_path']}")
    print(f"  提供方符号表扫描次数：{result['provider_scan_count']}"
          "（本顺序内每个实际装入的私有库最多遍历一遍动态符号表）")
    for scan in result["provider_scans"]:
        missing = "、".join(f"{s}@{v}" for s, v in scan["missing_pairs"]) or "无"
        print(f"    · {scan['name']} <- {scan['path']}：扫描 "
              f"{scan['scan_count']} 次，待核 {scan['checked_pairs']} 对，"
              f"缺失：{missing}")
    for ev in result["events"]:
        print(f"    · {ev}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="两个插件的两种装入顺序推演")
    parser.add_argument("--dir", default=os.environ.get("MOUNT_DIR", "samples"),
                        help="挂载目录（默认 samples/ 或 $MOUNT_DIR）")
    parser.add_argument("plugin_a", nargs="?", help="插件 A（相对挂载目录）")
    parser.add_argument("plugin_b", nargs="?", help="插件 B（相对挂载目录）")
    parser.add_argument("--list", action="store_true", help="只列出可选插件")
    args = parser.parse_args(argv)

    mount_root = os.path.abspath(args.dir)
    if args.list or not (args.plugin_a and args.plugin_b):
        print(f"挂载目录 {mount_root} 内的 x86-64 ELF：")
        for rel in list_plugins(mount_root):
            print(f"  {rel}")
        if not (args.plugin_a or args.plugin_b):
            return 0
        if not (args.plugin_a and args.plugin_b):
            parser.error("需要两个插件参数")

    try:
        result = run_simulation(mount_root, args.plugin_a, args.plugin_b)
    except (ValueError, Exception) as exc:  # noqa: BLE001 - CLI 兜底
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    for order_result in result["orders"]:
        _show_order(order_result, mount_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
