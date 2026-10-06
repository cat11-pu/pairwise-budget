# pairwise-budget

只依赖 Python 标准库的滑动窗口预算分配内核：把整数刻度切成等宽窗口，
在每个窗口里把一份预算按权重分给若干申领方，支持超支借用、回收再分配、
优先级抢占与跨窗口结转。刻度与预算都由调用方以整数注入，内核不读真实
时钟、不起线程、不做任何 I/O，同一串调用永远得到同样的结果。

## 目录

- `budget/core.py`：窗口换算、加权分配、单窗口账本与跨窗口滚动内核。
- `tests/test_core.py`：验收用例。

## 运行测试

在项目根目录执行：

```
python3 -m unittest discover -s tests -v
```

Windows 上如果没有 `python3`，可用：

```
python -m unittest discover -s tests -v
```
