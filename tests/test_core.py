"""核心引擎单元测试。运行：python3 -I tests/test_core.py（从仓库根目录）。"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from app.elfparse import ElfError, ElfFile, is_x86_64_elf  # noqa: E402
from app.resolver import simulate_both_orders, simulate_order  # noqa: E402
from app.service import list_plugins, run_simulation  # noqa: E402

SAMPLES = os.path.join(REPO_ROOT, "samples")
ALPHA = os.path.join(SAMPLES, "plugin_alpha.so")
BETA = os.path.join(SAMPLES, "plugin_beta.so")
FOO_A = os.path.join(SAMPLES, "vendor_alpha", "libfoo.so.1")
FOO_B = os.path.join(SAMPLES, "vendor_beta", "libfoo.so.1")


class TestElfParse(unittest.TestCase):
    def test_dynamic_tags(self):
        elf = ElfFile(ALPHA)
        self.assertEqual(elf.needed, ["libfoo.so.1", "libc.so.6"])
        self.assertEqual(elf.runpath, ["$ORIGIN/vendor_alpha"])
        self.assertIsNone(elf.soname)  # 插件没有 SONAME

    def test_soname(self):
        self.assertEqual(ElfFile(FOO_A).soname, "libfoo.so.1")
        self.assertEqual(ElfFile(FOO_B).soname, "libfoo.so.1")

    def test_versioned_requirements(self):
        self.assertEqual(ElfFile(ALPHA).versioned_requirements("libfoo.so.1"),
                         [("foo", "FOO_1.0")])
        self.assertEqual(ElfFile(BETA).versioned_requirements("libfoo.so.1"),
                         [("foo", "FOO_2.0")])
        # 系统库条目不影响对私有库的提取
        self.assertEqual(ElfFile(ALPHA).versioned_requirements("nosuch.so"), [])

    def test_strong_provided_versions(self):
        self.assertEqual(ElfFile(FOO_A).strong_provided_versions("foo"),
                         {"FOO_1.0"})
        self.assertEqual(ElfFile(FOO_B).strong_provided_versions("foo"),
                         {"FOO_1.0", "FOO_2.0"})
        self.assertEqual(ElfFile(FOO_A).strong_provided_versions("nosuch"),
                         set())

    def test_probe(self):
        self.assertTrue(is_x86_64_elf(ALPHA))
        self.assertFalse(is_x86_64_elf(os.path.join(SAMPLES, "src", "foo_v1.c")))

    def test_reject_non_elf(self):
        with self.assertRaises(ElfError):
            ElfFile(os.path.join(SAMPLES, "src", "foo_v1.c"))


class TestSimulate(unittest.TestCase):
    def test_order_alpha_beta_blocked(self):
        result = simulate_order([ALPHA, BETA], SAMPLES)
        self.assertEqual(result["status"], "blocked")
        block = result["block"]
        self.assertIn("foo@FOO_2.0", block["reason"])
        self.assertEqual(block["hit_path"], FOO_A)
        self.assertEqual([c["name"] for c in block["chain"]],
                         ["plugin_beta.so", "libfoo.so.1"])
        self.assertEqual(block["missing"], [("foo", "FOO_2.0")])

    def test_order_beta_alpha_ok(self):
        result = simulate_order([BETA, ALPHA], SAMPLES)
        self.assertEqual(result["status"], "ok")
        paths = [l["path"] for l in result["loads"] if not l["reused"]]
        self.assertEqual(paths, [BETA, FOO_B, ALPHA])
        # 复用事件存在
        self.assertTrue(any("复用" in e and FOO_B in e
                            for e in result["events"]))
        # 系统库被跳过
        self.assertTrue(any("libc.so.6" in e and "跳过" in e
                            for e in result["events"]))

    def test_both_orders_independent(self):
        result = simulate_both_orders(SAMPLES, "plugin_alpha.so",
                                      "plugin_beta.so")
        self.assertEqual(result["orders"][0]["status"], "blocked")
        self.assertEqual(result["orders"][1]["status"], "ok")
        # 两种顺序各自从空状态开始：可行顺序里装的是 vendor_beta 那份
        ok_paths = [l["path"] for l in result["orders"][1]["loads"]]
        self.assertNotIn(FOO_A, ok_paths)

    def test_missing_runpath_entry(self):
        # 把插件放到没有私有库的布局里：RUNPATH 指向的目录不存在 -> 阻断
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(ALPHA, os.path.join(tmp, "plugin_alpha.so"))
            result = simulate_order([os.path.join(tmp, "plugin_alpha.so")], tmp)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("libfoo.so.1", result["block"]["reason"])
            self.assertIsNone(result["block"]["hit_path"])

    def test_soname_mismatch_blocked(self):
        # 私有库文件名与 SONAME 不一致 -> 阻断，且给出命中路径
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "vendor_alpha"))
            shutil.copy(ALPHA, os.path.join(tmp, "plugin_alpha.so"))
            # 现场构建一个文件名叫 libfoo.so.1、SONAME 却是 libother.so 的库
            src = os.path.join(tmp, "x.c")
            with open(src, "w") as fh:
                fh.write("int foo(void){return 1;}\n")
            subprocess.run(
                ["gcc", "-shared", "-fPIC", "-Wl,--soname,libother.so",
                 "-o", os.path.join(tmp, "vendor_alpha", "libfoo.so.1"), src],
                check=True)
            result = simulate_order([os.path.join(tmp, "plugin_alpha.so")], tmp)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("SONAME", result["block"]["reason"])
            self.assertTrue(result["block"]["hit_path"].endswith("libfoo.so.1"))

    def test_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "sub"))
            shutil.copy(ALPHA, os.path.join(tmp, "sub", "plugin_alpha.so"))
            # RUNPATH 改为 $ORIGIN/.. 仍能留在挂载目录内；这里验证选择器越界
            from app.resolver import resolve_inside
            with self.assertRaises(ElfError):
                resolve_inside(os.path.join(tmp, "sub"), "../plugin_alpha.so")

    def test_expand_origin_forms(self):
        from app.resolver import _expand_origin
        self.assertEqual(_expand_origin("$ORIGIN", "/x"), "/x")
        self.assertEqual(_expand_origin("$ORIGIN/a/b", "/x"), "/x/a/b")
        self.assertEqual(_expand_origin("${ORIGIN}/a", "/x"), "/x/a")
        self.assertIsNone(_expand_origin("/abs/path", "/x"))
        self.assertIsNone(_expand_origin("$OTHER/a", "/x"))

    def test_non_origin_runpath_blocked(self):
        # RUNPATH 只有非 $ORIGIN 条目 -> 找不到库，原因里说明只支持 $ORIGIN
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "libs"))
            shutil.copy(FOO_A, os.path.join(tmp, "libs", "libfoo.so.1"))
            src = os.path.join(tmp, "p.c")
            with open(src, "w") as fh:
                fh.write("extern int foo(void); int p(void){return foo();}\n")
            plugin = os.path.join(tmp, "p.so")
            subprocess.run(
                ["gcc", "-shared", "-fPIC", "-Wl,-rpath,/etc",
                 "-o", plugin, src, os.path.join(tmp, "libs", "libfoo.so.1")],
                check=True)
            result = simulate_order([plugin], tmp)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("$ORIGIN", result["block"]["reason"])
            self.assertIn("/etc", result["block"]["reason"])


class TestService(unittest.TestCase):
    def test_list_plugins(self):
        plugins = list_plugins(SAMPLES)
        self.assertIn("plugin_alpha.so", plugins)
        self.assertIn("plugin_beta.so", plugins)
        self.assertIn(os.path.join("vendor_alpha", "libfoo.so.1"), plugins)

    def test_run_simulation_validation(self):
        with self.assertRaises(ValueError):
            run_simulation(SAMPLES, "plugin_alpha.so", "plugin_alpha.so")
        with self.assertRaises(ValueError):
            run_simulation(SAMPLES, "plugin_alpha.so", "nope.so")
        result = run_simulation(SAMPLES, "plugin_alpha.so", "plugin_beta.so")
        self.assertEqual(len(result["orders"]), 2)


class TestBatchProviderCheck(unittest.TestCase):
    """批量核查方法 check_provided_pairs 与逐条核查口径一致。"""

    def test_matches_per_symbol_semantics(self):
        provider = ElfFile(FOO_B)  # 提供 foo@FOO_1.0 与 foo@@FOO_2.0
        # 一次调用同时核对两个版本：只遍历一遍符号表
        missing = provider.check_provided_pairs(
            {"foo": {"FOO_1.0", "FOO_2.0", "FOO_9.9"}})
        self.assertEqual(missing, {("foo", "FOO_9.9")})
        # 与旧的逐条 API 口径一致
        for ver in ("FOO_1.0", "FOO_2.0"):
            self.assertIn(ver, provider.strong_provided_versions("foo"))

    def test_all_missing_and_empty(self):
        provider = ElfFile(FOO_A)  # 只提供 foo@FOO_1.0
        self.assertEqual(
            provider.check_provided_pairs({"foo": {"FOO_2.0"}}),
            {("foo", "FOO_2.0")})
        # 空核查集合不产生缺失，也无需扫描
        self.assertEqual(provider.check_provided_pairs({}), set())

    def test_unknown_symbol_missing(self):
        provider = ElfFile(FOO_B)
        missing = provider.check_provided_pairs({"nope": {"FOO_1.0"}})
        self.assertEqual(missing, {("nope", "FOO_1.0")})


class TestScanOncePerOrder(unittest.TestCase):
    """每种顺序里，每个实际装入的私有提供方库符号表最多遍历一遍。"""

    def test_sample_scan_counts(self):
        result = simulate_both_orders(SAMPLES, "plugin_alpha.so",
                                      "plugin_beta.so")
        for order in result["orders"]:
            # 只有一个提供方库 libfoo.so.1，被两条边引用：整表仍只扫一遍
            self.assertEqual(order["provider_scan_count"], 1)
            self.assertEqual(len(order["provider_scans"]), 1)
            scan = order["provider_scans"][0]
            self.assertEqual(scan["scan_count"], 1)
            self.assertEqual(scan["name"], "libfoo.so.1")
            # 核查索引里登记了两个待核版本对（FOO_1.0 与 FOO_2.0）
            self.assertEqual(scan["checked_pairs"], 2)

    def test_dynsym_traversed_once_even_with_two_referrers(self):
        # 直接计数底层批量核查的调用：同一提供方在一种顺序里只能被扫一次
        with mock.patch.object(
                ElfFile, "check_provided_pairs",
                autospec=True,
                side_effect=ElfFile.check_provided_pairs) as spy:
            result = simulate_both_orders(
                SAMPLES, "plugin_alpha.so", "plugin_beta.so")
        # 两个顺序各自独立推演，每个顺序里唯一的提供方库只扫一遍 -> 共 2 次
        self.assertEqual(spy.call_count, 2)
        for order in result["orders"]:
            scanned = {s["path"] for s in order["provider_scans"]}
            self.assertEqual(len(scanned), order["provider_scan_count"])
            self.assertLessEqual(order["provider_scan_count"],
                                 len([l for l in order["loads"]
                                      if not l["is_root"]
                                      and not l["reused"]]))

    def test_blocked_order_still_reports_scan_stats(self):
        result = simulate_order([ALPHA, BETA], SAMPLES)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["provider_scan_count"], 1)
        scan = result["provider_scans"][0]
        self.assertEqual(scan["missing_pairs"], [("foo", "FOO_2.0")])


def _write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


class TestFirstBlockOrdering(unittest.TestCase):
    """前一条依赖缺符号、后一条缺库时，按原核查顺序报告首个失败。"""

    def _build_fixture(self, tmp):
        """构造一个插件，NEEDED 顺序可在 libbase / libmiss 间切换。

        链接期用同时带 BASE_1.0/BASE_2.0 的 libbase 满足 .symver 版本节点；
        链接完成后把 libbase 替换为只提供 BASE_1.0 的版本，从而在推演期
        构成对 BASE_2.0 的缺符号；libmiss.so.1 链接后删除（缺库）。
        """
        vendor = os.path.join(tmp, "vendor")
        os.mkdir(vendor)
        base_lib = os.path.join(vendor, "libbase.so.1")
        # 链接期版本：BASE_1.0 + BASE_2.0
        _write(os.path.join(tmp, "base2.map"),
               "BASE_1.0 { global: base_fn; local: *; };\n"
               "BASE_2.0 { global: base_fn_v2; } BASE_1.0;\n")
        _write(os.path.join(tmp, "base2.c"),
               "int base_fn(void){return 1;}\n"
               "int base_fn_v2(void){return 2;}\n"
               "__asm__(\".symver base_fn,base_fn@BASE_1.0\");\n"
               "__asm__(\".symver base_fn_v2,base_fn@@BASE_2.0\");\n")
        subprocess.run(
            ["gcc", "-shared", "-fPIC", "-O2",
             "-Wl,--soname,libbase.so.1",
             "-Wl,--version-script=" + os.path.join(tmp, "base2.map"),
             "-o", base_lib, os.path.join(tmp, "base2.c")], check=True)
        _write(os.path.join(tmp, "miss.c"),
               "int missing_lib_fn(void){return 0;}\n")
        miss_lib = os.path.join(vendor, "libmiss.so.1")
        subprocess.run(
            ["gcc", "-shared", "-fPIC", "-O2",
             "-Wl,--soname,libmiss.so.1",
             "-o", miss_lib, os.path.join(tmp, "miss.c")], check=True)
        plugin_src = os.path.join(tmp, "p.c")
        _write(plugin_src,
               "extern int base_fn_v2(void);\n"
               "__asm__(\".symver base_fn_v2,base_fn@BASE_2.0\");\n"
               "extern int missing_lib_fn(void);\n"
               "int p(void){return base_fn_v2()+missing_lib_fn();}\n")

        def downgrade_base():
            """链接后把提供方替换为只有 BASE_1.0 的库（制造缺符号）。"""
            _write(os.path.join(tmp, "base1.map"),
                   "BASE_1.0 { global: base_fn; local: *; };\n")
            _write(os.path.join(tmp, "base1.c"),
                   "int base_fn(void){return 1;}\n")
            subprocess.run(
                ["gcc", "-shared", "-fPIC", "-O2",
                 "-Wl,--soname,libbase.so.1",
                 "-Wl,--version-script=" + os.path.join(tmp, "base1.map"),
                 "-o", base_lib, os.path.join(tmp, "base1.c")], check=True)

        return base_lib, miss_lib, plugin_src, downgrade_base

    def test_missing_symbol_before_missing_lib_reports_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_lib, miss_lib, plugin_src, downgrade = self._build_fixture(tmp)
            plugin = os.path.join(tmp, "p.so")
            # NEEDED：libbase（缺符号）在前，libmiss（缺库）在后
            subprocess.run(
                ["gcc", "-shared", "-fPIC", "-O2",
                 "-Wl,-rpath,$ORIGIN/vendor", "-o", plugin, plugin_src,
                 base_lib, miss_lib], check=True)
            downgrade()
            os.remove(miss_lib)  # 制造缺库
            result = simulate_order([plugin], tmp)
            self.assertEqual(result["status"], "blocked")
            block = result["block"]
            # 首个失败是前一条边的缺符号，而不是后一条边的缺库
            self.assertEqual(block["edge_seq"], 1)
            self.assertEqual([c["name"] for c in block["chain"]],
                             ["p.so", "libbase.so.1"])
            self.assertIn("BASE_2.0", block["reason"])
            self.assertEqual(block["hit_path"], base_lib)

    def test_missing_lib_before_missing_symbol_reports_missing_lib(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_lib, miss_lib, plugin_src, downgrade = self._build_fixture(tmp)
            plugin = os.path.join(tmp, "p.so")
            # NEEDED：libmiss（缺库）在前，libbase（缺符号）在后
            subprocess.run(
                ["gcc", "-shared", "-fPIC", "-O2",
                 "-Wl,-rpath,$ORIGIN/vendor", "-o", plugin, plugin_src,
                 miss_lib, base_lib], check=True)
            downgrade()
            os.remove(miss_lib)
            result = simulate_order([plugin], tmp)
            self.assertEqual(result["status"], "blocked")
            block = result["block"]
            self.assertEqual(block["edge_seq"], 1)
            self.assertEqual([c["name"] for c in block["chain"]],
                             ["p.so", "libmiss.so.1"])
            self.assertIn("libmiss.so.1", block["reason"])
            self.assertIsNone(block["hit_path"])
            # 后一条边的库仍被装入并核查（一次扫描），只是不作为首个阻断
            self.assertEqual(result["provider_scan_count"], 1)
            self.assertEqual(
                result["provider_scans"][0]["missing_pairs"],
                [("base_fn", "BASE_2.0")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
