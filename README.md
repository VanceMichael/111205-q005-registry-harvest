# 登记网页混合编码与字段歧义隔离

## 现有能力

`service.map_record` 保持原有接口。采集入口在 `harvester/` 中，覆盖原始响应到结构化档案的完整路径：

| 模块 | 职责 |
| --- | --- |
| `harvester/streaming.py` | 流式严格解码、编码依据选择、冲突记录、原始字节偏移 |
| `harvester/sections.py` | 按页面声明的区块身份抽取带位置的字段 |
| `harvester/rules.py` | 按 `(来源, 区块种类)` 选择字段、别名、日期格式与时区 |
| `harvester/pipeline.py` | `Collector`：入库、隔离、人工确认后的定向重放 |

根目录 `test_service.py` 是原有回归测试，`tests/test_baseline.py` 同时加载它和离线演示；`tests/test_ingest.py` 是端到端验证。

## 行为和约束

**解码**

- 原字节从不改写；每个分块的 `(起始, 结束)` 字节范围、进入/离开时暂存的多字节字节数都记录在 `chunk_carry_over`。
- 分块边界截断多字节字符是正常的：增量解码器保留暂存字节，只有流结束后仍不完整才判为 `truncated-multibyte`。
- 依据优先级为 BOM > 响应头 > 页面声明 > 来源配置；`manual-confirm` 由人工确认，优先级最高。冲突不会隐藏，而是写入 `conflicts`。
- 全程 `errors='strict'`，不产生替换字符。无法无损解码的样本整体进入隔离区，附带原始 Base64 与失败字节的绝对偏移。
- 多个候选编码都能无损解码但结果不同（且没有权威依据）时，标记为 `ambiguous` 并隔离，不猜测。
- `iso-8859-1`/`latin1` 之类的万能解码标签不作为依据；无 BOM 的裸 `utf-16` 标签也不作为依据。

**解析**

- 字段必须归属到带 `data-section`/`data-block`/`data-scope` 身份的区块；区块外的标签只记 `unsectioned-label`，绝不并入相邻区块。
- 同一标签出现在不同区块时，各区块按自己的规则解析（例如 b 的 `current` 用 `%d/%m/%Y`，`history` 用 `%Y-%m-%d`）。
- 一个区块内两个标签落到同一规范字段且值不同（或同一标签重复出现）时，进入隔离区，不取第一个值。
- 只有人工确认的 `occurrence` 规则才能指定重复标签中的第几个。
- 日期格式和时区由来源规则声明，无法确定时进入 `unresolved-date` 隔离，不猜测。只有日期没有时刻的格式按声明的日历日保留。

**入库与重放**

- 每条记录带：来源标识、原始响应 SHA-256 与编码依据、区块身份与属性、字符区间和原始字节区间、每个字段命中的规则与解释。
- 档案键是 `(来源, 来源内标识)`，因此不同来源的相同企业编号不会合并，编号相同的两次出现会各自留下记录。
- 人工确认编码或区块规则后，`Collector.replay()` 只重放点名样本；其余隔离样本原样保留，其他来源的档案对象不被替换。同一确认重复重放时 `skipped_duplicates` 记录键名，不新增档案。

## 环境和运行

使用 Python 3.11.10，仅依赖标准库。`.python-version` 固定解释器版本；`pyproject.toml` 仅登记 unittest 入口。

在仓库根目录执行：

```sh
python3 -m unittest discover -s tests -v
python3 demo.py
```

完整演示命令：

```sh
python3 -m unittest discover -s tests -v && python3 demo.py
```

演示实时读取合成输入并运行当前接口，通常在 90 秒以内结束，不回放历史日志。状态只存在于进程内，重复执行不污染仓库；本地示例只使用仓库内夹具，不连接网络。

## 合成来源

`fixtures/responses.json` 以 Base64 保存原始响应。`chunks` 是累计字节边界，最后剩余字节组成末块，边界有意落在多字节字符内部。`fixtures/pages/a.html`、`fixtures/pages/b.html` 是原有别名映射示例；`fixtures/pages/samples/` 下每个响应样本都有一份可读页面（`sample-a.html` 以 GB18030 保存，`sample-f.html` 用文字标注无法解码的 `0xff`）。`fixtures/source_rules.json` 是按来源的字段、别名、日期格式与时区规则；`fixtures/rules.json` 保留原有别名映射示例。

样本覆盖：GB18030 正文 + UTF-8 响应头（a）、同一档案两个区块（b）、跨来源相同编号（c）、区块内重复标签冲突（d）、两种编码均可无损但结果不同（e）、存在无法解释的字节（f）。

## 能力边界

当前实现只覆盖仓库内离线样本和标准库能提供的解码器；不包含网络抓取、业务编排、持久化存储或跨进程恢复。演示输出不代表这些扩展能力已经存在。
