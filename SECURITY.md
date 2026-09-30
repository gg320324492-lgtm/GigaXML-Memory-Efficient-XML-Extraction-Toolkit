# GigaXML 安全模型与漏洞报告

> 对象：`src/gigaxml/xsd.py` 与解析器。
> 本文记录**这个工具允许什么、拦什么、拦在哪一层**，以及**一处已知的行为变更**和**根为符号链接时边界如何定义**。

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

★ 没有任何逃生口。不提供 `--allow-remote-schemas` 之类的开关，
与 ① 的立场一致（不为兼容性放宽安全默认）。

#### ③ 文件系统发布边界 —— `writers.py` / `cli.py`

- 写入先落 `<name>.tmp`，**原子改名后目标路径才出现** ——
  看到的要么是完整文件，要么是文件不存在。
- 重跑的 part 名由 `part_name(index, extension)` **自己生成**，不来自外部。
- run report 对读不到的字段写 `null`，不写 `0`/空哈希 ——
  不编造一个「看起来完整」的收据。

#### ④ 不受信任元数据边界 —— `checkpoint.py`

`--resume` 信任 `checkpoint.json`，而那个文件**可以被第三方改动**。
边界规则：manifest 里的值必须经过验证才能被当作参数用。

★ **这条边界当前是有缺口的**（三个已实测的缺陷，钉在
`tests/golden/test_known_defects.py`，按当前错误行为断言，待后续里程碑修复）：

1. `read_checkpoint` 用 `str()` / `int()` / `bool()` **强制转换**而非校验 ——
   `"complete": "false"` 被读成 `True`（非空字符串为真），
   九种敌意 manifest 八种被接受。
2. `verify_parts` 直接 `directory / part.name`，**不检查 part 名** ——
   恶意 manifest 可让续跑去打开并解析 parts 目录外的文件（**读**，不是写；
   写入侧的名字由 `part_name()` 生成，不受影响）。
3. `config_identity()` **不含 `schema`** ——
   改 XSD 内容而不改路径，哈希不变，`--resume` 放行。

这些**不是已修复的项**，记录在此是因为安全模型要诚实地包含已知的缺口，
而不是只画出挡住的部分。

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
