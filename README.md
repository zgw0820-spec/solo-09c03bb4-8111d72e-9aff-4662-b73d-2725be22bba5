# 采集代理 · 双插件装入顺序推演器

发布员在网页上从挂载目录选择两个 x86-64 ELF 插件，系统**不执行任何插件**，
而是从“尚未装入任何私有库”的空状态出发，分别推演两种装入顺序在同一
Linux 进程语义下的结果。

## 推演规则（与需求对应）

| 规则 | 实现位置 |
| --- | --- |
| 不执行插件，只读 ELF 元数据 | `app/elfparse.py`（纯解析，无 dlopen） |
| 两种顺序各自从空状态推演 | `resolver.simulate_both_orders` |
| 按 DT_NEEDED 顺序逐层（广度优先）找库 | `resolver.simulate_order` 中的队列 |
| 引用方用自己的 DT_RUNPATH 找首个同名文件 | `resolver._find_in_runpath` |
| 同名私有库一旦装入，后续引用只能复用 | `simulate_order` 的 `loaded` 表 |
| 只支持 `$ORIGIN` 相对目录（含 `${ORIGIN}`） | `resolver._expand_origin` |
| 只支持文件名与 SONAME 一致的私有库 | `resolver._load_private` |
| 固定名单系统库跳过核查 | `resolver.SYSTEM_LIBS` |
| 每条私有依赖核对 GNU 版本化强符号 | `resolver` 核查索引 + `ElfFile.check_provided_pairs` |
| **每种顺序每个私有提供方库符号表最多遍历一遍** | `ElfFile.check_provided_pairs`（批量核查）+ `_PendingCheck` 核查索引 |
| 可行顺序列出实际库路径 | 结果 `loads`（页面与 CLI 均展示） |
| 阻断时按原核查顺序给出首个失败的依赖链、原因、命中路径 | 结果 `block`（chain / reason / hit_path / edge_seq） |

版本化强符号的核对口径：对每条私有依赖边，取引用方 `.gnu.version_r` 中
`vn_file` 等于该库名的条目，找出引用方未定义的强符号（bind=GLOBAL）及其
要求的版本；提供方必须存在同名、已定义、GLOBAL 且版本名一致的符号
（`.gnu.version_d`），否则阻断。

## 符号核查优化（避免反复扫表）

旧实现对每条依赖边的每个待核符号都调用一次
`ElfFile.strong_provided_versions`，每次都从头遍历提供方的整个 `.dynsym`；
提供方导出符号多、或被多个引用方复用时，同一张动态符号表会被反复扫描。

现在的做法（`app/resolver.py` + `app/elfparse.py`）：

1. **核查索引只记录待核对**：BFS 推演时，把每条私有依赖边要求的
   `(符号, 版本)` 对登记到该提供方库名下的 `_PendingCheck`，并记下引用边
   在原核查顺序（BFS 出队顺序）中的序号；不预建全量导出索引。
2. **每库每顺序最多扫一遍**：一种顺序推演完后，对每个实际装入的私有
   提供方库只调用一次 `ElfFile.check_provided_pairs`，整表只遍历这一遍，
   用符号名索引待核集合、命中即移除，一次性回答所有引用方的所有待核对。
3. **同名库仍按该顺序实际装入的路径核对**：核查索引挂在“本顺序实际装入的
   那份库”上，两种顺序各自从空状态独立推演、各自统计，互不串用。
4. **首个阻断按原核查顺序报告**：每条私有依赖边有递增序号；找库/装入类
   失败（缺库、SONAME 不一致等）只登记候选并跳过该分支而不终止推演，
   符号核查后在“缺库”与“缺符号”候选间取边序号最小者。因此
   “前一条依赖缺符号、后一条缺库”时报告前一条的缺符号链，反之亦然。
5. 结果里新增 `provider_scan_count` / `provider_scans`，网页与 CLI 均显示
   每个提供方库的扫描次数、待核对数与缺失对。

## 运行

```bash
docker compose up --build
# 打开 http://localhost:8080
```

compose 把 `./samples` 只读挂载到容器 `/data/plugins`。换成自己的插件目录：
修改 `docker-compose.yml` 的 volume，或 `docker run -v /path:/data/plugins:ro`。

无 Docker 时可用 CLI（纯标准库，无需 Flask）：

```bash
python3 app/cli.py --dir samples plugin_alpha.so plugin_beta.so
python3 app/cli.py --dir samples --list     # 只列出可选插件
```

## 自带样例（两种不同结果）

`samples/` 下是一对菱形依赖（源码与构建脚本在 `samples/src/`，
`sh samples/src/build.sh` 可重建）：

- `plugin_alpha.so`：`RUNPATH=$ORIGIN/vendor_alpha`，要求 `foo@FOO_1.0`
- `plugin_beta.so`：`RUNPATH=$ORIGIN/vendor_beta`，要求 `foo@FOO_2.0`
- `vendor_alpha/libfoo.so.1`：只提供 `foo@@FOO_1.0`
- `vendor_beta/libfoo.so.1`：同时提供 `foo@FOO_1.0` 与 `foo@@FOO_2.0`

结果：

- **alpha → beta：阻断**。alpha 的 `vendor_alpha/libfoo.so.1` 先装入，
  beta 只能复用这份，而它缺少 `foo@FOO_2.0`。页面给出依赖链
  `plugin_beta.so → libfoo.so.1`、原因与已命中库路径。
- **beta → alpha：可行**。`vendor_beta/libfoo.so.1` 先装入，同时满足两个
  插件，页面列出实际装入的库路径。

两个插件都还依赖 `libc.so.6`，用于演示固定名单系统库的跳过。

## 交付状态

### 已做

- 纯 Python ELF64 解析：DT_NEEDED / DT_RUNPATH / DT_SONAME、.dynsym、
  GNU 版本三节（versym / verdef / verneed）。
- 上表全部推演规则；阻断报告含依赖链、原因、命中路径。
- **符号核查优化**：核查索引只登记待核 `(符号, 版本)` 对；每种顺序里每个
  实际装入的私有提供方库的动态符号表最多遍历一遍（`check_provided_pairs`）；
  同名库按该顺序实际装入的路径核对；首个阻断按原核查顺序（边序号）报告，
  缺符号与缺库同时存在时取最早边。
- Flask 网页（选择两个插件 → 同页展示两种顺序结果，含每个提供方库的
  符号表扫描次数 / 待核对数 / 缺失对）+ 无依赖 CLI（同样打印扫描统计）。
- 自带 ELF 样例（源码可重建）、Dockerfile、docker-compose.yml。
- 安全约束：插件选择与 `$ORIGIN` 展开均限制在挂载目录内；解析有大小上限。

### 已验证（本机实跑）

- `tests/test_core.py` 共 24 项单元测试全部通过（原 16 项 + 新增 8 项）：
  动态标签解析、版本需求 / 提供提取、两种顺序结果、系统库跳过、同名复用、
  SONAME 不一致阻断、非 `$ORIGIN` RUNPATH 阻断、路径越界拒绝，以及新增的
  批量核查口径、单库每顺序只扫一遍、扫描统计、首个阻断优先级
  （缺符号在前报符号、缺库在前报缺库）。
- 自带样例双顺序实测：两个顺序都只有 1 个提供方库 `libfoo.so.1`、被两条
  引用边复用、各登记 2 个待核对，`provider_scan_count` 均为 **1**；
  alpha→beta 阻断（缺 `foo@FOO_2.0`，命中 vendor_alpha 路径），
  beta→alpha 可行（列出三条实际装入路径）。
- 强化场景实测：3 个插件共用同一提供方、3 条引用边、3 个待核对，
  `check_provided_pairs` 仅调用 1 次（旧逐条口径等价为 3 次全表扫描）。
- Flask（3.0.3，与 Dockerfile 约束同系列）本机以与容器相同的
  `python -m app.webapp` 入口启动，`GET /` 与 `POST /simulate` 均 200，
  页面正确渲染两个顺序的扫描次数（均为 1）与缺失对表格。
- 样例的 DT_NEEDED / RUNPATH / SONAME / 版本符号经 `readelf` 交叉核对。
- 全部 Python 模块通过 `py_compile`，Jinja 模板语法校验通过。

### 未完成 / 未验证

- 本机没有 docker / 容器运行时，`docker compose up --build` 的容器内
  端到端流程未在本环境实跑；Dockerfile 与 compose 未改动，已用同版本
  Flask、同入口、同 `MOUNT_DIR` 在本机等价自测网页渲染与 POST 流程。
- 未版本化的未定义符号不核查（需求只要求版本化强符号）；弱符号按
  “非强符号”跳过。
- 仅支持 ELF64 小端 x86-64；DT_RPATH、`$LIB`/`$PLATFORM` 等动态标记、
  符号版本继承（verdef 多 aux）等罕见情形未支持。
- Web 用 Flask 内置服务器，仅供演示，非生产部署形态。
