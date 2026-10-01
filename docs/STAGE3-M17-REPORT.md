# STAGE3-M17 报告 — Benchmark 可复现

> 阶段：Stage 3 / 里程碑 **M17**。前置：M0–M16 全部通过（`1c90960`）。
> **完成后停下，等复审。未开始 M18。**
>
> ★ **本里程碑最重要的结果在 §1，不在 §2。** 判据 A–D、F 的交付物都在，
> 而判据 C 的诚实标注顺带查出的一件事是：**CI 真正把门的那一个数字，
> 在本仓库里没有任何一次运行产生过它。**

---

## §1 ★ 判据 E 的实测，以及顺带查出的那件事

### 1.1 M14 的约束还在，而且它是对的

判据 E 要求先核实"performance job 保持单平台 ubuntu"这个约束是否还在。**在**：

| | |
|---|---|
| 跑 `perf_baseline.py` 的 job | **恰好一个**（`test.yml:385` `performance-baseline`） |
| `runs-on` | **`ubuntu-latest`**，无 matrix |
| 谁在强制 | `tools/ci_selfcheck.py shape`，静态断言唯一 / 单平台 / 无 matrix |
| 变异验证 | 给它一个带 `os: [ubuntu-latest, windows-latest]` matrix 的 workflow → **rc≠0**，消息里点名 performance job |

**为什么单平台是对的，本里程碑量到了：**

| 同一份工作（10 MB，29,090 条记录） | 吞吐 |
|---|---|
| 本机，Windows 11 | **43,070 rec/s** |
| 共享 ubuntu runner（已录参考值） | **19,312 rec/s** |

**差 2.23 倍。** 跨平台的吞吐数字不只是精度问题，是量错了对象。

★ **更要紧的是我把它测成了失败**：把参考值抬到本机的 3.2 倍（模拟"参考值来自一台更快的机器"），
`python benchmarks/perf_baseline.py` 在**代码一行未改**的情况下 **rc=1**：

```
FAIL
  - throughput 42,813 rec/s is below the 81,137 rec/s floor (60% of the recorded 135,228)
```

**脚本没有任何平台护栏。** 它只管量、只管比。所以"跨平台的吞吐回归门禁是错判据"不是理论——
它就是**把 job 挪到一台比录参考值慢的机器上，让它在未改动的代码上变红**。
这与 `perf-baseline.json` 自己 `note` 里记的那次事故是同一个形状（桌面 42,246 的门限
对上一台只跑得动 19,312 的 runner，代码未改而变红）。**M16 加的 Windows/macOS 腿因此不能延伸到这里。**

### 1.2 ★★ 顺带查出的：CI 那个数字不是脚本写的

给 `perf-baseline.json` 补身份块时，我要回答"这些数字的 commit 是什么"，于是去读脚本，
发现了一件与判据 C 直接相关的事。**用键集比对，不靠读说明文字**：

| | 脚本在**每一个存在过的提交**里都写 | 提交的文件里有 |
|---|---|---|
| 顶层 | `rates`、`peaks` | **无** |
| `measured_on` | `machine` | **无** |
| `measured_on` | — | `runner`、`note` |

`1d1b767`（创建）、`234150e`（最后更新）、`HEAD` 三处都核对过；
`benchmarks/perf_baseline.py` **从未**写过 `runner` 或 `note`；
仓库里没有第二个代码路径写这个文件。

★ **所以 `benchmarks/perf-baseline.json` 不是 `perf_baseline.py --update` 产生的。**
连带的第二件事更直接：**它没有 `rates`/`peaks`，也就是没有五次单次值**——
判据 B 明确要求的"全部单次运行值必须在 JSON 里"，**在 CI 真正把门的这个文件上不成立**，
而那五次运行已经不可恢复。

**这个结论的边界要说准**：它**不能**证明"从未有过一次运行"。
完全可能有人跑了脚本、读了它打印的数字、然后连同那两段说明一起手写进文件——
那段 `note` 是对一次真实事故的仔细记述，它不该被贬低。
**成立的是更窄也足够的那个说法：本仓库里没有任何一次运行产生过这个文件。**
这句话现在写在文件自己的 `provenance_of_these_numbers` 字段里，并有测试守住。

**为什么没有顺手修：** 修它需要在 CI job 里重录，而 CI 没跑、也不该由我声称结果。
检查路径现在**每次运行都打印一条 NOTICE**（原文见 §8）说明参考值无法重算，**但构建保持绿**——
因为为记账问题在未改动的代码上变红，正是 A11 要防的那种失败。
下一次 job 里的 `--update` 会补齐：两个脚本现在都写 `schema_version`、身份块、
生成器的 seed 与文档摘要、以及每一次单次运行的 rate 与 peak。

### 1.3 ★ 同一类缺陷的另外三处，我没有动

`benchmarks/bench_extraction.py:79`、`bench_inspect.py:42`、`bench_resident.py:62`
各有**同一个形状的回退**：

```python
peak = getattr(info, "peak_wset", None)
return (peak if peak is not None else info.rss) / (1 << 20)
```

`bench_extraction.py` 自己的注释说得很清楚：读当前 RSS "低 10-14%，而且根本不是峰值"。
而这三个脚本**只记 `peak_mb`，完全没有方法字段**——比 `run_comparison.py` 那处还少一层。

| | |
|---|---|
| 触发条件 | `psutil` 的 `memory_info()` 没有 `peak_wset`，即 **Linux 与 macOS** |
| 本机 / `windows-latest` | **不触发**（实测 `hasattr(info, "peak_wset")` 为 True） |
| 会不会让门禁变红 | **不会**——这三个脚本不在 CI 里跑（README：「They do not run in CI」） |
| 后果 | 在 Linux/macOS 上，这些脚本发布的 `peak_mb` 是当前 RSS，**低 10-14%，顶着峰值的名字** |

**我没有改它们**，两个理由：M17 判据 G 的范围是"只加身份与可追溯性"，
而这三个脚本不写带身份块的结果文件，没有地方挂这个字段；以及这属于"做完停下"的范围外。
**修法与我在 `run_comparison.py` 上做的是同一行形状。** 需要复审裁决（见 §9-3）。

---

## §2 判据 A：身份块的字段与来源

`benchmarks/provenance.py`（437 行）是唯一来源，两个结果文件都带 `schema_version` + `identity`。

| 字段 | 来源 | 为什么在里面 |
|---|---|---|
| `schema_version` | 常量 2 | 区分"这个文件早于身份块"与"这个文件有洞"，两者是不同的毛病 |
| `git_commit` | `git rev-parse HEAD` | **40 位 SHA，绝不写分支名**；工作树脏时字符串里写明，因为那时 commit 只是下界 |
| `gigaxml` | `gigaxml.__version__` | 读**包**而不是 `importlib.metadata`——editable 安装下后者报的是上次 `pip install -e` 的旧号 |
| `python` / `lxml` | `platform` / `lxml.etree` | 3.11 与 3.13 的差别比这里多数数字都大 |
| `os` / `cpu` / `machine` / `memory_total_gb` | `platform` / `psutil` | 取不到时降级为 `"unavailable"`，不抛异常 |
| `config_sha256` | **git blob**（文件 config）或 sha256（内联字符串） | 见下 |
| `config_sha256_source` | 一句话说明是哪种 | |
| `dataset_sha256` | 读进去的那些字节的摘要 + `generate` 命令与 seed | |
| `memory_method` | **哪个计数器应答、由谁读** | §4 |

★ **身份块永远不会让一次 benchmark 失败**：每个探针都降级成一句"读不到"的话。
一个因为 `git` 不在 PATH 就抛异常的身份块，会把"缺一个事实"变成"一次测量失败"——
那是反方向的错误交易。

### 2.1 ★ config 哈希取自 git blob，不是工作区——本项目第三次 CRLF 陷阱

本仓库 `core.autocrlf=true`。实测一次真实 clone：

```
blob    512 B  b7b51aa97e7c7ff52c7de4ef8ce0c93bab47a1bc6cdcc68452b668e146f9d647
clone   524 B  ee8035a80c83856b20715c19201304f283d85d00f32f35a0d00ebcb5bb170bb1
```

**照直记下来的 `config_sha256` 只在记录它的那台机器上为真**，换一个人第一次核对就失败。
这比不记更糟——它看起来像个身份。

★ 这是同一个陷阱第三次（M14 `.gitattributes`、M16 `fhir-all.xsd` 的 scratch 副本、这次），
**但这次它落在判据 A 唯一的交付物上**。一般形式写进方法论文档了：
**在 `core.autocrlf=true` 的仓库里，工作区不是那个文件；哈希工作区得到的是关于你这次检出的数，
不是关于这个项目的数。**

`perf_baseline.py` 的 config 是另一种情况：源码里 253 字节的字符串字面量，
没有工作区/blob 之分，直接哈希，字段里写明了这一点。
（顺手交叉验证：从 `234150e` 的 blob 里抽出那个字面量算出的摘要，与新脚本自己写出的**完全一致**。）

### 2.2 两个已录文件补身份块时，每一个测量值都逐字节未变

`results.json` **+19 −1**，`perf-baseline.json` **+18 −0**——不是整文件重写。
补块脚本在写完后重新读回并逐键比对，任何一个已录值变了就 `ABORT`；
我又从脚本外部独立算了一遍两个文件的测量值摘要，**改前改后相同**：
`2519a6a9…` 与 `31649810…`。

---

## §3 判据 B：原始值与重算，以及三个变异

**12 个数据点全部能从自己的 `runs[]` 精确重算**，包括两个难看的：
1 GB 的 pandas（五次全败，摘要是 4 键的失败形状）与 4 GB 的 pandas（`complete_output` 为假）。

★ **三个定义值得写下来，因为读者若按常识理解会"发现"一个并不存在的 bug：**

* **`p95` 在五次重复时就是最大值。** `p95_index = round(0.95 * (n - 1))`，`n=5` 时是 `n-1`，
  没有插值。
* **`records_per_s_median` 除的是已四舍五入的中位数。** 文件里看得见这个舍入，
  所以从原始中位数重算会在最后一位对不上，读起来像篡改。
* **只有 `exit_code == 0` 的运行参与统计。** 有一个数据点一条都没有。

**唯一不能仅从 `runs[]` 重算的字段是 `complete_output`**，它比较 `rows_written` 与
`rows_in_document`，后者是生成器的**期望**行数——是检查的输入，不是测量值。
它不是装饰：**正是它抓住了 4 GB 的 pandas 五次全部 exit=0 却只写出 10,485,760 / 11,915,264 行**
（少 1,429,504 行），而只看挂钟的检查会把那评成"慢一点的成功"。

### 3.1 三个变异，每个都跑在测试内部

| 变异 | 期望 | 实测 |
|---|---|---|
| 改一个原始 `wall_s`，摘要不动 | 重算必须红 | **红**（`median_s` 与 `stdev_s` 同时不符） |
| 删掉一次运行 | 重算必须红 | **红** |
| 把期望行数改成 pandas 实际写出的数 | `complete_output` 必须翻面 | **False → True** |

**变异写在测试里而不是测试旁边**，所以每次运行都自证这个检查有牙——
若检查被削弱到不再抓它的变异，证明它有牙的那个测试会先红。

---

## §4 ★ 判据 F：内存方法是约束还是文字

**简报问的是：有没有可执行的检查，还是只是文字？**

### 4.1 改之前的答案：**只是文字**

`run_comparison.py:114` 写的是硬编码字符串：

```python
"peak_rss_method": "peak_wset (self-read in child)",
```

而它的 WRAPPER 是：

```python
peak = getattr(info, "peak_wset", None) or info.rss
```

★ **那是一个真的回退分支**：在 psutil 没有 `peak_wset` 的平台上它返回**当前** RSS——
一个不同的量。而标签无论走哪条分支都写 `peak_wset`。
**一次悄悄走了回退的运行会被归档成峰值测量，且没有任何东西会发现。**

（今天这次记录的那组数字，标签**是真的**，而且是可判定的：`results.json` 记 `os: Windows 11`，
win32 上 `memory_info()` 有 `peak_wset`，所以走的是峰值分支。但当时那只是字符串，不是保证。）

### 4.2 现在的答案：有可执行的检查，三层

**第一层——方法不再是写死的。** 子进程现在报出**哪个计数器应答**：

```
__GIGAXML_PEAK__421892096 peak_wset
```

父进程从这一行里读出计数器名。**测量未变**（同一个计数器、同一个进程、同一时刻），
变的是记录说了什么——因为只有子进程知道。

**第二层——数字本身可执行地验证。** 测试跑真实的 `run_once()`，对手是一个分配 384 MiB 的子进程：

```
exit_code        0
peak_rss_mb      402.6          （384 MiB 分配 + 约 18 MiB 解释器）
stdout_tail      allocated 384 MiB
stderr_tail      '__GIGAXML_PEAK__422125568 peak_wset'
```

父进程读的 harness 会报约 4 MiB，**测试会红**。

**第三层——阈值不是猜的，且它的依据由使用它的测试重新测量。**
本机实测，一个分配 800 MiB 并自读的子进程：

| | |
|---|---|
| 子进程自读 `peak_wset` | **817.9 MiB** |
| 父进程看同一个活着的子进程，5 次采样 | **4.1, 4.1, 4.1, 4.1, 4.1 MiB** |
| 差 | **813.8 MiB** |

那个 4.1 正是 `run_comparison.py` 注释里说"曾对每个实现每个尺寸都报"的冻结值。
**变异**：把记录下来的 4.1 喂给阈值检查，断言它被拒——否则每个判据 F 的守卫都是装饰。

`perf_baseline.py` 的声明用结构方式验证：`PROBE` 交给 `python -c` 并在那里调用
`gigaxml.run.peak_rss_mb()`，测试断言 `run_once` **完全没有引用**
`psutil`、`memory_info`、`getrusage`、`PeakWorkingSet`、`VmHWM`——
父进程不许碰任何内存 API。

### 4.3 这道探针区分什么、不区分什么

它区分**哪个进程读了计数器**——那正是判据 F 的内容。
它**不**区分**峰值 vs 当前值**：在那次探针里子进程的 `peak_wset` 与 `rss` 相等，
因为读数是在分配仍被持有时取的。两个区分都有价值，**只有第一个是这些守卫的根据**。
这句话写进了方法论文档和测试注释。

---

## §5 判据 C：`benchmarks/history/` 与诚实的"无记录"

| 版本 | 发行 | `recorded` | 依据 |
|---|---|---|---|
| 0.9.0 | 2026-09-27 | **false** | 当时还不存在任何 benchmark 结果文件 |
| 1.0.0 | 2026-09-28 00:02 | **false** | 第一个结果文件晚 4 小时（`51609be`，04:02） |
| 1.1.0 | 2026-09-28 01:15 | **true** | 恢复得到，见下 |
| 1.2.0 | 2026-09-30 19:22 | **false** | 最后一次测量早 1 天 20 分钟 |
| 1.2.1 | 2026-10-01 01:31 | **false** | 最后一次测量早 2 天 |

★ **五个版本里四个没有记录，其中三个是在最后一次测量之后发行的。**
每个 `false` 条目都带**确立这个"没有"的提交日期**，所以这句话可被核对，而不是一句保证。
**没有任何条目包含未被测量的数字**——一个测试遍历全部 `false` 条目，出现任何数值就红。

### 5.1 唯一那条记录：commit 恢复了，数据集没恢复

* **commit —— 恢复了，方法是可复述的。** 那次扫描自己的 `recorded_at` 是
  `2026-09-28T19:07:29+00:00`，而提交时间线在那一刻有个空隙：
  `82e31eb` 是前一天 18:25（本地），`53c5b21` 是次日 08:39。**中间只可能有一个 HEAD。**
  ★ **这是下界，不是证明**——那一刻的工作树未必与它一致，文件里这么写了。
* **工具版本 —— 恢复了，并指出原值是错的。** 从那个 commit 读出 1.1.0；
  文件自带的 `environment.gigaxml` 写 0.1.0，`e837d7c` 修的正是这个读法，
  在扫描结束五个半小时之后。**错值留在原处**，旁边记正确的那个——那才是当时写下的东西。
* **config 哈希 —— 真的。** 该 config 历史上只有一次提交，且在 `82e31eb` 与 `HEAD` 逐字节相同。
* **数据集 —— 没了，且不可恢复。** `data/b100m.xml` 及两个更大的兄弟文件已删；
  `run_comparison.py` 从未哈希它们、从未记 `generate` 命令或 seed。
  `gigaxml generate` 在已知 seed 下**确实逐字节可复现**（实测两次相同），
  **但没记下来的 seed 无法从摘要反推**。这是这条不变量唯一补不上的字段，文件照实说。

### 5.2 ★ 版本字符串不是键

`benchmarks/history/1.1.0.json` 以录制时的版本字符串命名并记 `"1.1.0"`，
**而它测的那棵树在 `v1.1.0` tag 之后 6 个提交**，两处 `pyproject.toml` 都读 `1.1.0`。
**它测的不是 1.1.0 发行版。** 键是 commit，文件里写明了，
并且有一个测试**对着仓库核实那个偏移量**（`git rev-list --count`），而不是相信那句话。

---

## §6 判据 D：两套门限，以及一处文档不一致

| | 用于 | 吞吐 | 内存 |
|---|---|---|---|
| **正式对比** | 受控机器上的两次运行 | 超过 **−20%** 算显著 | 超过 **+25%** 算显著 |
| **CI** | 共享 runner 上的一个 job | 低于记录参考值的 **60%** 失败 | 超过 **60 MiB** 绝对值失败 |

两者不可互换，测试里断言它们不是同一个数，且 CI 那个数在代码里就是 `0.60`。

**没测到的峰值按"没比"报告，不按 0 算。** `is_significant()` 在任一侧缺失时返回
`memory_significant: None` 并说明缺的是哪一侧——填成 `0.0` 会让之后每一次比较读成"大幅改善"。

★ **一处文档不一致，我记录而没有擅自调和：** `benchmarks/README.md` 与代码说 60%，
`performance-baseline` job 的注释与路线图 A11 说 50%。**跑的是代码，60%**，
也就是**允许掉 40%**，不是 50%。按 §3「不改 CI 门限」我没有动它——**这是 M19 的文档修正**。

---

## §7 判据 G 与范围

| | |
|---|---|
| `src/` `pyproject.toml` `packaging/` 改动 | **无**（`git diff 1c90960..HEAD -- src/ pyproject.toml packaging/` 为空） |
| 版本 / 运行时依赖 / 产品 CLI flag | **未动** / `1.2.1` / `lxml>=5.0`、`pyyaml>=6.0` / **无新增** |
| 是否重跑 4 GB 对比 | **没有**。`run_comparison.py` 的新代码路径用 **100 MB / 1 次 / 1 个实现**验证，输出写到 scratch，**已录 `results.json` 全程未被覆盖** |
| 是否跑 `--update` 覆盖 CI 参考值 | **没有**，且不该跑——那会用本机 Windows 数字覆盖 runner 的 Linux 参考值，正是该文件 `note` 警告的事故 |
| 新依赖 | 无 |
| 改动范围 | 14 个文件，**+2119 −6** |

**为什么改 `perf_baseline.py` 与 `run_comparison.py` 不算违反 §3 的"不改测量逻辑"：**
改的是**写出什么**，不是**怎么量**——门限常量、计数器、时机、进程结构全部未动，
`--seed 0` 显式传入与默认值实测逐字节相同。**而判据 A 要求字段存在，
唯一能让它不被下一次 `--update` 抹掉的办法就是让脚本自己写。**

---

## §8 本次重跑的数字

```
$ pytest -q --cov=src/gigaxml --cov-report=term-missing --cov-fail-under=95 \
      --junit-xml=ci-report.xml tests/unit tests/integration tests/golden \
      tests/security tests/property
1828 passed in 240.23s (0:04:00)
Required test coverage of 95% reached. Total coverage: 95.66%

$ tools/ci_selfcheck.py report ci-report.xml --min-tests 1700 --max-skips 40
  total 1828 | passed 1828 | skipped 0 | failed or errored 0            rc=0
$ tools/ci_selfcheck.py report ci-report.xml --min-tests 1828 --max-skips 0
  total 1828 | passed 1828 | skipped 0 | failed or errored 0            rc=0
$ tools/ci_selfcheck.py shape
  shape: clean. 13 jobs, 2 workflow files, 5 shared test directories.  rc=0
$ ruff check .          → All checks passed!                          rc=0
$ ruff format --check . → 223 files already formatted                  rc=0
```

**CI 那个 job 本身，本机实跑（就是 CI 跑的那条命令）：**

```
$ python benchmarks/perf_baseline.py
records      29,090
throughput   43,070 rec/s (median of 5)
peak memory  26.7 MiB (max of 5)
recorded at   234150e on Linux (as recorded in measured_on.system) (schema 2)
NOTICE       this reference cannot be recomputed: this baseline has no 'rates', so its
             summary cannot be recomputed from anything. It records repeats=5 and a median,
             and the individual runs behind them are gone. Re-record it with
             perf_baseline.py --update from the CI job, which writes both lists.
reference    19,312 rec/s
required     11,587 rec/s (60% of reference)
OK: within the 60% throughput floor and the memory ceiling                rc=0
```

| 项 | 数值 |
|---|---|
| 全树 | **1828 passed**（1800 + 新增 28），skipped 0 / failed 0 |
| 覆盖率 | **95.66%**（与上轮相同；`src/` 未改） |
| `results.json` 摘要可从自身原始值重算 | **12/12** |
| 重算检查的三个变异 | **3/3 变红** |
| `perf-baseline.json` 可重算 | **否**，且文件与测试都记着这一点 |
| 判据 F：父读 vs 自读 | **817.9 vs 4.1 MiB**（5 次采样全 4.1），差 813.8 |
| 判据 F：harness 对 384 MiB 子进程记录 | **402.6 MiB**，计数器名由子进程报出 |
| 判据 E：性能 job | **1 个**，`ubuntu-latest`，无 matrix；带 matrix 的变异 **rc≠0** |
| 判据 E：同工作跨平台吞吐 | 本机 **43,070** vs runner **19,312** rec/s（**2.23×**） |
| 判据 E：参考值来自 3.2× 快的机器 | **rc=1**，未改动的代码变红 |
| `history/` 条目 | **5 个**，1 条记录 / 4 条"无记录"，无记录条目里**零个数字** |
| 提交 | 每次独立确认 `git log` 真的前进，attribution **全 0**；总数与 SHA 见 `git log 1c90960..HEAD`（本报告的提交在列，报告自身在写完时又追加过，所以这里不写死数字） |
| 领先 origin/main 且 **未 push** | **34** |
| `data/` | 跑完全树后 **531 MB**（见 §9-4） |
| 清理 | `.scratch/m17/` 已删（含一次真实 clone、8.5 MB） |

---

## §9 需要复审裁决的

1. ★ **`perf-baseline.json` 的 19,312 rec/s 与 24.4 MiB 在本仓库没有运行产生过它**（§1.2）。
   严重度我压着说：**不是安全缺口，也不是门禁失效**——检查正常工作、构建是绿的；
   但它是本项目在 CI 里唯一断言的性能数字，而它**不可重算、不可追溯**。
   两条路：(a) 在 CI job 里 `--update` 重录一次，让它变成真记录（**推荐**，一次运行的事）；
   (b) 就这样留着，把 NOTICE 作为已知状态。请定。
2. ★ **三个主 benchmark 脚本的同一类回退缺陷**（§1.3）。修法与我在 `run_comparison.py`
   上做的是同一行形状，但要动三个文件，且它们不写带身份块的结果文件。
   本里程碑按范围没动。**请定是否在 M19/M20 一并修。**
3. **`results.json` 的 `dataset_sha256` 永远补不上**（§5.1）。文件照实写着。
   这不是待办，是一个**永久的、已记录的洞**——除非有人还记得当年用的 seed。
   复审若认为该记入 `M20` 的已知限制清单，请指示。
4. **`data/` 里 516 MB 是本轮跑全树重新生成的**（`tests/performance/` 产出
   `s10/s100/s400.xml`）。复审上轮清理过它们；**跑一次全树就会回来**，
   所以这不是一个能靠删除解决的事实。要么接受，要么把性能测试的产物目录改到临时位置——
   那是 M18/M19 的事，本里程碑没动。
5. **50% / 60% 的文档不一致**（§6）。按纪律没动 CI 门限，交给 M19。

---

## §10 我自己错的九处

| # | 我以为 | 实测 |
|---|---|---|
| 1 | `blob_sha256()` 收一个路径参数 | 收 `(repo_root, relative_path)`，`TypeError`，冒烟跑直接崩。改后重跑 |
| 2 | ★ **`perf-baseline.json` 的摘要"可以从原始值重算"** | **我先写了这句话，后才检查有没有原始值——没有。** 那是一句**我在测量之前就写下的假话**，已改，并把"文件里没有 `rates`"改成承重的断言 |
| 3 | 我的判据 E 变异测试用 `read_text`/`write_text(newline="")` 还原工作流是安全的 | ★ **它把 423 个 CRLF 写成了 LF。** 内容相同、测试通过、CI 会通过，而**工作树被留在改动状态**——是之后的 `git status` 发现的，不是测试发现的。现在按字节读写，且变异锚点匹配检出实际的行尾 |
| 4 | 判据 F 的那个测试是空的 | 它不是空的（harness 记 402.6 MiB）。但**我一度这样怀疑**，因为我自己的探针被 shell 转义毁了，脚本崩在 `SyntaxError` 上却报出一个"合理"的 17.8 MiB。**处置不是相信结论，是加一道 `compile()` 守卫**——脚本先编译，失败落在出错的行上 |
| 5 | `provenance.py` 里用 `__import__("statistics")` 省事 | 立刻改成正常 `import statistics` |
| 6 | 我那 28 个测试一次就写对了 | **5 个失败，全是我的断言太粗**：匹配我自己写的措辞（`"NOT RECORDED" in ...`）、用子串找 `"peak"` 撞上文档串、对 4 键失败形状用 `[...]` 取键、把"有原始运行"写成"有成功的运行"。**都不是数据的问题**，逐个改准 |
| 7 | 父读探针的阈值 150 MiB 余量够 | 实测子进程自读 209.5 MiB，余量只有 59。探针加到 32 块、断言提到 190 |
| 8 | 我的 `FROZEN` 探针能用 | 用了 `{{}}` 但那串没走 `.format()`，`TypeError`。改成单括号 |
| 9 | 那次 800 MiB 的父读/自读对比是"一次测量" | 它是真的，但**在 Linux 与 macOS 上父读可能不冻结**。我只在 Windows 上测了，方法论文档里写明了这一点，没有把它说成跨平台结论 |
| 10 | ★ 我给一条 `git add && git commit` 链标了 `commit rc=$?` | **那条 rc 是整条链的，不是 commit 的。** `git add -f docs/…` 返回 **1**（`.git/info/exclude` 的 `/.??*`，复审上轮亲手复现过的那个陷阱），链短路，`echo "commit rc=1"` 于是把 add 的失败报成了 commit 的失败。**而且这次是更坏的形态：rc=1 且根本没有暂存**——重试同一命令 rc=0 才成功。**是"每次提交后独立确认 `git log` 真的前进了"这条纪律抓住的**，不是 rc |

★ **第 10 条是复审自己写进 M16 纪律的那个陷阱第五次复发**，值得单列：
本仓库的 `.git/info/exclude` 里有 `/.??*`，它让 `git add` 在**没有把文件加入索引**的情况下返回 1。
复审上轮记的是"rc=1，即使文件其实已暂存"；**本次量到的是没有暂存的那一种**，
所以"看 rc"和"看 `git log`"这两件事必须都做，而只有后者能区分它们。
**另外一条从这次学的：不要把 `&&` 链的 rc 标成某一个命令的名字。**
链上任何一步短路，后面每一步的 rc 都不会执行，而我把那个数字归给了最后一步。

★ **第 2 条与第 3 条是同一件事的两面**：第 2 条是我**先下结论后测量**，
第 3 条是我的检查**通过了而仓库仍然是坏的**。两者都是"绿色不等于对"，
而 M15 立的规矩是"看到没红先量再判"——这次量出来的是**绿的本身有问题**。

★ **第 4 条是 M16 那条教训的第三次**，也是第一次落在**判据的核心检查**上：
我一度要靠一个"看起来很合理"的数字（17.8 MiB）下结论。**唯一挡住它的是
我坚持去量那个数字本身，而不是接受测试通过。**

---

## §11 未验证 / 需要 push 或受控机器的项

| # | 项 | 说明 |
|---|---|---|
| 1 | **CI runner 上的结果** | **未 push，CI 一次没跑。** §8 里的"CI 那个 job 本机实跑"是**本机 Windows** 的运行，路径与输出格式相同，**不是 runner 的结果** |
| 2 | **下一次 job 里 `--update` 之后基线长什么样** | 脚本会写出 `rates`/`peaks`/身份块/seed/摘要——**本机已用 scratch 路径验证**（2MB/5 次，rc=0，摘要可重算），**但从未在 runner 上执行过** |
| 3 | `perf_baseline.py` 在 Linux 上的身份块内容 | `identity()` 在本机跑通；Linux 上的 `cpu`/`os` 字段会不同（`platform.processor()` 在 Linux 上常为空，代码回退到 `platform.machine()`——**这一条未在 Linux 上验证**） |
| 4 | README 记的"per-record 循环把吞吐打到 51%" | **是 README 的说法，不是本里程碑的测量。** 方法论文档里已标注。再验证它意味着故意改产品热路径 |
| 5 | `bench_extraction.py` 等三个脚本在 Linux/macOS 上的实际峰值偏差 | §1.3 的推断基于 `psutil` 在 win32 有 `peak_wset`、代码在缺失时回退。**未在 Linux 上实跑** |
| 6 | `data/` 在 CI 上会被拉到多大 | 工作流未改；`benchmarks/compare/` 的数据本来就不在 CI 里生成 |

---

## 进度

```
M0 ─ M1 ─ M2 ─ M3 ─ M4 ─ M5 ─ M6 ─ M7 ─ M8 ─ M9 ─ M10 ─ M11 ─ M12 ─ M13 ─ M14 ─ M15  ✓
                                                                                          │
M16 真实数据矩阵 ─────────────────────────────────────────────────────────────────────────  ✓
                                                                                          │
M17 benchmark 可复现 ──────────────────────────────────────────────────────────────────────  ⚠
   （判据 A–F 完成；★ 查出 CI 那个数字不是脚本写的、三个主脚本有同一类回退缺陷，
     两者都如实记录未修；见 §9）

M18 注释收敛 · M19 文档 · M20 2.0 RC   未开始
```

---

## 给 M18 的输入

1. ★ **`core.autocrlf=true` 下工作区不是那个文件**（§2.1，M14/M16/本次第三次）。
   任何哈希、任何"入库前后字节会变"的判断，都必须说清哈希的是 blob 还是工作区。
   **本轮最贵的一次教训就是这个**（§10-3）。
2. **改动一个 tracked 文本文件做变异测试，要按字节读写**（§10-3）。
   `read_text` + `write_text(newline="")` 会静默改写行尾，测试照样绿。
3. **★ `benchmarks/bench_extraction.py` / `bench_inspect.py` / `bench_resident.py`
   都有 `run_comparison.py` 那个回退缺陷，且连方法字段都没有**（§1.3）。
   M18 收敛注释时，这三处的注释与 `peak_mb` 字段名是同一件事的两半。
4. **阈值与文档不一致时，按纪律不动阈值，把不一致记下来**（§6）。
   本项目的规矩是"不伪造未验证的东西"，它对**过时的描述**同样适用。
5. **性能测试每次跑全树产出 516 MB**（§9-4）。这不是能靠删除解决的问题。
6. ★ **`.git/info/exclude` 的 `/.??*` 让 `git add` 返回 1 而不暂存**（§10-10）。
   复审上轮记的是"rc=1 但已暂存"，**本次量到的是没有暂存的那一种**。
   两件事都要做：**看 rc，也独立确认 `git log` 前进了**——只有后者能区分它们。
   并且**别把 `&&` 链的 rc 标成某一个命令的名字**。
