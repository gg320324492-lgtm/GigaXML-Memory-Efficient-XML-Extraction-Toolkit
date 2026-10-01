# STAGE3-M15 报告 — 依赖与供应链

> 阶段：Stage 3 / 里程碑 **M15**。前置：M0–M14（`4a996ae`）。三次提交：`6c3c399` → `80d6ffb` → `a4a95ae`。
> **完成后停下，等复审。未开始 M16。**

---

## §0 先说最要紧的：本里程碑的每一个 job 都没跑过

**未 push，所以 CI 一次没有运行过。** 本报告没有任何一句断言"CI 会绿"。

判据 A–G 的多数内容是**对仓库文件的改动**，这类改动可以在本机穷尽验证，我做了；
但下面这些**只有 runner 能回答**，本报告逐条列在 §6，**不做任何推测性结论**：

| 需要 push 才能验证 | 本机等价物 |
|---|---|
| `ci-shape` 里 `pins` / `pins --verify-remote` 两个 step | 两个命令在本机跑过，输出见 §2.3 |
| `dependency-audit` job | **完全相同的命令序列**在本机跑过，见 §4 |
| release job 的 SBOM + SHA256SUMS 两个 step | 用**真实归档**完整排演过，见 §5.1 |
| release 附上新文件 | **无法验证**——见 §5.4 |
| Dependabot 是否真的会开 PR | **无法验证** |

★ **§3 的顺序执行了，但"跑一次看红"这一环不存在。** §3 定死"先 pin、再产物、最后扫描"，
这个顺序我按三次提交执行了。但 §3 每一步后面跟着的"跑它、处理红"我没有做——没有观察点。
替代方案在 §1 逐条说明，并说明替代的强度上限。

---

## §1 三次提交，以及每一步的验证强度

| 提交 | §3 步骤 | 本机验证到什么程度 |
|---|---|---|
| `6c3c399` | **先 pin**（判据 C/D） | **强**。SHA 全部 `git ls-remote` 实测；守卫 8 条变异，含两个对照组 |
| `80d6ffb` | **产物**（判据 E/F） | **强**。真实归档 + 两个独立实现复算 + 三处真缺陷由排演抓出 |
| `a4a95ae` | **最后扫描**（判据 B/A） | **中**。扫描命令本机跑过并给出结果；**但"CI 上这个 job 是红的"无法验证** |

★ **顺序有意义，我按它执行了，但我要说清楚它没有兑现的那一半。**
pin 是唯一会改变"发布链实际执行什么"的一项，所以它最先做、单独验证。实测结果是：
**两条 pin 都是"冻结"而不是"升级"**，见 §2.2——这正是 pin 之后必须先确认的事，
否则 §3 那条"若 pin 之后 CI 有变化就停下报告"就无从判断。

---

## §2 判据 C：pin 的完整清单

### 2.1 现状（改动前）

`package.yml` 里 6 个 action，`test.yml` 里 2 个，**无一个 pin 到 SHA**。
其中两个有发布权限：

- `softprops/action-gh-release@v2` — 附着文件到公开 Release
- `pypa/gh-action-pypi-publish@release/v1` — 上传 PyPI，**名字不可重用**

### 2.2 两条 pin 的实测过程

判据 C 要求"**实测，别抄网上的**"。下面全部是本机 `git ls-remote` 的原始输出。

**softprops/action-gh-release：**

```
$ git ls-remote --tags https://github.com/softprops/action-gh-release \
      'refs/tags/v2.6.2' 'refs/tags/v2'
3bb12739c298aeb8a4eeaf626c5b8d85266b0e65	refs/tags/v2
3bb12739c298aeb8a4eeaf626c5b8d85266b0e65	refs/tags/v2.6.2
```

★ **`@v2` 与 `v2.6.2` 是同一个 commit。** 这是"冻结而非升级"的证据。
若两者不同，这条 pin 就是一次版本升级穿着 pin 的外衣，判据 G 已被破坏。

**pypa/gh-action-pypi-publish：**

```
$ git ls-remote --heads https://github.com/pypa/gh-action-pypi-publish release/v1
dc37677b2e1c63e2034f94d8a5b11f265b73ba33	refs/heads/release/v1

$ git ls-remote --tags https://github.com/pypa/gh-action-pypi-publish 'refs/tags/v1.14.2*'
a892a5a61159132606e93a2fa6f4358831b04d26	refs/tags/v1.14.2
dc37677b2e1c63e2034f94d8a5b11f265b73ba33	refs/tags/v1.14.2^{}

$ gh api repos/pypa/gh-action-pypi-publish/compare/v1.14.2...release/v1
  status  identical (ahead 0, behind 0)
```

★ **两个发现，都改变了结论：**

**(a) `release/v1` 是分支，不是 tag。** 比 tag 更弱——分支可以被 force-push，
被删掉的历史不留痕迹，也没有任何发布为它背书。所以这个更需要 pin。

**(b) 但"分支头不保证是任何已发布版本"，于是版本注释可能写不出来。**
这是 pin 带来的**新问题**，不是原有问题。实测答案是：**分支头恰好就是 `v1.14.2` 的
commit**（`^{}` 剥离后与分支头逐字符相同，compare 也报 identical）。
于是判据 C 与判据 G **没有冲突**——这是本里程碑最需要测量的一处，
因为两种裁决（pin 分支头而无注释 / pin v1.14.2 而改变行为）都会破坏判据之一。

### 2.3 pin 后的结果

```
$ python tools/ci_selfcheck.py pins --verify-remote
third-party action                      pinned to     version       resolved to
softprops/action-gh-release             3bb12739c298  v2.6.2        v2.6.2 (release)
pypa/gh-action-pypi-publish            dc37677b2e1c  v1.14.2       v1.14.2 (release)

2 third-party action(s) pinned; 4 first-party action(s) left on tags:
actions/checkout, actions/download-artifact, actions/setup-python, actions/upload-artifact
rc=0
```

### 2.4 ★ 我没有做的一件事，以及为什么

**softprops 已经发布 v3.0.3，`v2` 落后整整一个大版本。**

```
v3.0.3  ->  efb35369e0ad2afab669f228072c1b0d510eae64   (由 refs/tags/v3^{} 剥离得到)
```

迁移到 v3 是一个真实决定：v3 改过输入、改过输出格式、改过 release-note 行为。
**判据 G 说本里程碑只加检查与产物，不动发布链，所以我没有做，并且写在了 pin 旁边的注释里。**
"已 pin" 与 "是最新的" 是两个不同的性质，本里程碑只主张前者。

### 2.5 guard 的规则按 owner 划，不按名字列表

`tools/ci_selfcheck.py pins` 的规则是"**owner 不是 `actions` 的一律要 pin**"，
不是"这两个 action 要 pin"。名单式规则的问题是每次新增发布步骤都要记得改名单，
而忘记改的那一刻正是守卫不再守着它被写出来守护的东西的那一刻。
变异 **P6** 专门验证了这一点：新增一个 tag 形式的第三方 action → **红**。

---

## §3 判据 D：pin 机制测试

守卫：`tools/ci_selfcheck.py pins [--verify-remote]`，接进 `ci-shape` job，**拆成两个 step**。

★ 拆开是有意的：结构检查不需要网络且无可争辩；解析检查要问远端，
而 github.com 不可达时它说"检查没能运行"并 **rc=2**，绝不说"工作流有问题"。
合成一个 step 的话，一次网络抖动就会被读成缺陷——这正是 M14 `report` 那类工具翻车的形状。

### 3.1 八条变异，8/8 符合预期（含两个对照组）

| id | 变异 | 结果 |
|---|---|---|
| **P0** | **对照：完全不变** | **绿** ✓ |
| **P1** | **简报要求的：pin 改回 `@v2`** | **红** ✓ |
| P2 | 留 SHA，删掉版本注释 | 红 ✓ |
| P3 | SHA 正确，版本写成不存在的 `v9.9.9` | 红（`--verify-remote`） ✓ |
| **P4** | ★ **形状对、代码错：指向 v3.0.3 的真 commit，注释仍写 v2.6.2** | **红**（`--verify-remote`） ✓ |
| P5 | pypa 改回 `release/v1` 分支 | 红 ✓ |
| P6 | 新增一个 tag 形式的第三方 action | 红 ✓ |
| **P7** | **对照：把官方 action 也 pin 成 SHA**（判据 C 豁免） | **绿** ✓ |

★ **P4 是最重要的一条。** SHA 是合法 40 位十六进制，注释也在，
**结构检查完全无话可说**——只有 `--verify-remote` 能抓。这正是它存在的理由。
单条变异（判据 C 要求的 P1）无法证明一个守卫承重；**P0 与 P7 才是让另外六条可信的东西**。

### 3.2 ★ 我第三次栽在"确认变异真的落到了目标上"

简报里写着你在 M14 复审时连续第四次栽在这条上。我这次也栽了，但**栽的地方不一样**：

第一次跑 8 条，P6 / P7 被脚本判为 **"未测到"而不是"漏掉"**——脚本拒绝把没验证的东西算成结论。
诊断结果是两个不同的原因：

- **P6：Windows 的 `write_text` 把 `\n` 翻译成 `\r\n`**，我断言的字符串因此对不上。
- **P7：`actions/checkout@v4` 在 `package.yml` 里出现三次**，而我的断言假设锚点唯一。

修完第一次，P2 又坏了。**这次是我断言的数学错了**：`replace` 是 `find` 的子串
（P2 删注释），所以"替换串计数 +1"不成立——**四种 find/replace 关系，四种不同的算法**。

★ 最终改成**每个变异显式声明它应该产出的那一段文本**，与 find/replace 的关系无关。
这是本轮唯一正确的形式；前两版都是"看起来在验证、实际只验证了一种形状"。

**第三次时间上是另一回事**：`git add` 在 `.github/` 上 **rc=1 却已经把文件暂存了**，
`&&` 链断掉，`git commit` 根本没执行。**git status 显示三个文件都已 staged。**

---

## §4 判据 B：漏洞扫描

### 4.1 ★ 工具选型与"它在本机跑得起来吗"

判据 B 明确问了这一条，简报也警告"`pip-audit` 是不是也有 Windows 问题，先查再用"。

**实测结果：**

| 工具 | 本机安装 | 本机运行 |
|---|---|---|
| `pip-audit` | 2.10.1，**装得上** | **跑得通**，见 §4.2 |
| `cyclonedx-bom`（`cyclonedx-py`） | 7.5.0，**装得上** | ★ **跑不起来**，见 §4.3 |

两者都装在**一次性 venv** 里（`%TEMP%/ecwork/m15probe`），项目的 `.venv` 未被触碰。

★ **`cyclonedx-py` 在这台机器上连自己的 `--help` 都打不出来：**

```
$ cyclonedx-py environment --help
UnicodeEncodeError: 'gbk' codec can't encode character '•' in position 2823
RC=1

$ python -c "import locale,sys; print(locale.getpreferredencoding(False), sys.stdout.encoding)"
cp936 gbk
```

argparse 的帮助里有 `•`（U+2022），本机控制台编码编不出去。
`PYTHONIOENCODING=utf-8` 之后 RC=0。这个环境变量已经写进 `package.yml` 的 SBOM step。
**runner 是 UTF-8，本来不会触发；但工具在开发者机器上不可用这件事必须记下来**，
否则下一个人会花半小时以为是配置错了。

### 4.2 ★ 扫描的实际结果

**runtime 依赖：lxml、pyyaml**（从 `pyproject.toml` 的 `[project.dependencies]` 读，不手写清单）。

```
$ pip list --format=freeze | grep -v '^pip=='
gigaxml==1.2.1
lxml==6.1.3
PyYAML==6.0.3

$ pip-audit --requirement runtime.txt --progress-spinner off
No known vulnerabilities found
RC=0
```

**没有 high / critical，也没有其它等级。判据 B 要求逐条列出——这里没有可列的条目，
所以写的是"扫描了哪三个包、用的什么命令、退出码几"。**

★ **但一个永远说"没问题"的扫描器和一个没在扫的扫描器长得一模一样**，所以做了对照：

```
$ printf 'jinja2==3.1.3\n' > control.txt && pip-audit -r control.txt
Found 8 known vulnerabilities in 1 package
Name   Version ID              Fix Versions
jinja2 3.1.3   PYSEC-2026-1474 3.1.4
jinja2 3.1.3   PYSEC-2026-1475 3.1.5
jinja2 3.1.3   PYSEC-2026-1472 3.1.5
jinja2 3.1.3   PYSEC-2026-1471 3.1.6
jinja2 3.1.3   PYSEC-2026-1471 3.1.6
jinja2 3.1.3   PYSEC-2026-1474 3.1.4
jinja2 3.1.3   PYSEC-2026-1475 3.1.5
jinja2 3.1.3   PYSEC-2026-1472 3.1.5
RC=1
```

★ ★ **它报 8 行，实际只有 4 个不同的 advisory，每个出现两次。**
直接抄"8 个漏洞"就是伪造——这正是判据 B 那句"不许只说扫描通过"要防的事。
（成因未查，判据 B 不要求；记录在此以免下一个人照抄这个数字。）

### 4.3 这个 job 红了会怎样（判据 B 的"不要求阻断发布"）

**它让 `test.yml` 变红，它不阻断 release。** 载体是"这个 job 在哪个 workflow 里"，
不是某个 flag——`package.yml` 对它没有 `needs:`，以后也不会有，
所以即便 advisory 数据库当天不可达，发布仍然做得了。

**为什么不让它"红但不失败"**：扫描器在有发现时返回非零是它的全部意义；
把那个退出码吞掉，就回到了"一份需要有人记得去看的东西"。

---

## §5 判据 E / F：SHA256SUMS 与 SBOM

### 5.1 ★ 排演：真实归档 + 两个独立实现

不是断言，是排演。用 `zip` / `tar` 造出 release job 真正会产出的目录结构：

```
artifacts/
  gigaxml-gui-windows.zip                 gigaxml-gui-macos.tar.gz
  gigaxml-gui-linux.tar.gz                 gigaxml-gui-sbom.json
  gigaxml-gui-windows/GigaXML-Setup-1.2.1.exe   <- 嵌套，**/ glob 真的被走到
```

**生成结果（`SHA256SUMS.txt` 逐字）：**

```
c56fbfc8e20d9e26304be7b16836b7f4ae88624be5c7cabe62c07c8a822e6015  gigaxml-gui-linux.tar.gz
84d1893584082154b5c2fe214f883ef1b8431af21b12b0f46b21aeb1a0cc3fbb  gigaxml-gui-macos.tar.gz
d388d08108d157452f8189c52d0f8be4b17e2a1c13aba55a0f9753c09acdb51e  gigaxml-gui-windows.zip
3473c9f28b5fe0723bb646d560cc6abaee0b3b7f794d03c2e8d60553964ad40c  gigaxml-gui-windows/GigaXML-Setup-1.2.1.exe
b06af3f351e43c41d45f1c593d54fea0976ef791a7b040adf0e0da87d40d6b29  gigaxml-sbom.json
```

**独立复算 #1 — GNU coreutils（与本项目无关的另一套 C 实现）：**

```
$ sha256sum -c SHA256SUMS.txt
gigaxml-gui-linux.tar.gz: OK
gigaxml-gui-macos.tar.gz: OK
gigaxml-gui-windows.zip: OK
gigaxml-gui-windows/GigaXML-Setup-1.2.1.exe: OK
gigaxml-sbom.json: OK
rc=0
```

**独立复算 #2 — Windows `certutil -hashfile`（第三套实现）：5/5 全部吻合，mismatches: 0**

★ 只有写它的代码在检查自己，那些数字就只是主张。
`sha256sum -c` 是另一个 C 实现读同样的字节，这一步才让它们成为证据。

**★ 变异：把 `gigaxml-gui-macos.tar.gz` 最后一个 bit 翻转**

| 谁 | 结果 |
|---|---|
| `sha256sum -c` | **rc=1**，`gigaxml-gui-macos.tar.gz: FAILED` |
| `certutil` | `a6b87eb1aad84cbf` vs 列出的 `84d1893584082154` — **不一致** |
| 本项目的工具 | **rc=1**，`does not match` |

**三方全部发现。**

### 5.2 ★ 排演抓出的三个真缺陷——每一个都是"在 Linux 上没事"

**这三个缺陷单靠读代码一个都看不出来。**

**(1) `files: |` 是字面块标量，里面的 `#` 是内容不是注释。**
我给 `files:` 写了 9 行解释性注释，`release_globs()` 把它们**当成 9 条 glob** 读了进去。
当时无害只是运气：九行散文都不匹配。**但只要有一行散文里写着
`artifacts/**/GigaXML-Setup-*.exe`，它就会匹配到真的**，校验和文件就会声称一个
release 并不附带的文件。修法是两处：工具丢弃整行 `#`，注释移到块外。

**(2) ★ `write_text` 在 Windows 把 `\n` 翻译成 `\r\n`，标准工具读不了自己生成的文件。**

第一次排演的输出：

```
$ sha256sum -c SHA256SUMS.txt
gigaxml-gui-linux.tar.gz
: FAILED open or read          ← 五个文件全部
rc=1
$ certutil ...                 五个全部 agrees，mismatches: 0
```

`certutil` 五个全对、`sha256sum` 五个全错——**因为 CRLF 让 `sha256sum` 把文件名读成了
带一个尾随回车的名字**，于是打不开。**哈希全是对的，报错的是读它的工具。**
这是校验和文件能给出的最令人困惑的报告，而这个工具存在的全部意义就是让标准工具能读它。
现在 `newline="\n"` 是写死的，`tests/unit/test_supply_chain_guards.py`
直接断言字节里没有 `\r`——并已验证该断言**能失败**（去掉 `newline=` 参数它立刻红）。

**(3) 孤儿检查把归档的输入目录也报成"未列出的产物"。**
第一版遍历整棵树，于是 `gigaxml-gui-linux/_internal/libfoo.so` 被报成
"会带着没有哈希一起发布"。**那不是缺陷，那是一个已经有哈希的归档的输入。**
报警报这些，读者会开始忽略这条消息，真正要紧的那种（归档旁边的野文件）就淹在噪声里。
现在只看顶层。

### 5.3 ★ SBOM：格式合法 ≠ 说的是这个项目

**生成**（真实 `pip install .`，一次性环境）：

```
$ cyclonedx-py environment <venv>/bin/python --pyproject pyproject.toml \
      --mc-type library --sv 1.6 --of JSON --validate -o artifacts/gigaxml-sbom.json
INFO | CDX > Validating result to spec: 1.6/JSON
RC=0

$ python tools/check_sbom.py artifacts/gigaxml-sbom.json
pyproject declares gigaxml 1.2.1 with runtime dependencies: lxml, pyyaml
components (3):
  lxml          6.1.3
  pip           26.1.2   <- not a declared dependency
  pyyaml        6.0.3
also present and not declared in pyproject: pip
SBOM describes gigaxml 1.2.1 and lists every declared runtime dependency.
RC=0
```

**判据 F 的两半，两半都验了：**
- **格式合法**：`--validate` 开着（该工具**默认就是开的**，我写出来是为了让读者不必知道这件事），
  CycloneDX 1.6 通过。
- **列出的包与实际依赖一致**：`tools/check_sbom.py` 检查根组件是本项目、版本与
  `pyproject.toml` 一致、每个声明的 runtime 依赖都在 `components` 里。

★ **`pip` 在 `components` 里。** 它确实装在那个环境里，是一台机器的真实记录；
工具**打印出来而不是把它藏起来**——一台机器的记录不做整理。
（我的第一版比较把 `gigaxml` 报成"只在安装里、不在 SBOM 里"，那是**我比错了**：
根组件按 CycloneDX 规范在 `metadata.component`，不在 `components`。）

**★ 覆盖范围，以及我为什么没有覆盖更多：**
这份 SBOM 描述的是**库安装**（gigaxml + lxml + pyyaml）。
三个冻结二进制里真正的 PySide6 那棵树是另一个集合，描述它等于描述一次安装、而不是那个
冻结产物——**列出比实测更多的条目，比一份窄的更糟，因为多出来的条目看起来像是查过的。**
这一条写在了 `package.yml` 的注释里，也就是下载者会读到的地方。

**SBOM 检查的 5 条变异，5/5 符合预期**（含对照组）：

| id | 变异 | 结果 |
|---|---|---|
| **S0** | **对照：本次真实生成的 SBOM，不改** | **绿** ✓ |
| S1 | 悄悄少一个声明的依赖 | 红 ✓ |
| S2 | SBOM 描述的是另一个项目 | 红 ✓ |
| S3 | SBOM 里的版本树已经没有 | 红 ✓ |
| **S4** | ★ **合法 CycloneDX，成分表为空** | **红** ✓ |

★ S4 正是 `--validate` 单独抓不到的那一类。

### 5.4 ★ release job 的唯一结构性改动，以及它新增的失败模式

`release` job 原本**没有任何 Python**（checkout、解压、调 action，全是 bash 和 HTTP）。
两个新 step 是 Python，所以加了一个 `actions/setup-python@v5`。

**判据 G 要求"发布流程一个字都不许改"。我加了一个 step，所以必须说清楚它加了什么：**

- **没有改**：发布什么、怎么发、何时发、rc 门、Trusted Publishing、`prerelease`、草稿状态。
- **加了**：**一种以前不存在的失败方式——release 现在需要能连到 PyPI**，
  因为 SBOM 是从一次真实安装生成的，不是手写的。

这个失败是**故意的**（校验和没生成就不该发布），但它是新的，不该以意外的形式出现。
**这一条需要复审时裁决**：如果认为"发布链不应新增网络依赖"，
可以改成把产物生成放在 tag 构建 job 里，但那样 SBOM 描述的是构建机的环境而不是发布的环境。

### 5.5 release 接线的守卫（4 条变异，4/4）

`shape` 新增检查：release job **生成并附上**两个文件。

| id | 变异 | 结果 |
|---|---|---|
| **R0** | **对照：文件重写但内容不变** | **绿** ✓ |
| R1 | 校验和生成了但不附上 | 红 ✓ |
| R2 | SBOM 生成了但没人检查它说的是不是这个项目 | 红 ✓ |
| R3 | 整个校验和 step 删掉 | 红 ✓ |

★ **三个说法可以各自为假而 job 依然是绿的**：文件生成了、文件附上了、文件被检查了。
删掉生成 step → 绿；删掉 `files:` 里那行 → 绿；删掉检查 → 绿。R1/R2/R3 各打一个。

---

## §6 哪些东西需要 push 才能验证

| # | 未验证项 | 本机等价物 / 为什么无法验证 |
|---|---|---|
| 1 | **`ci-shape` 的两个 pins step 在 runner 上是绿的** | 两个命令本机跑过（§2.3），但 runner 的网络与 git 环境未验证 |
| 2 | **`pins --verify-remote` 在 CI 上不被网络抖动误伤** | ★ **本机实测它会**：`git ls-remote ...: Connection was reset` 与 `SSL_ERROR_SYSCALL` 各出现一次，rc=2、提示语正确。这证明区分是对的，**但也证明它需要网络** |
| 3 | **`dependency-audit` job 在 runner 上跑得通** | 完全相同的命令序列本机跑过并给出结果；`$RUNNER_TEMP` 路径未验证 |
| 4 | **release job 的 SBOM step** | 本机用真实安装生成并校验过；`cyclonedx-py` 的 `$RUNNER_TEMP/.../bin/python` 路径形式未跑过（Windows 上是 `Scripts/python.exe`） |
| 5 | **release job 的 SHA256SUMS step** | 完整排演过（§5.1），包括 `zip`/`tar` 产出的真实归档 |
| 6 | **新文件真的被附到了 Release 上** | ★ **无法验证**。这是 `softprops/action-gh-release` 在 tag 构建里第一次携带新文件，而该 job 只在 `refs/tags/v*` 上跑 |
| 7 | **`fail_on_unmatched_files: true` 对新增两行的行为** | 未验证 |
| 8 | **Windows/macOS 两条腿**（M14 的）在新 `test.yml` 下仍绿 | 未验证；新 job 只在 ubuntu，本机也无法模拟 |
| 9 | **Dependabot 真的会开 PR** | 需要仓库启用该功能 |
| 10 | **Dependabot 改 SHA 时会一并改 `# vX.Y.Z` 注释** | 这是它的文档化行为，**未实测**。若它只改 SHA，`pins --verify-remote` 会红——这是**预期行为**，不是故障 |
| 11 | **13 个 job 全部为绿** | 未验证，一个都没跑过 |

★ 第 2 项值得单独说：**这台机器到 github.com 的链路是间歇性的**，
`git ls-remote` 在同一次会话里成功过也失败过。CI 上这条 check 依赖网络，
所以我把"检查没能运行"（rc=2）和"工作流有问题"（rc=1）分成了两种退出码和两种措辞。

---

## §7 未做的事（明确不做，不是遗漏）

- **未 bump 版本**（仍 `1.2.1`）、**未 push**（HEAD 领先 origin/main 24 个提交）。
- **未改 `pyproject.toml`**（runtime 部分、dev extra 都没动）——`git diff 4a996ae..HEAD -- pyproject.toml` 为空。
- **未改 `src/`、`packaging/`、`benchmarks/`**——同一命令在这些路径上返回空。
- **未改发布语义**（判据 G），除了 §5.4 说的那个新增网络依赖，已显式提出待裁决。
- **未把 pip-audit 加进 dev extra**，理由与代价写在 `test.yml` 的 step 注释里。
- **未迁移到 softprops v3**，理由与它已存在的事实写在 §2.4。
- **未新增任何 CLI flag、未新增任何 runtime 依赖。**

---

## §8 我自己错的七处，以及测量把它们翻过来了

| # | 我以为 | 实测 |
|---|---|---|
| 1 | `git ls-remote <url> refs/tags/` 能列出全部 tag | **末尾斜杠是 glob，匹配不到任何东西，退出码 0**——一个"这个仓库没有 tag"的假答案。正确的写法是 `refs/tags/*`。它顺带污染了两次 `gh api`（`newest` 为空导致路径非法） |
| 2 | `sort -V` 能按版本排序 | 这个 Git Bash 里没有，输出明显乱序。改用 Python 显式排序 |
| 3 | workflow 里的 glob 相对 `artifacts/` 解析 | 相对**步骤的工作目录**解析。我拿它相对 `artifacts/` 找 `artifacts/artifacts/*.zip`，**得到一个空集**——守卫拦下了，没有产出"看起来像证据"的空校验和文件 |
| 4 | `--artifacts artifacts` 这个相对路径能和 `base.glob()` 的绝对路径比较 | 不能，`relative_to` 全抛 `ValueError`，被守卫 `except ValueError: continue` **逐个吞掉**，于是这个检查只能说出"没有文件"，它确实这么说了 |
| 5 | "补丁落没落到目标上"有**一个**正确的判断式 | **没有。** find/replace 有四种关系，我的前两版各在其中一种上错。"替换串计数 +1" 在 `replace ⊂ find` 时不成立。最终改成每个变异显式声明应有产物 |
| 6 | 注释写在 `files: |` 里是给读代码的人看的 | **它是 9 条 glob。** 而且我把它挪出去时缩进掉到第 0 列，把 YAML 弄坏了一次（`expected <block end>`） |
| 7 | ★ `normalise()` 是 PEP 503 | **不是。** 真正的 PEP 503 把 `-_.` 折叠成单个 `-`；我原来只保留 `-`、把 `.` 和 `_` 丢了，于是 `ruamel.yaml → ruamelyaml`。**docstring 里那句"PEP 503"是不实陈述**，而 `lxml` / `pyyaml` 都没有分隔符，所以两种实现在今天对所有依赖结果相同——**这正是它会活到出事那天的原因**。是我自己写的测试抓到的 |

★ 另外两件不是"错"但值得记：

- **`write_text` 的 CRLF**（§5.2(2)）与 **`cyclonedx-py` 的 GBK**（§4.1），
  是本轮 Windows 第三次把一个字符串在传递途中改掉。前两次分别是 M14 的
  `.gitattributes` 与 M5 的编码 bug。**三次形状相同：代码在本机是对的，在别处不是。**
- **我在 `80d6ffb` 的提交信息里写了一行 `Co-Authored-By:`。**
  我自己那条 `grep -ci "co-authored"` 抓到它（返回 1，期望 0），`git commit --amend` 去掉。
  **如果没有那条检查，它就进去了。** 现在 HEAD 及前两次提交均为 0。

---

## §9 本次重跑的数字

```
$ pytest -q --cov=src/gigaxml --cov-report=term-missing --cov-fail-under=95 \
      --junit-xml=ci-report.xml tests/unit tests/integration tests/golden \
      tests/security tests/property
1798 passed in 238.61s (0:03:58)
Required test coverage of 95% reached. Total coverage: 95.66%
RC=0
```

| 项 | 数值 |
|---|---|
| 通过 | **1798**（1779 + 新增 **19** 个守卫测试） |
| 失败 / 跳过 / 错误 | **0 / 0 / 0** |
| 覆盖率 | **95.66%** |
| `ruff check .` | **All checks passed** |
| `ruff format --check .` | **216 files already formatted** |
| `shape` | clean，**13 jobs** / 2 workflow 文件 / 5 个共享测试目录 |
| `pins --verify-remote` | **2 个第三方 action 已 pin**，4 个官方保留 tag |
| pin 变异 | **8/8** |
| 接线变异 | **4/4** |
| SBOM 变异 | **5/5** |
| 校验和排演 | **PASS**（5 文件 / 2 独立实现 / 变异三方全抓） |
| SBOM 校验 | **通过**（CycloneDX 1.6，3 组件，`pip` 已标注） |
| 漏洞扫描 | **无发现**（lxml 6.1.3 / PyYAML 6.0.3 / gigaxml 1.2.1），对照组 4 个不同 advisory |
| 三次提交改动 | 7 文件，**+1296 −3** |
| `src/` `pyproject.toml` `packaging/` `benchmarks/` 改动 | **无** |
| 版本 / runtime 依赖 | `1.2.1` / `lxml>=5.0`、`pyyaml>=6.0`（均未动） |
| attribution 行 | **0**（HEAD 及前两次） |
| 领先 origin/main 且**未 push** | **24 个提交** |

---

## §10 需要复审裁决的三件

1. ★ **release job 新增了"需要 PyPI 可达"这个失败模式**（§5.4）。
   判据 G 说"发布流程一个字都不许改"，我加了一个 step 并显式提出了它。
   若认为不可接受，替代方案是把产物生成移进 tag 构建 job——代价是 SBOM 描述构建机而非发布环境。

2. **SBOM 只覆盖库安装，不覆盖冻结二进制里的 PySide6 树**（§5.3）。
   我选了窄而实测的那一份。理由是"列出比实测更多的条目更糟"，
   但这**是一条策略判断**，如果发布页的读者需要的是二进制里的东西，该由复审定。

3. **softprops 停在 v2.6.2，而 v3.0.3 已存在**（§2.4）。判据 G 让我别动，我也没动，
   但 pin 让"落后一个大版本"这件事变得可见了。

---

## §11 本机环境的一处说明（不是仓库的问题）

★ 简报预言的 `.git/info/exclude` 规则**确实存在且比"静默拒绝"更糟**：

```
$ git add .github/dependabot.yml
The following paths are ignored by one of your gitignore files:
.github
hint: Use -f if you really want to add them.

$ git status --short
(完全没有输出 —— 不是 ?? ，是完全不可见)
```

★ **新文件在 `git status` 里根本不出现**，开发者可以提交其它所有东西而永远不知道这个文件被丢了。

★ **而且 `git add` 返回 rc=1，即使三个文件其实都已经暂存了**：

```
$ git add .github/workflows/test.yml tests/unit/test_supply_chain_guards.py
The following paths are ignored by one of your gitignore files: .github
add rc=1
$ git status --short
A  .github/dependabot.yml
M  .github/workflows/test.yml
M  tests/unit/test_supply_chain_guards.py      ← 三个都在暂存区里
```

**"暂存成功但退出码是失败"是最坏的组合**：`git add X && git commit` 会静默地什么都不提交。
我撞上了这两次（§3）。

规则在 `.git/info/exclude:43`，**未跟踪、不在 `.gitignore`**，所以：
**这是这台机器的事，不是仓库的事，其他 clone 不受影响。** `.github/dependabot.yml` 已用 `-f` 加入。

---

## 进度

```
M0  ─ M1  ─ M2  ─ M3  ─ M4  ─ M5  ─ M6  ─ M7  ─ M8  ─ M9  ─ M10 ─ M11 ─ M12 ─ M13 ─ M14  ✓
                                                                                    │
M15 依赖与供应链 ────────────────────────────────────────────────────────────────── ✓

M16 真实数据 · M17 benchmark · M18 注释收敛 · M19 文档 · M20 2.0 RC   未开始
```

---

## 给 M16 的输入

1. **`cyclonedx-py` 需要 `PYTHONIOENCODING=utf-8` 才能在本机打印任何东西**（§4.1）。
   下一个在 Windows 上调用第三方工具的人会撞上它。
2. **`git add` 在本机 `.github/` 上会 rc=1 但仍然暂存**（§11）——
   任何 `git add … && git commit` 的写法在这里都不成立。
3. **`tools/` 里的守卫现在有测试了**（`tests/unit/test_supply_chain_guards.py`，19 个），
   新守卫应当沿用这个形状：先变异验证能不能红，再落成测试固定契约。
4. **`refs/tags/*` 而不是 `refs/tags/`**（§8-1）——本机上这条命令的空结果是假答案。
5. **`git ls-remote` 到 github.com 在本机是间歇性的**（§6-2）：任何依赖它的检查都必须
   把"连不上"和"不合格"分成两种退出码，否则会被读成缺陷。