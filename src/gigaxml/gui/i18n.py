"""Interface text in the user's language.

**The mechanism, and why it is this and not Qt's.** Every panel in ``gigaxml.gui`` builds
its widgets by hand in Python -- there are no ``.ui`` files -- so Qt's ``retranslateUi``
machinery has nothing to hook onto, and a runtime switch would mean hand-writing a
retranslate pass over every widget anyway, with every missed widget silently showing two
languages at once. Instead each string is wrapped in :func:`tr` at the moment it is put on
a widget, and the language is read once, when the window is built. **Changing the language
takes effect on the next launch**; the settings panel says so beside the control.

**The keys are the English strings themselves.** That is what makes the fallback rule work
without any bookkeeping: a string with no entry in the current language's table, or a
language with no table at all, comes back exactly as it went in -- which is the English
text. There is no way to get a blank label or a symbolic key out of this module, which is
the property the project's rule "untranslated falls back to English" actually needs.

**Nothing here imports PySide6**, for the same reason ``settings.py`` does not: the table
and the lookup are testable without a display, and a test that needs a window is a test
that does not get run.

**Values are never translated.** Text that reaches a config file, a CLI argument or a
stored setting -- ``abort``, ``quarantine``, ``csv``, record paths, type names -- is data,
and this module has nothing to say about it. Where a combo box shows a translatable label
for such a value, the label goes through :func:`tr` and the value rides in the item's
``data``, read back with ``currentData``/``findData``; that separation lives in the panels,
not here.
"""

from __future__ import annotations

__all__ = ["current_language", "set_language", "tr"]

#: The language this module falls back to, and the table-less identity: English strings
#: displayed as themselves. The settings module owns the user-facing default; this is only
#: what an unknown stored value lands on.
FALLBACK_LANGUAGE = "en"

#: The translations. Keys are the English source strings, character for character -- the
#: same strings the panels pass to :func:`tr`, including ellipses and em dashes. A key that
#: is missing here displays as English rather than as anything broken, which is why adding
#: a new string to a panel does not require touching this file to stay correct.
ZH: dict[str, str] = {
    # -- main window -------------------------------------------------------
    "Document": "文档",
    "Structure": "结构",
    "Fields": "字段",
    "Preview": "预览",
    "Execute": "执行",
    "Batch": "批量",
    "Settings": "设置",
    "&File": "文件(&F)",
    "&Open document…": "打开文档(&O)…",
    "&Save config…": "保存配置(&S)…",
    "&Quit": "退出(&Q)",
    "&Help": "帮助(&H)",
    "&About": "关于(&A)",
    "ready": "就绪",
    "opened {}": "已打开 {}",
    "copied {}": "已复制 {}",
    "on_error set to quarantine": "on_error 已设为 quarantine",
    "press Analyse on this tab to see what the document declares": (
        "此文档尚未分析：请在本页按「分析」查看它声明了什么"
    ),
    "the document's namespaces are listed here": "文档使用的命名空间列在此处",
    "the config is in the Fields tab": "配置在「字段」页",
    "GigaXML {} — the CLI does the work": "GigaXML {} — 实际工作由 CLI 完成",
    "{}: not built yet": "{}：尚未实现",
    # -- execution panel ----------------------------------------------------
    "Source": "源",
    "an .xml or .xml.gz file": ".xml 或 .xml.gz 文件",
    "Browse…": "浏览…",
    "Input file": "输入文件",
    "a YAML or JSON config": "YAML 或 JSON 配置",
    "Config": "配置",
    "Record path (for the total)": "记录路径（用于总数）",
    "Used only to find the total for the progress bar, by asking `inspect`. "
    "The extraction itself takes the record path from the config.": (
        "仅用于向 inspect 询问记录总数以驱动进度条；提取本身使用的记录路径来自配置文件。"
    ),
    "Output": "输出",
    "where to write the rows": "行数据的写出位置",
    "Format": "格式",
    "abort": "中止",
    "quarantine": "隔离",
    "What to do when one record cannot be extracted. Opening a config sets this to "
    "what that config says; changing it afterwards overrides the config, and the "
    "line underneath says so.": (
        "一条记录无法提取时的处理方式。打开配置时会设为该配置声明的值；"
        "之后再改动会覆盖配置，下方一行会说明这一点。"
    ),
    "On error": "出错时",
    "off": "关闭",
    "Commit the output in parts of this many records, so an interrupted run can "
    "be continued. Off writes a single file.": (
        "把输出按每这么多条记录一份分批落盘，中断后可续跑。关闭则写单个文件。"
    ),
    "Checkpoint every": "检查点间隔",
    "Resume the run already in that directory": "续跑该目录中已有的运行",
    "Continue the run a checkpoint was made from. The tool refuses if the source "
    "or the config has changed since, and says exactly what differs. Never turned "
    "on for you: resuming is a decision, not a default.": (
        "从检查点继续之前的运行。若源文件或配置此后有变动，工具会拒绝并准确说明差异。"
        "绝不会替你勾选：续跑是一个决定，不是默认。"
    ),
    "Progress": "进度",
    "not started": "未开始",
    "Start": "运行",
    "Cancel": "取消",
    "choose an input and an output first": "请先选择输入与输出",
    "config error: {}": "配置错误：{}",
    "starting…": "正在启动…",
    "cancelling…": "正在取消…",
    "cancelled": "已取消",
    "finished — {} rows": "已完成 — {} 行",
    "finished": "已完成",
    "failed with exit code {}": "失败，退出码 {}",
    "part": "个部分",
    "parts": "个部分",
    "There is an unfinished run in this directory: {} {}, {} rows, {} records "
    "consumed. Tick Resume to continue it, or choose another directory.": (
        "此目录中有一个未完成的运行：{}{}，{} 行，已消费 {} 条记录。"
        "勾选「续跑」以继续，或另选目录。"
    ),
    "the run will use {}, overriding the config's {}": "本次运行将使用 {}，覆盖配置中的 {}",
    "this config sets on_error to {}, replacing your choice of {}": (
        "此配置将 on_error 设为 {}，替换了你此前选择的 {}"
    ),
    "the run will use {}, not your earlier choice of {}": "本次运行将使用 {}，而非你此前选择的 {}",
    "Choose a document": "选择文档",
    "XML documents (*.xml *.xml.gz);;All files (*)": "XML 文档 (*.xml *.xml.gz);;所有文件 (*)",
    "Choose a config": "选择配置",
    "Configs (*.yaml *.yml *.json);;All files (*)": "配置 (*.yaml *.yml *.json);;所有文件 (*)",
    "Where to write": "写出位置",
    "{} records": "{} 条记录",
    " of {}": "（共 {}）",
    "{} rows": "{} 行",
    "{} rejected": "{} 条已隔离",
    "part {}": "第 {} 部分",
    # -- settings panel -----------------------------------------------------
    "Default batch size": "默认批大小",
    "How many records the CLI holds before writing. Larger is faster and uses more "
    "memory; the tool warns above the point where that matters.": (
        "CLI 在写出前暂存的记录数。越大越快，内存占用也越大；超过有影响的阈值时工具会给出警告。"
    ),
    "Default output format": "默认输出格式",
    "Default on error": "默认出错策略",
    "Theme": "主题",
    "Follow the system": "跟随系统",
    "Light": "浅色",
    "Dark": "深色",
    "Language": "语言",
    "Takes effect after restart": "重启后生效",
    "Kept in {}": "保存在 {}",
    # -- batch panel --------------------------------------------------------
    "config file": "配置文件",
    "Add documents…": "添加文档…",
    "output directory": "输出目录",
    "State": "状态",
    "Detail": "详情",
    "No documents queued.": "尚未排队任何文档。",
    "Add documents": "添加文档",
    "{} done, {} failed, {} skipped, {} to go": "完成 {}，失败 {}，已跳过 {}，待运行 {}",
    "exit {}": "退出码 {}",
    "no result": "无结果",
    "waiting": "等待",
    "running": "运行中",
    "done": "完成",
    "failed": "失败",
    "skipped": "已跳过",
    # -- document panel -----------------------------------------------------
    "Open document…": "打开文档…",
    "Recent documents": "最近文档",
    "Remove from list": "从列表移除",
    "Clear list": "清空列表",
    "or drag an .xml or .xml.gz file onto this window": "或将 .xml / .xml.gz 文件拖入本窗口",
    "Open a document": "打开文档",
    "or": "或",
    "not a document: {} (expected an existing {} file)": "不是文档：{}（应为确实存在的 {} 文件）",
    "no document open": "未打开文档",
    "that drop carried no file": "拖入的内容不包含文件",
    "{}  (missing)": "{} （缺失）",
    "{} of these files are not there at the moment. They are kept so you can see "
    "what you had; opening one will say so rather than fail silently.": (
        "其中 {} 个文件目前不在原处。保留它们是为了让你能看到曾打开过什么；"
        "打开时会如实说明，而不是静默失败。"
    ),
    "modified {}": "修改于 {}",
    "unknown": "未知",
    # -- document info ------------------------------------------------------
    "analysis reads the whole document: expect roughly {} min ({} MiB at about {} MiB/s)": (
        "分析会读取整个文档：预计约 {} 分钟（{} MiB，约 {} MiB/s）"
    ),
    "analysis reads the whole document: expect roughly {} s ({} MiB at about {} MiB/s)": (
        "分析会读取整个文档：预计约 {} 秒（{} MiB，约 {} MiB/s）"
    ),
    # -- structure panel ----------------------------------------------------
    "Analyse": "分析",
    "max paths": "路径上限",
    "default": "默认",
    "Stop tracking distinct paths after this many. Off uses inspect's own default. "
    "Hitting the cap is reported in the warnings, never silently.": (
        "到这个数量后不再跟踪新路径。关闭时使用 inspect 自身的默认值。"
        "触顶会在警告中说明，绝不静默。"
    ),
    "no document analysed": "尚未分析文档",
    "filter rows by path…": "按路径筛选行…",
    "Record candidates": "记录候选",
    "Selected candidate": "选中的候选",
    "Namespaces": "命名空间",
    "All paths": "全部路径",
    "Export paths as CSV…": "导出路径为 CSV…",
    "ready to analyse {}": "待分析 {}",
    "no document": "无文档",
    "open a document first": "请先打开文档",
    "analysing…": "正在分析…",
    "inspect failed with exit code {}": "inspect 失败，退出码 {}",
    "{} elements · {} candidates · {} paths": "{} 个元素 · {} 个候选 · {} 条路径",
    "yes": "是",
    "no": "否",
    "[inside {}]": "[位于 {} 内]",
    "no candidate records were found, so there are no namespaces to show": (
        "未发现候选记录，因此没有可显示的命名空间"
    ),
    "the records found use no namespace prefixes": "找到的记录未使用命名空间前缀",
    "rebound prefixes (they mean more than one thing):": "被重定义的前缀（它们代表不止一种含义）：",
    "namespaces used with no prefix:": "未加前缀使用的命名空间：",
    "namespaces used with no prefix": "未加前缀使用的命名空间",
    "see {}": "见 {}",
    "path": "路径",
    "score": "得分",
    "count": "数量",
    "shape consistency": "形状一致性",
    "nested inside": "嵌套于",
    "depth": "深度",
    "children": "子元素数",
    "distinct shapes": "不同形状数",
    "Export paths": "导出路径",
    "child elements": "子元素",
    "attributes": "属性",
    "namespaces": "命名空间",
    "(none)": "（无）",
    "(top level)": "（顶层）",
    "why it was proposed": "入选原因",
    "example values": "示例值",
    "sampling…": "正在采样…",
    "could not be sampled: {}": "无法采样：{}",
    "the sample held no records, so there is nothing to show": "样本中没有记录，无内容可显示",
    "example values (first {} of a sample)": "示例值（样本前 {} 条）",
    "({} record(s) were rejected)": "（{} 条记录被隔离）",
    "occurrences": "出现次数",
    "repeat score": "重复得分",
    "sampling was cancelled": "采样已取消",
    "sampling failed with exit code {}": "采样失败，退出码 {}",
    # -- fields panel -------------------------------------------------------
    "Record": "记录",
    "record path": "记录路径",
    "From candidate": "来自候选",
    "Fill the record path and the field list from the candidate selected in the "
    "structure panel: its direct children and its attributes.": (
        "用结构面板中选中的候选填充记录路径与字段列表：其直接子元素与属性。"
    ),
    "Add": "添加",
    "Delete": "删除",
    "Move up": "上移",
    "Move down": "下移",
    "Save config…": "保存配置…",
    "field name": "字段名",
    "type": "类型",
    "required": "必填",
    "select a candidate in the structure panel first": "请先在结构面板中选择一个候选",
    "asking the CLI for a config…": "正在向 CLI 请求配置…",
    "the CLI could not write a config for this candidate": "CLI 无法为该候选写出配置",
    "the CLI reported success but wrote no config": "CLI 报告成功但没有写出配置",
    "this is a config the CLI accepts — {} fields": "这是 CLI 可接受的配置 — {} 个字段",
    "Save config": "保存配置",
    # -- preview panel ------------------------------------------------------
    "rows": "行数",
    "How many records to sample, passed to `sample -n`.": "采样多少条记录，传给 sample -n。",
    "nothing sampled yet": "尚未采样",
    "waiting for {} and {}": "等待 {} 与 {}",
    "waiting for {}": "等待 {}",
    "a document": "文档",
    "a config the CLI accepts": "CLI 可接受的配置",
    "ready to preview": "可以预览",
    "{} row(s) shown": "显示 {} 行",
    "the CLI wrote {}": "CLI 写出了 {}",
    "the document ran out before the limit": "文档在达到条数限制前结束",
    "sample failed with exit code {}": "sample 失败，退出码 {}",
    "Sample": "样本",
    "Rejected ({})": "已隔离 ({})",
    "No records were rejected in this sample. Rejections are only collected when "
    "the config sets on_error: quarantine, and only if some record fails to "
    "extract.": (
        "本次样本中没有记录被隔离。只有配置设置 on_error: quarantine 且确有记录提取失败时，"
        "才会收集隔离记录。"
    ),
    # -- results panel ------------------------------------------------------
    "finished — {}": "已完成 — {}",
    "the tool wrote no summary for this run": "本次运行没有写出摘要",
    "this run did not finish": "本次运行未完成",
    "It was stopped before it wrote anything, so there is no output to look at.": (
        "它在写出任何内容之前就被停止，因此没有输出可查看。"
    ),
    "Copy the output path": "复制输出路径",
    "Copy the path of the parts directory": "复制分片目录的路径",
    "Copy the path of the partial output": "复制部分输出的路径",
    "rejected: {}": "已隔离：{}",
    "rejected: {} — see {}": "已隔离：{} — 见 {}",
    "nothing was rejected": "没有记录被隔离",
    "took {}": "用时 {}",
    "written as {}": "写出为 {}",
    "{} row": "{} 行",
    "{} record": "{} 条记录",
    "{} part": "{} 个部分",
    "{} parts": "{} 个部分",
    ", {} rows in them": "，其中 {} 行",
    "The parts it finished are in this directory. It is not the whole run: the "
    "manifest does not say the source was consumed.": (
        "已完成的部分在此目录中。这不是完整的运行：清单并未表明源文件已被完整消费。"
    ),
    "The {} it finished are in this directory{}. It is not the whole run: the "
    "manifest says the source was not fully consumed, so what is here is where it "
    "got to.": (
        "已完成的{}在此目录中{}。这不是完整的运行：清单表明源文件尚未被完整消费，"
        "当前内容即它进行到的位置。"
    ),
    "The output file was started, but the run was stopped before anything was "
    "written to it. The target still holds whatever it held before.": (
        "输出文件已创建，但运行在写入任何内容之前就被停止。目标路径仍保留着之前的内容。"
    ),
    "Part of the output is on disk, {} of it. It is not the finished file: the run "
    "never got to the point of putting it in place, so the target still holds "
    "whatever it held before. Nothing else on the disk was changed.": (
        "磁盘上有一部分输出，共 {}。这不是完成的文件：运行尚未到把它就位的步骤，"
        "目标路径仍保留之前的内容。磁盘上没有其他改动。"
    ),
    "{} bytes": "{} 字节",
    # -- error panel and advice ---------------------------------------------
    "Show the raw output": "显示原始输出",
    "Hide the raw output": "隐藏原始输出",
    "the tool did not say what kind of failure this was": "工具未说明这是哪类失败",
    "the tool called this: {}": "工具将其归类为：{}",
    "Set on_error to quarantine": "将 on_error 设为 quarantine",
    "Copy the path of the complete output": "复制完整输出的路径",
    "Go to the namespace panel": "前往命名空间面板",
    "Go to the field panel": "前往字段面板",
    "Copy the path of the checkpoint": "复制检查点的路径",
    "Show the details": "显示详情",
    "A record did not match the field types you gave": "有记录与你给定的字段类型不符",
    "Setting on_error to quarantine skips the records that fail and keeps the "
    "rest. The run will finish, and the skipped records go to the rejection "
    "log — so the output is complete for every record that matched, and the "
    "ones that did not are accounted for rather than dropped silently.": (
        "将 on_error 设为 quarantine 可跳过失败的记录并保留其余记录。运行会正常结束，"
        "被跳过的记录进入隔离日志 — 因此输出对每条匹配的记录都是完整的，"
        "不匹配的记录也有据可查，而不是被悄悄丢弃。"
    ),
    "The finished file could not be put in place": "完成的文件无法就位",
    "On Windows this usually means the target is open in another program. "
    "Close it and run again. Nothing is lost: the complete output is in the "
    ".tmp file beside the target, and it can be renamed by hand.": (
        "在 Windows 上这通常意味着目标文件正被其他程序打开。关闭它后重新运行。"
        "没有丢失任何内容：完整输出位于目标旁边的 .tmp 文件中，可以手动改名。"
    ),
    "Resuming was refused": "续跑被拒绝",
    "The message below is the answer, and it is not summarised here. The tool "
    "names every component that differs with both values -- the size and "
    "sha256 it recorded beside the ones it found, or the two config hashes -- "
    "and putting that in different words would only lose the numbers. A "
    "checkpoint continues the run it recorded: if the source or the config has "
    "changed since, the parts already written belong to a different run, and "
    "continuing would mix two of them. The other refusal is that there is no "
    "checkpoint in that directory to resume from at all. Either way nothing was "
    "written, so nothing is lost by stopping to look.": (
        "下方消息就是答案，此处不作转述。工具会逐一列出有差异的组成部分及两侧数值 — "
        "它记录的大小与 sha256 对照实际读到的，或两份配置的哈希 — 换一种措辞只会丢掉数字。"
        "检查点只延续它记录的那次运行：若源文件或配置此后有变，已写出的部分属于另一次运行，"
        "继续会把两次混在一起。另一种拒绝是该目录中根本没有可续跑的检查点。"
        "无论哪种情况都没有写入任何内容，停下来查看不会有任何损失。"
    ),
    "A field path uses a namespace prefix the config does not declare": (
        "某个字段路径使用了配置未声明的前缀"
    ),
    "The namespace panel lists the prefixes in scope for the records the "
    "document has, which is what the paths are resolved against — so it is "
    "where the difference is visible: a prefix that is not there is one the "
    "records do not use, and either the path has a typo or the config was "
    "written for a different document. It only has that list once the document "
    "has been analysed; if it looks empty, analyse first.": (
        "命名空间面板列出文档中记录实际使用范围内的前缀，路径正是针对它们解析的 — "
        "差异就在那里可见：列表中没有的前缀就是这些记录不使用的，要么路径写错，"
        "要么配置是为另一个文档写的。该列表只有在分析文档之后才有内容；"
        "若它看起来是空的，请先分析。"
    ),
    "The run stopped before it read any records": "运行在读取任何记录之前就停止了",
    "This is usually the config rather than the document — a path that does not "
    "match, or a namespace prefix the config never declared. The field panel "
    "checks both as you edit, so a config that fails here is one that was changed "
    "after the fact.": (
        "这通常是配置而非文档的问题 — 路径不匹配，或配置从未声明某个命名空间前缀。"
        "字段面板会在编辑时即时检查这两者，所以在这里失败的配置往往是事后被改动过的。"
    ),
}

_TABLES: dict[str, dict[str, str]] = {"zh": ZH}

#: The language in effect. Module state rather than a parameter, because every call site
#: is "put this text on a widget while building the window", and threading a language
#: through every construction signature would make each panel's test wear the mechanism
#: instead of the panel. The window sets it once, from the stored settings, before it
#: builds anything.
_current: str = FALLBACK_LANGUAGE


def set_language(language: str) -> None:
    """Select the language the :func:`tr` table is read from.

    An unknown value falls back to English rather than raising: a settings file written by
    a newer build, or by hand, must not be able to stop the window opening -- the same
    reasoning that makes every stored setting coerce to its default.
    """
    global _current
    _current = language if language in _TABLES else FALLBACK_LANGUAGE


def current_language() -> str:
    """The language :func:`tr` is currently translating into."""
    return _current


def tr(text: str) -> str:
    """The display text for ``text`` in the current language.

    The fallback is the identity: no table for the language, or no entry for the string,
    and the English text comes back unchanged. That is deliberate -- an untranslated
    string is a reading inconvenience, while a blank label or a symbolic key is a broken
    interface, and the identity rule makes only the first possible.
    """
    return _TABLES.get(_current, {}).get(text, text)
