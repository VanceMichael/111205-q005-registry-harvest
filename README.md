# 登记网页混合编码与字段歧义隔离

## 现有能力

当前有字段别名合并和原始响应样本；没有编码选择器、流式接收器、区块提取、隔离重放流程。

`service.py` 保持已有规范化接口。根目录 `test_service.py` 为原有回归测试，`tests/test_baseline.py` 会同时加载原测试和离线演示验证。

## 环境和运行

使用 Python 3.11.10，仅依赖标准库，无需登录账号、安装第三方包或下载数据。`.python-version` 固定解释器版本；`pyproject.toml` 仅登记 unittest 入口，项目不需要打包安装。

在仓库根目录执行：

```sh
python3 -m unittest discover -s tests -v
python3 demo.py
```

完整演示命令：

```sh
python3 -m unittest discover -s tests -v && python3 demo.py
```

演示实时读取合成输入并运行当前接口，通常在 90 秒以内结束，不回放历史日志。状态写入临时目录，重复执行不污染仓库；本地 HTTP 示例只访问回环地址，不连接生产系统。

## 合成来源

`fixtures/responses.json` 用 Base64 保存原始响应。`chunks` 是累计字节边界，最后剩余字节组成末块，边界有意落在多字节字符内部。`fixtures/pages` 保留可读页面，`fixtures/rules.json` 为现有别名映射示例。

## 能力边界

当前自动化验证覆盖已有接口和离线响应器。业务编排、持久化的流程决策与跨模块恢复不属于当前实现范围，演示输出不代表这些扩展能力已经存在。
