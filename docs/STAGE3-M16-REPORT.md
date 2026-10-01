# STAGE3-M16 报告 — 真实数据矩阵

> 阶段：Stage 3 / 里程碑 **M16**。前置：M0–M15 全部通过（`afdea97`）。
> **完成后停下，等复审。未开始 M17。**
>
> ★ **本里程碑最重要的结果在 §4，不在 §2。** 三类既有数据的判据 B 全部对上了；
> 而判据 D 的第一次真实检验，在一份真实 XSD 上同时找到**两个缺口**。按判据 E，
> 两者都只固定、不修。

---

## §1 ★ 选第四类的论证（判据 A 的两条）

判据 A 要求先说清两件事，说不出就不要加。

### 1.1 它与现有三类在什么结构维度上不同

现有三类覆盖的维度，逐条量过（见 `REAL-WORLD-VALIDATION.md`）：

| | MediaWiki | PubMed | ERP（生成） | **FHIR R4** |
|---|---|---|---|---|
| 命名空间 | ✓ 默认命名空间 | ✗ | ✗ | ✓ 默认命名空间 |
| 深嵌套 | ✓ 3 层 | ✓ 4 层以上 | ✗ | ✓ |
| 元素文本里的标量 | **全部** | **全部** | **全部** | **一个都没有** |
| 标量在 `value=` 属性里 | ✗ | ✗ | ✗ | ✓ **304 个元素里 157 个** |
| **随附真实 XSD** | ✗ | ✗ | ✗ | ✓ **150 个文件、296 条 include 边** |
| 记录内同名兄弟形状不同 | 部分 | 部分 | ✗ | ✓ 三个 `<name>` 两两不同 |
| 记录内的外部命名空间子树 | ✗ | ✗ | ✗ | ✓ `<div xmlns=".../xhtml">` |
| 稀疏记录（`minOccurs=0`） | ✗ | ✗ | ✗ | ✓ |

★ **未覆盖的那一维是「标量在属性里」和「随附 schema」，而这两维都不是三个既有数据能顺手长出来的。**
Wikipedia 的命名空间在**数据**上，不在 **schema** 上——本项目此前从未被指向过一份真实的 XSD。

### 1.2 为什么它可能暴露现有测试碰不到的问题

**这一条不是推测，是已发生的事实。** 三条既有数据路径都不经过 `src/gigaxml/xsd.py`，
而判据 D 把真实 XSD 指进去之后：

- **M1 的沙箱在真实 schema 上跑通了**（150 文件 / 296 条 include 边，2.5 s），
  说明既有 fixture 至少没有把真实 include 图的规模写小；
- **同时 `record_field_types` 拒绝了全部 146 个全局元素**，因为它用裸局部名查一个
  按 Clark 记法建索引的 schema；
- **同时 `xs:import` 的拒绝被降级成 warning**，schema 照样编译成功。

★ **后两条正是「合成数据碰不到」的东西**：M1–M15 写过的每一个 XSD fixture 都没有
target namespace，且导入的命名空间 `xmlschema` 自己没有副本。这两条假设都承重，
都对真实 schema 为假，所以守卫量的是 fixture 而不是世界。

### 1.3 为什么是 FHIR 而不是别的

判据 §3 给的候选方向与实测：

| 候选 | 实测 |
|---|---|
| Stack Exchange dump | **不可达**：`dumps.stackexchange.com` → HTTP 403 |
| OpenStreetMap 子集 | **不可达**：`api.openstreetmap.org` / `download.geofabrik.de` → 连接超时 |
| **HL7 FHIR R4** | **可达**，且**自带真实多文件 XSD** |

★ 判据 §3 说候选方向"不强制"。我先按可达性把前两个排除，**剩下的选择只有一个能同时
满足判据 A 的第一维和判据 D 的前提**。这不是「凑数」，也不是「下载便宜所以加」——
`REAL-WORLD-VALIDATION.md` 里那张表每一行都能独立复算。

### 1.4 数据量小，是明说的

判据 §4 要求：不许截取子集冒充全量，若截取必须写明。**FHIR 没有更大的公开 XML dump 可下**——
HL7 以逐个示例文件的形式发布。所以：

- 语料是**具名的 5 个文件**，写在 `examples/fhir/fetch.py` 的 `EXAMPLES` 里并记进 `fhir.json`；
- `patient-example.xml` 的期望记录数就是 **1**，原因写在 `REAL-WORLD-VALIDATION.md` §4b；
- 判据 A 自己说的是「判据是**结构不同**，不是行数多」，所以小而异是合规的。

---

## §2 判据 B：四个数据集的 B 表

完整表在仓库根的 **`REAL-WORLD-VALIDATION.md`**（按简报 §5 的要求放在根目录并入库）。
此处只列判据 B 的核心两列：

| 数据集 | 期望记录数 | 实际记录数 | 两者的来源 |
|---|---|---|---|
| Wikipedia | 560,605 | **560,605** ✓ | `inspect` 计数 vs `extract` 行数 |
| PubMed | 4,989 | **4,989** ✓ | `inspect --max-paths 30` vs `extract` 行数 |
| ERP products | 145,600 | **145,600** ✓ | **生成器 manifest** vs `extract` 行数 |
| ERP orders | 48,533 | **48,533** ✓ | README 记录 vs `extract` 行数 |
| FHIR | 1 | **1** ✓ | `inspect` 计数 vs `extract` 行数 |

★ **五项全部对上，且没有一项是同义反复。** Wikipedia / PubMed / FHIR 的"期望"由
`inspect` 数——它只统计元素出现次数；"实际"由 `extract` 写——它写出的是行。
ERP products 的"期望"来自**生成器自己的 manifest**，是第三次独立测量。
**ERP orders 是最弱的一对**：它的 48,533 来自文档而非 manifest，所以这是一个被核对的
主张，不是两次测量互相印证。`REAL-WORLD-VALIDATION.md` 里明写了这一条，没有把它包装得更强。

**哈希（三组，互相独立可复算）**

| | 归档 | XML 本体 |
|---|---|---|
| Wikipedia | `6832fd10…` (356,186,307 B) | `2575aba6…` (1,696,517,417 B) |
| PubMed | `d241061c…` (13,837,691 B) | **无**——直接读 `.gz`，没有解压产物 |
| ERP | 不适用（生成） | `f8feb02d…` (52,708,114 B)，与 manifest 比对 |
| FHIR | 无单一归档 → `set_sha256` + **150 个文件各自的 sha256** | 实例文件 5 个 sha256 |

★ **Wikipedia 的两个哈希与 2026-09-27 记录的逐字节相同**，dump 在这五天里没重生成，
但 `fetched_at` 往前走了一天。两条事实分开记，不合并。
★ **PubMed 本轮没有重新下载**，是校验盘上文件与记录哈希吻合（下载日期 2026-09-30，
校验日期 2026-10-02）。重新下载会取到另一个日子并改掉 README 记录的数字，没有收益。

---

## §3 判据 C：体积与清理

**下载前先报体积**（实测 HEAD，不是估计）：

| | 归档 | 解压后 | 瞬时占用 |
|---|---|---|---|
| Wikipedia | 356,186,307 B | 1,696,517,417 B | ~2.05 GB |
| PubMed | 13,837,691 B | 无解压 | 14 MB |
| FHIR schema | 150 文件 / 3,077,151 B | — | ~3 MB |
| FHIR 实例 | 14,124 B | — | — |

盘上 `data/` 峰值 **2.2 GB**，`out/` 92 MB，起始可用 448 GB。

★ **清理前先说清楚哪些不是我的。** `data/` 里的 `s10.xml` (11 MB) / `s100.xml` (101 MB) /
`s400.xml` (404 MB) 是 **2026-10-01 02:45 更早里程碑留下的**，本里程碑既没有生成也没有使用它们。
合计 **516 MB**。**我没有删**——删别人产生的产物不是本里程碑该顺手做的事，
交给复审判定。见 §6-6。

**已删（本里程碑自己产生的）**

| 产物 | 体积 | 为什么可删 |
|---|---|---|
| `data/wikipedia-articles.xml` | 1,696,517,417 B | `benchmarks/datasets/fetch.py` 可从零重建，两个哈希已记录 |
| `data/hl7.fhir.r4.core-4.0.1.tgz` | 4,531,911 B | 只是探针，见下 |
| `.scratch/m16/` | ~3.5 MB | 排演脚本与被 CRLF 改过的 schema 副本，见 §7-2 |

★ **被删的 tgz 记在这里，是为了让一条论断仍可复核**：HL7 的官方 FHIR R4 包
（`https://packages2.fhir.org/packages/hl7.fhir.r4.core/4.0.1`，
**4,531,911 B，sha256 `b090bf929e1f665cf2c91583720849695bc38d2892a7c5037c56cb00817fb091`**）
解出 4,739 个文件，**0 个 `.xml`、0 个 `.xsd`、4,736 个 `.json`**。
**FHIR 的官方分发是 JSON-first，它的 XML schema 集是单独发布的**——
这就是为什么 fetch 脚本爬的是 `hl7.org/fhir/R4/` 而不是这个包。留下 4.4 MB
只为了让这个结论可查，不值；哈希记下来，需要时重新下一遍即可复算。

清理后 `data/` 从 **2.2 GB 降到 531 MB**，其中 516 MB 是 §6-6 待裁决的那批。

---

## §4 ★ 判据 D：沙箱第一次面对真实 XSD

`data/fhir/xsd/` 是 `examples/fhir/fetch.py` 按**传递闭包**爬下来的 150 个真实 schema 文件。
测得 **296 条 `xs:include` / `xs:import` 边**（293 include + 3 import），
**远程 `schemaLocation` 0 条**、`../` 越界 0 条、带 DOCTYPE 的文件 0 个。

| 编号 | 测试 | 期望 | 实测 | |
|---|---|---|---|---|
| **C0** | **对照：未改动的真实集合** | 编译通过 | **通过**，146 个全局元素，2.5 s | ✓ |
| D0 | 真实 150 文件集在 `sandbox`+`always` 下编译 | 编译通过 | **通过** | ✓ |
| D1 | 真正不存在的元素名 | `SchemaError` | `SchemaError` ×3 | ✓ |
| **D2** | ★ **schema 确实声明了的元素名** | 读得到 | **`SchemaError`，146/146 全中** | ✗ **缺口** |
| D3 | 远程 `schemaLocation` | 拒绝 | **0 次出网**，但**编译成功** | ⚠ |
| D4 | `../` 越界 | 拒绝 | 文件**未读**，但**编译成功** | ⚠ |

### 4.1 D0/D1：真实 include 图通过，守卫也没坏

150 个文件、296 条边，2.5 s 编译完成，**沙箱一次都没有误报**。真正不存在的名字
（`/DefinitelyNotAResource`、`/patient`、`/PATIENT`）仍然被拒。

★ 这一条的重要之处在于它排除了"守卫其实一直是坏的"这个解释：D1 证明同一个守卫
在名字真的不存在时仍然正确工作，D2 的失败不是守卫整体失效，而是它对**存在**的名字
给出了**不存在**的答案。

### 4.2 ★ D2：缺口一 —— 带 target namespace 的 schema 一律读不到

根因量到了一行：

```
xmlschema 按 Clark 记法建索引：'{http://hl7.org/fhir}Patient'
schema.get_element("Patient")                 -> None
schema.get_element("{http://hl7.org/fhir}Patient") -> XsdElement(name='Patient', occurs=[1,1])
```

`record_field_types` 传的是**裸局部名**，所以对**任何**带 target namespace 的 schema 都返回 `None`。

★ **这个错误信息自己就是证据**：它一边说"没有声明名为 Patient 的顶层元素"，
一边在同一句里把 `Patient` 列进"它有"的名单。任何人都不需要相信我的测量——
错误信息自证。

**固定**：`tests/golden/test_known_defects.py::test_a_known_defect_a_namespaced_schema_reports_declaring_the_element_it_could_not_find`。
fixture 里两份 schema **唯一差异是命名空间**，且都先断言它们**本身是合法 XSD**——
没有这个断言，一个"因为别的原因坏了的 fixture"会给出同样的红。

### 4.3 ★ D3/D4：缺口二 —— "读"拦住了，"报出来"没拦住

判据 D 问的是"若引用了远程 schemaLocation，那正是 M1 要拦的，**报告它，并确认拦得住**"。
实测答案要分成两半：

| 维度 | 结果 |
|---|---|
| 有没有真的发起网络连接 | **0 次**（在 `_open_schema` 期间挂钩 `socket.connect` 计数） |
| 有没有读到沙箱外的文件 | **没有**（D4 的越界文件确实存在，但没被打开） |
| 用户知不知道被拦了 | **不知道**——schema **编译成功**，只有 stderr 上的 `XMLSchemaImportWarning` |

★ 触发条件不是"越界"，而是**被拒/缺失的那个命名空间，`xmlschema` 自己恰好有副本**。
FHIR 导入的 `http://www.w3.org/XML/1998/namespace` 正是其一。
M1 的每个 fixture 导入的都是 `xmlschema` 没有的命名空间，所以没有副本可退，拦截就终止了编译。

★ 最能说明问题的一次测量不是 D3/D4，而是**把 `xml.xsd` 直接删掉**：
**没有任何变异、没有越界、没有任何攻击性**，schema 照样编译，146 个全局元素，2 条 warning，
`Patient` 依然是 13 个子元素。**用户删错一个文件，得到的是一次成功的运行。**

这正是 `SECURITY.md` 里写着"修复前不只是会出网，而是**出网失败被静默降级成 Warning，
schema 照样编译成功返回**"的那个形状。`xs:import` 这条路上，"读"的部分仍然成立，
**"报出来"的部分回来了**。

**固定**：`…::test_a_known_defect_a_blocked_import_still_compiles_and_only_warns`。
**两半都断言，缺一不可**：`leakMarker` 不在 schema 里（证明没读），以及**没有抛异常**
（证明没报）。只断言前一半的测试，在旧行为下也会绿。
测试里另有一个 `inside.xsd` 对照组，它必须**把 marker 带进来**——
否则"什么都没读"这个策略也会满足"leakMarker 不在"。

---

## §5 ★ 判据 E：缺口清单

判据 E：遇到真缺口，**固定 + 停下报告，不许顺手修**。本里程碑找到两个，两个都固定了，**都没修**。

| # | 缺口 | 严重度 | 固定位置 |
|---|---|---|---|
| **E-1** | `record_field_types` 拒绝一切带 target namespace 的 schema（146/146） | 功能性；错误信息自相矛盾，把用户引向错误的排查方向 | `tests/golden/test_known_defects.py`（9 passed） |
| **E-2** | 被拒或缺失的 `xs:import` 只降级为 warning，schema 编译成功 | 安全报告性；**读取边界仍成立**，但用户不知情 | 同上 |

★ **E-2 的严重度我压着说**：它不是出网、不是越界读取、不是任意文件读。
在 FHIR 这套真实 schema 上，被替代掉的 `xml.xsd` 内容与原来**完全相同**（实测：
`Patient` 都是 13 个子元素）。所以在今天，E-2 的后果是**一个沉默的错误答案**，
不是一次数据泄漏。但它落在 `SECURITY.md` 明确宣称已修复的那句话上，
所以它该由复审决定按什么严重度记录，不该由我决定。

**没有改任何实现代码。** `src/`、`pyproject.toml`、`packaging/`、`benchmarks/` 均无改动。

---

## §6 未验证 / 需要 push 的项

| # | 项 | 说明 |
|---|---|---|
| 1 | **四个数据集在 CI runner 上的结果** | 未 push，CI 一次没跑。本机结果不代表 runner |
| 2 | **任何人 clone 后能否复现 FHIR 那 150 个文件** | 依赖 `hl7.org` 可达；本机可达，**未在 runner 上验证** |
| 3 | **三个数据集的 URL 长期可达** | 本轮实测均 HTTP 200；Wikipedia 与 PubMed 都是滚动窗口，会变 |
| 4 | **CI 上 `data/` 会被拉到多大** | 工作流未改；判据 C 只要求工作区不大，CI 未涉及 |
| 5 | **`.scratch/m16/` 下的探索脚本未入库** | 它们是排演工具，不是守卫。要长期复现应转成 `tools/` 或测试 |
| 6 | **`data/s10.xml` / `s100.xml` / `s400.xml` 共 516 MB 未删** | 更早里程碑产生，非本里程碑产物，**未擅自删除**，请裁决 |
| 7 | **判据 D 在 CI 上没有对应 job** | 本里程碑未改工作流；这两个固定测试不需要真实 schema，**在 CI 上是绿的** |

★ **第 7 条要读清楚**：两个固定测试用的是仓库里的小 fixture，**不依赖下载**，
所以 CI 上会照常运行并守住当前行为。真实 150 文件集的验证是本报告的一次性测量，
不是 CI 里的常驻检查。

---

## §7 我自己错的六处

| # | 我以为 | 实测 |
|---|---|---|
| 1 | 爬到 `fhir-all.xsd` 声明的 146 个 include 就够了 | **不够。** `fhir-base.xsd` / `fhir-xhtml.xsd` / `xml.xsd` 只在传递闭包里。首次编译报的是"缺 base type"——**一个把"少下了文件"说成"schema 坏了"的错误信息** |
| 2 | scratch 里量到的沙箱数字可以代表真实集合 | **不能。** 我用 `Path.write_text` 写入口文件，Windows 把 33 个 `\n` 翻成 `\r\n`；HL7 原文件本身是**混合行尾**（146 CRLF + 33 LF）。**150 个文件里唯一被我改动的恰好是入口。** 全部沙箱数字重测 |
| 3 | D3/D4 应该被 `refused-by-policy` 拒绝 | **两次都编译成功了。** 我第一次写下 `expect=refused-by-policy` 时，是从 `SECURITY.md` 推的，不是量的 |
| 4 | D3 会有 warning | **没有 warning，0 次连接。** xmlschema 自带 `xml.xsd` 副本，命名空间被直接满足 |
| 5 | 我写的 `namespaced.xsd` fixture 有效 | **它本身不合法**（缺 `elementFormDefault="qualified"` 与前缀绑定）。一个坏 fixture 会给出同样的红，却证明不了任何事——`tests/security/conftest.py` 正是为这个陷阱写的 |
| 6 | `schema.elements` 迭代出的是元素对象 | **是 str**（`NamespaceView.__iter__` 只 yield 不带前缀的名字）。我一度以为 gigaxml 在用 `.name`，实际是 `_local(e)`；以 `sed` 读到的文件与 `inspect.getsource` 为准 |
| 7 | 我新写的 `fetch.py` 守卫是承重的 | ★ **两条不是。**(a) 体积下限会触发，却把截断文件**留在盘上**——`pubmed/fetch.py` 同一处是 `unlink` 后再抛，我只借了它的哈希与 TLS，没借这条；而 `crawl()` 只下载**缺失**的文件，所以残留件会被永远信任。(b) `verify()` 把 JSON 里的条目**不排序**直接喂给 `set_digest`，同一批字节换个落盘顺序就会报"校验失败"。两条已修并复验 |

★ **第 7 条是 M15 教训的第四次，也是第一次落在本里程碑自己的交付物上。**
M15 立的规矩是"新守卫先变异验证能不能红"，M16 我在 `fetch.py` 里写了三个守卫却直接交付；
量出来两个是空的。修完的复验：

```
G1  floor fired and cleaned up (file present: False)          修前 True
G2  crawl refused: fhir-all.xsd is already present but is 112 bytes, under the 512-byte floor
G3  same bytes, reversed order -> same digest   (修前 False)
G3  a file's bytes changed -> digest changes
```

★ **第 7 条与第 1、2 条是同一个反射**：M15 之后我仍然会先写完再量。
差别只在于这次量的是自己刚写的代码，所以它没有变成一个"别人要复审才发现"的缺口。

★ **第 2 条是 M15 那个 CRLF 陷阱的第三次**：`.gitattributes`（M14）、编码 bug（M5）、
现在这次。**形状完全一样——代码在本机是对的，在别处不是。**

★ **第 1、2 条是同一个错误的两面**：我报告了"146/146 下载成功"，
那句话对**所量的那个集合**是真的，但那个集合本身是错的。
"全部成功"和"量对了东西"是两件事，后者才决定结论。

---

## §8 本次重跑的数字

```
$ pytest -q --cov=src/gigaxml --cov-report=term-missing --cov-fail-under=95 \
      --junit-xml=ci-report.xml tests/unit tests/integration tests/golden \
      tests/security tests/property
1800 passed in 237.86s (0:03:57)
Required test coverage of 95% reached. Total coverage: 95.66%

$ tools/ci_selfcheck.py report --min-tests 1800 --max-skips 0 ci-report.xml
  total 1800 | passed 1800 | skipped 0 | failed or errored 0
  report: the run was real, and it ran the platform-specific tests.   rc=0

$ tools/ci_selfcheck.py shape
  shape: clean. 13 jobs, 2 workflow files, 5 shared test directories.  rc=0

$ ruff check .            → All checks passed!                       rc=0
$ ruff format --check .   → 219 files already formatted              rc=0
```

| 项 | 数值 |
|---|---|
| 全树 | **1800 passed**（1798 + 新增 2 个固定测试），skipped 0 / failed 0 |
| 覆盖率 | **95.66%** |
| Wikipedia 期望 / 实际 | 560,605 / **560,605**（峰值 35.44 MiB / 21.671 s / 25,869 rec/s） |
| PubMed 期望 / 实际 | 4,989 / **4,989**（峰值 35.99 MiB / 1.034 s） |
| ERP products / orders | 145,600 / **145,600** · 48,533 / **48,533** |
| FHIR 期望 / 实际 | 1 / **1**（峰值 31.60 MiB / 0.002 s） |
| 五个 B 字段配对 | **5/5 相符**（其中 1 对是文档核对而非独立测量，已注明） |
| 判据 D 测试 | C0/D0/D1 通过，D2–D4 为上述两个缺口 |
| 真实 schema 集编译 | 150 文件 / 296 边 / 2.5 s / 146 全局元素 |
| 出网次数（D0 / D3 / D4） | **0 / 0 / 0** |
| `fetch.py` 校验路径 | rc=0（150 文件核对通过） |
| `fetch.py` 三条守卫变异 | **3/3 承重**（修完复验，§7-7） |
| 新增固定测试 | **2 个**，`test_known_defects.py` 9 passed |
| 盘上 `data/` | **2.2 GB → 531 MB**（516 MB 非本里程碑产物，未删） |
| 改动范围 | `REAL-WORLD-VALIDATION.md`、`examples/fhir/*`、`tests/golden/test_known_defects.py`、`benchmarks/datasets/wikipedia.json`、`docs/STAGE3-M16-REPORT.md` |
| `src/` `pyproject.toml` `packaging/` `benchmarks/*.py` 改动 | **无** |
| 版本 / 运行时依赖 / CLI flag | **未动** / `1.2.1` / `lxml>=5.0`、`pyyaml>=6.0` / **无新增** |
| attribution 行 | **0** |
| 领先 origin/main 且 **未 push** | 见提交后 `git log` |

---

## §9 需要复审裁决的

1. ★ **`data/` 里 516 MB 更早里程碑的产物**（§6-6）。本里程碑产生的 1.6 GB Wikipedia
   XML 与 4.4 MB 探索用 tgz 我会清掉，但**别人的产物不替你删**。
2. ★ **E-2 的严重度记录**（§5）。读取边界成立、被替代内容相同，但我按 `SECURITY.md`
   的原话把它标成"报告性"——是该记成"已知限制"还是"未修复的安全缺口"，请定。
3. **第四类的定位**（§1.4）：以 5 个小文件为数据、以 150 个 schema 文件为价值。
   若复审认为判据 A 要求"数据"而非"schema"，这一类应当撤下——代价是判据 D 失去
   唯一的数据源，退回成"只在报告里测量、无常驻检查"。

---

## 进度

```
M0 ─ M1 ─ M2 ─ M3 ─ M4 ─ M5 ─ M6 ─ M7 ─ M8 ─ M9 ─ M10 ─ M11 ─ M12 ─ M13 ─ M14 ─ M15  ✓
                                                                                          │
M16 真实数据矩阵 ─────────────────────────────────────────────────────────────────────────  ⚠
   （判据 A–D 完成；判据 E 找到 2 个真缺口，按要求固定 + 停下，未修）

M17 benchmark · M18 注释收敛 · M19 文档 · M20 2.0 RC   未开始
```

---

## 给 M17 的输入

1. ★ **`socket.connect` 是量"有没有出网"的唯一可靠办法**（§4.3）。日志消息会骗人——
   xmlschema 对远程引用**连 warning 都没有**。
2. **`Path.write_text` / `write_bytes` 之外的"规范化副本"是这个项目的第三次陷阱**（§7-2）。
   下载下来的字节要用 `write_bytes`，比较之前先断言集合逐字节相同。
3. **"下载成功"和"下载了正确的集合"是两件事**（§7-1）。按传递闭包爬，不要照抄入口文件的
   include 列表。
4. **fixture 本身要先断言合法**（§7-5）。一个新的守卫测试，落地之前先问"如果产品代码是
   对的，这个测试会绿吗"。
5. **合成 fixture 的隐含假设会承重**（§4.3）。写安全/格式类测试时，主动检查 fixture 有没有
   假设真实世界恰好不同（例如"这个命名空间库里没有副本"）。
