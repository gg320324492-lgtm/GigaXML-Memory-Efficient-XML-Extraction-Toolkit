# GigaXML 安全模型与漏洞报告

> 对象：`src/gigaxml/xsd.py` 与解析器。
> 本文记录**这个工具允许什么、拦什么、拦在哪一层**，以及**一处已知的行为变更**和**根为符号链接时边界如何定义**。

本文提到的其他概念，各自有专门的一份文档：

| 想知道 | 看 |
|---|---|
| 报出来的错是哪一类、退出码是几、该怎么办 | [ERRORS.md](ERRORS.md) |
| 配置里有哪些键、`schema:` 是什么 | [CONFIG-FORMAT.md](CONFIG-FORMAT.md) |
| 运行报告里每个字段的含义 | [RUN-REPORT-FORMAT.md](RUN-REPORT-FORMAT.md) |
| `--checkpoint-every` 在磁盘上写了什么、`--resume` 读什么 | [CHECKPOINT-FORMAT.md](CHECKPOINT-FORMAT.md) |
| 「原子地落盘」到底覆盖哪一层、不覆盖哪一层 | [OUTPUT-DURABILITY.md](OUTPUT-DURABILITY.md) |
| 从 Python 调用时的公开接口 | [python-api.md](python-api.md) |

## 安全模型

### 为什么是「边界」而不是「校验」

GigaXML 读的是外部输入，但输入分四种，**每一种的信任等级不同**：

| 输入 | 谁产生的 | 信任等级 |
|---|---|---|
| XML 文档 | 用户自己的机器，但可能来自别处 | 不信任 |
| XSD schema | 用户写死在配置里，**但 schema 里的 include 不是用户写的** | 半信任 |
| 输出路径 | 用户自己 | 信任 |
| checkpoint manifest | **上一次运行写的文件，可被第三方改动** | 不信任 |

所以策略不是「校验每个字段」，而是**为每类输入定一条边界**，边界上的规则只有一组，
不给开关：一个「允许远程 DTD」的逃生口会让下面每一条都变成可配置的猜测。

### 四条边界

```
                          ┌──────────────────────────────────────────────┐
                          │              gigaxml 进程                    │
  XML 文档 ──────────────▶│  ① 流式解析边界      parser/streaming.py    │
  (不信任)                │      ↓ 只交出「元素 + 文本」                │
                          │                                              │
  XSD schema ─────────────▶│  ② schema resolver 边界   xsd.py            │
  (半信任)                │      ↓ 只交出「字段类型」                   │
                          │                                              │
  输出目标 ───────────────▶│  ③ 文件系统发布边界       writers / cli     │
  (信任)                  │      ↓ 原子改名后才可见                      │
                          │                                              │
  checkpoint.json ────────▶│  ④ 不受信任元数据边界   checkpoint.py       │
  (不信任)                │      ↓ 只交出「续跑参数」                    │
                          └──────────────────────────────────────────────┘
```

#### ① 流式解析边界 —— `parser/streaming.py:216-221`

解析器的**五项**安全默认，由 `lxml` 的 parser 设置，缺一不可：

```python
{
    "resolve_entities": False,
    "no_network": True,
    "load_dtd": False,
    "attribute_defaults": False,
    "huge_tree": False,
}
```

| 项 | 拦什么 |
|---|---|
| `resolve_entities=False` | XXE：实体解析 |
| `load_dtd=False` | 加载外部 DTD |
| `no_network=True` | 远程 DTD 被拉取 |
| `attribute_defaults=False` | DTD 默认属性注入 |
| `huge_tree=False` | 过深嵌套、超大文本节点、实体放大 |

★ **这五项一起才是防护**：`huge_tree` 同时是三种 DoS 的上限，
为容纳一份「真的很深」的合法文档而打开它，等于同时撤掉三项限制。

★ 状态：**未改动**，已有测试 `tests/integration/test_security_limits.py`。

#### ② XSD resolver 边界 —— `xsd.py` 的 `ALLOW` / `DEFUSE`

schema 本身是用户选的，**但 schema 里的 include / import 不是** —— 那是外部内容
在决定「还有哪些文件被编译进这个进程」。编译发生在**任何一次运行开始之前**，
在同一个进程里，没有第二个人盯着，所以这条边界必须在库这一层，不能靠调用方。

```python
ALLOW = "sandbox"  # 只能读 schema 自己目录里的文件
DEFUSE = "always"  # 不解析 XML 实体
```

传入 `xmlschema.XMLSchema()`；两个调用点（`record_field_types` /
`validate_document`）都经唯一的 `_open_schema`，所以一处生效于两处。

| 路由 | 修复前 `allow='all'` + `defuse='remote'` | 修复后 `sandbox` + `always` |
|---|---|---|
| `../` 穿越 | **编译成功，外部元素进入 schema** | `XMLResourceBlocked` |
| 绝对路径 / 盘符 / `file://` URI | **三条全成功** | 三条全 `XMLResourceBlocked` |
| 远程 `http(s)` | **真的发起连接**，失败只降级为 Warning，编译照样「成功」 | `XMLResourceBlocked`（访问前） |
| 同目录合法 include | 通过 | **通过**（不误伤） |
| 绝对路径但**指向 sandbox 内** | 通过 | **通过**（不误伤） |
| **沙箱内 symlink 指向沙箱外** | **编译成功，外部元素进入 schema** | `XMLResourceBlocked`（resolve 后比较） |
| **沙箱内 symlink 指向沙箱内** | 通过 | **通过**（不误伤：链接本身不可疑） |
| 带 DOCTYPE 的 schema | **通过** | `XMLResourceForbidden` |

拒绝以 `GigaXMLError` 呈现，消息为
`was refused by the schema security policy: ...`，
与普通坏 schema 的 `is not a usable XSD: ...` **可区分**——
让用户能分清「我写错了」和「策略拦下了」。

★ 远程那一行值得单独看：修复前不只是「会出网」，
而是**出网失败被静默降级成 Warning，schema 照样编译成功返回**，
`_open_schema` 的 `except Exception` 抓不到 Warning ——
于是可能拿到一份不完整的类型映射，而整个过程不报任何错。

★ **但降级这条路在 M1 之后并没有完全堵上，直到 M16-FIX 才补上（2026-10-02）。**
`xs:import` 指向沙箱外的文件时，`_reject_escape` 抛出的 `XMLResourceBlocked`
**会被 `xmlschema` 抓住、降级成 `XMLSchemaImportWarning`，再拿它自己内置的那份
namespace 顶上** —— schema 照样编译成功返回，缺的声明悄无声息地不在。
上面那句描述的失败，在 `xs:import` 这条路上在 M1 之后**依然成立**。
**读取边界自始至终没有失守（0 出网、0 越界读），失守的是报告。**

现在 `_open_schema` 把 `XMLSchemaWarning` 升级为异常，并按已有的两种措辞分类：
带 `_BLOCKED_PREFIX` 的走**策略拒绝**（`SecurityError`），其余走**不是可用的 XSD**（`SchemaError`）。
两种措辞必须不同，因为用户的下一步不同：文件不在 → 去补文件；
被策略拦下 → 补上文件也没有用。**两种情况现在都报出来，没有一种静默。**

★ **被升级的是整个 `XMLSchemaWarning` 家族，不是只有 import 那一类。**
我在 xmlschema 4.3.2 上数过，它有 **4 个**子类，四个现在**全部**是错误：

| 子类 | 触发条件（据 `xmlschema` 源码） | 我实测到吗 |
|---|---|---|
| `XMLSchemaImportWarning` | `xs:import` 的每一个 `schemaLocation` 都取不到 | ✔ 实测 |
| `XMLSchemaIncludeWarning` | `xs:include` / `xs:redefine` / `xs:override` 的文档读不到或解析不了 | ✔ 实测 |
| `XMLSchemaAssertPathWarning` | `xs:assert` 的 XPath 里出现 `/` 或 `//` 这类绝对位置路径 | ✘ **构造不出来** |
| `XMLSchemaTypeTableWarning` | UPA 检查判定两个元素的 type table 不等价 | ✘ **构造不出来** |

**为什么按子类收窄**：需要 `from xmlschema.exceptions import XMLSchemaImportWarning`，
而那是**直接 ImportError** —— 这四个是 `exceptions` 模块的惰性属性，
`getattr(exceptions, "XMLSchemaImportWarning")` 拿到的也是 `None`。所以在当前版本上
按子类收窄做不到，收窄等于退回「静默缺声明」。

**代价是明确的**：一条本来能编译的 schema 现在可能失败，而失败可能落在上表后两行里。
后两行标着「构造不出来」，是因为我没有做出能触发它们的 schema ——
**读不到的东西必须说出来**，所以这里写「我没做出来」，而不是写「不会发生」。
能确定的是前两行：**一条 `xs:include` 指向不存在的文件，改动前只发 warning 并照常编译，
现在会失败。**

不误伤的两半同样有钉子：合法同目录 `xs:import` 仍然通过（上面那张表的
「同目录合法 include」一行没有因这次改动而变化），而真实 HL7 FHIR R4
（150 个 XSD、296 条 include·import 边）在升级后**仍编译成功、仍报 146 个全局元素** ——
它在升级前后都**不产生任何一条 `XMLSchemaWarning`**，这正是这条修法成立的前提。

★ 没有任何逃生口。不提供 `--allow-remote-schemas` 之类的开关，
与 ① 的立场一致（不为兼容性放宽安全默认）。

#### ③ 文件系统发布边界 —— `writers.py` / `cli.py`

- **输出不得覆盖输入。** 在创建任何 writer、打开任何文件**之前**，
  用 `samefile` + `resolve()` 判断输出/报告与输入是否同一个文件；
  是则拒绝、退出非零、**源文件字节不变**。
  ★ 判据是**规范化后的比较**，不是字符串比较 ——
  `./a.xml`、`sub/../a.xml`、绝对路径、符号链接、硬链接、大小写差异
  都必须认出来。字符串比较只能拦住「字面完全相同」那一种。
- **`--report` 同样受此约束**：它指向输入会覆盖输入；
  指向输出会与输出**互相覆盖**（谁留下取决于写入顺序）。
- 写入先落 `<name>.tmp`，**原子改名后目标路径才出现** ——
  看到的要么是完整文件，要么是文件不存在。
- 重跑的 part 名由 `part_name(index, extension)` **自己生成**，不来自外部。
- run report 对读不到的字段写 `null`，不写 `0`/空哈希 ——
  不编造一个「看起来完整」的收据。

**为什么这条曾是缺口**：原来拦住 `.xml` 输出的并不是保护，而是
「无法从后缀推断格式」这条**巧合**——用户只要显式给 `--format csv`，
保护就消失，源文件被静默覆盖成 CSV，而运行报告成功。

**未验证 / 不处理**（如实记录，不编造）：

| 情形 | 状态 |
|---|---|
| 硬链接、符号链接 | **本机实测通过**（本机有建立权限）——但**无权限的机器上测试会 skip 并写明 unverified**，不会静默通过 |
| 大小写不敏感（`A.XML` vs `a.xml`） | **仅 Windows 本机表现**；Linux CI 上不适用，未验证 |
| TOCTOU（检查与打开之间文件被替换） | **本里程碑不处理** —— 这是本机 CLI，不是多用户服务，攻击者若已能在两次系统调用之间替换文件，早已具备更直接的破坏能力 |

#### ④ 不受信任元数据边界 —— `checkpoint.py`

`--resume` 信任 `checkpoint.json`，而那个文件**可以被第三方改动**。
边界规则：manifest 里的值必须经过验证才能被当作参数用。

**这条边界现在是严格执行的**：`read_checkpoint` 对每个字段做**类型检查**
（`type(v) is int` / `is bool` 这一类），不做任何 `int()` / `bool()` / `str()` 转换，
并且在构造 `Checkpoint` 之前就拒绝越界的值。**转换是缺陷，检查是修复**——
`int("100")` 和 `bool("false")` 永不抛异常，所以任何建立在它们之上的读取器
都会把 manifest 想要的类型当作调用方假设的类型还回去。

| 校验 | 规则 |
|---|---|
| `version` | 恰好是整数 `1`。★ **用 `type is int` 而非 `!=`**：`True == 1` 且 `1.0 == 1`，值比较会放行布尔和浮点 |
| `records_consumed` / `rejected` / `part.rows` | 整数且 ≥ 0 |
| `rejected ≤ records_consumed` | 一次运行不可能拒绝比读入更多的记录 |
| `complete` | 恰好是 JSON 布尔（`"false"`、`1`、`0` 全拒） |
| `config` / `source.sha256` | 64 位**小写**十六进制（与 `hexdigest()` 实际写入一致） |
| `source` | 对象，且 `path` / `size` / `sha256` 三项齐全 |
| `parts[].name` | `part-` + **至少五位**数字 + `.(csv\|jsonl\|parquet)` —— 即 `part_name()` 会生成的形状（`f"part-{index:05d}"` 的 `05` 是**最小**宽度，不是上限：第 10 万个 part 起是 6 位，**产品必须收得下自己写出的名字**）；`../`、盘符、绝对路径、其他一切文件名全拒 |

★ **part 名在 manifest 被读取时就校验**，所以一个可疑名字不会被拼进路径、
更不会被打开。**这是读面**：写入侧的名字始终由 `part_name(index, extension)`
生成，从不来自 manifest，因此不存在「任意文件写」。

**仍然开着的一个缺口（属 M4，不是本节修复的对象）**：`config_identity()` **不含
`schema`** —— 改 XSD 内容而不改路径，哈希不变，`--resume` 放行。它钉在
`tests/golden/test_known_defects.py` 的 `the_xsd_is_absent_from_the_run_identity`
（该测试明确标着归属里程碑），按当前行为断言，等它的里程碑修复。

缺口记录在这里，是因为安全模型要诚实地包含还没挡住的部分，
而不是只画出挡住的部分。

### schema 身份与它的已知限制

XSD 的**内容哈希**现在参与 run identity（`config_identity()`）——
改 schema 内容而不改路径，续跑会被拒，不再静默产出不一致的结果。
身份**只取内容、不取路径**：同一份 schema 换个目录仍是同一个 run。

★ **已知限制：只哈希主 XSD 文件，不哈希它 `xs:include` 的子文件。**

一个 schema 可以 `xs:include` 同目录的另一个 XSD（M1 沙箱内的合法用法）。
**改了被 include 的子文件内容，主文件哈希不变，续跑仍会放行。**

这是**明确的限制，不是遗漏**：完整覆盖要遍历整个 include 图，
那需要复用 M1 的编译/加载机制，把两个本可独立的里程碑耦合起来；
而当前方案已经修掉真实可触发的那个缺陷（用户改主文件内容）。
Stage 3 的原则是**明确限制优于部分支持**。

**兼容性**：这次改动让**此前写下的 `config_hash` 全部改变** ——
用 `--checkpoint-every` 写出的旧 checkpoint **无法再 `--resume`**。
★ **未使用 schema 的运行不受影响**（其哈希逐字节未变，已用固定摘要断言）。

### 已知行为变更（一处，会让此前能用的输入失败）

**带 DOCTYPE 内部实体的 XSD 从「能用」变成「被拒」。**

| | 之前 `defuse='remote'`（库默认） | 现在 `defuse='always'` |
|---|---|---|
| 普通 XSD | 通过 | 通过 |
| 带 DOCTYPE 内部实体的 XSD | **编译成功** | `XMLResourceForbidden: Entities are forbidden` |

**为什么改**：与解析器立场一致 —— 解析器对文档拒绝 DTD 与实体
（`load_dtd=False` / `resolve_entities=False`）**且不给开关**；
schema 若单独放行，等于给同一类输入开了一个特例。

**影响面已查**：仓库里**没有一个 `.xsd` fixture 文件**
（`find tests examples -name "*.xsd"` 为空，测试用的是内联字符串），
所以现有测试零影响。但这是一次真实的语义收紧。

**钉住它的测试**：`tests/security/test_xsd_entities.py`
- `test_a_schema_with_an_internal_entity_is_refused` —— 拒绝，且消息点名实体规则
- `test_the_same_file_would_have_compiled_before_this_policy` ——
  用库自己的默认值编译同一个文件并成功，**证明这是策略改的，不是文件本来就坏**

### 符号链接与边界语义（已覆盖）

**规则：先 resolve，再比较。** 资源路径在被比较前先解析（跟随链接），
所以问题是「哪个文件」，从不是「哪个名字」。

`sandbox/link.xsd` → 指向 `sandbox` **之外**的真实文件：

| 路由 | 结果 |
|---|---|
| `../outside/secret.xsd`（直接穿越） | `XMLResourceBlocked` |
| `sandbox/link.xsd`（符号链接，字面路径在 sandbox 内） | **`XMLResourceBlocked`**（resolve 后在沙箱外） |
| `sandbox/alias.xsd` → `sandbox/real.xsd`（沙箱内链到沙箱内） | **通过** |

★ **第三行与第二行同样重要**：「见链接就拒」能通过所有拒绝测试，却会打断
把一份 schema 起两个名字的用户。**链接本身不可疑，只有它的去向可疑。**

#### 边界怎么定 —— 根文件是 symlink 时

**边界 = 传给 `_open_schema(schema_path)` 的那个路径经 `resolve()` 后的父目录。**

威胁模型决定了这个取向：**不可信的是 schema 文件的内容**（可能来自别处），
**不是用户自己点名的路径**。所以：

| 情形 | 边界 | 依据 |
|---|---|---|
| 根是 symlink，指向 `real/` | **`real/`**（resolve 后的目录） | 用户显式指名了那个位置，等于授权 |
| 从根出发的 include/import | 与该边界比较，越界拒绝 | 不是用户点名的 |

**已知限制（库的行为，非本模块）**：根为 symlink 时，**相对路径**的 include 按
**字面路径的父目录**（`base_url`）解析，而不是按 resolve 后的目录 —— 于是
`real/actual.xsd` include `sibling.xsd` 会去找 `entry.xsd` 同级的 `sibling.xsd`，
那个位置既不在边界内也不是真实文件。

- 库**原生**的行为是：**Warning + `elements=[]`** —— include 被静默丢掉，
  schema 仍「编译成功」。
- **本模块的行为是：明确拒绝。** 一个消失的 include 不该伴随一次成功的编译，
  这与 ② 里「远程 include 失败却照样编译成功」是同一类问题。
  ★ **这句话在 M16-FIX 之前对 `xs:import` 是假的** —— 那条降级路径只堵了 `xs:include`。
  现在两种都拒绝，见上文「降级这条路」。
- **相对路径 + 符号链接根**要能工作，请用**绝对路径**，或直接把根文件放在它真实的目录里。

钉住这两种行为的测试：`test_a_root_that_is_a_symlink_sets_the_boundary_where_it_lands`
（绝对路径：边界内通过 / 边界外拒绝）、
`test_a_relative_include_from_a_symlinked_root_is_refused`（相对路径：不静默通过）。

### 不在边界之内

- **不是**沙箱/容器：进程仍能读它自己有权限读的任何文件，只是**经由 schema 的路径**不能。
- **不防**一个把自己的恶意 XSD 路径写进配置的人 —— 那是本地执行权限，
  边界防的是「外部内容经由 schema 决定还有哪些文件被编译」。
- **不防** checkpoint 那三个缺口（上文 ④，待修）。

---

## 报告漏洞

**本节由后续的项目治理里程碑补全。**

这里将来会写清楚：报告一份安全问题该发到哪里、按什么格式、多久会有人看。
在那之前，本节**刻意留空**——一个虚构的联系方式或一条没人值守的 SLA，
比没有联系方式更糟，因为照着走的人会以为自己已经报告出去了。
