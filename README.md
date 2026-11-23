# pairwise-audio-loop-point

## 领域

游戏、图形与多媒体

## 主题

音频无缝循环点

## 说明

采样点精度、转码换算与跨边界预读。

初始状态下 `media/audio_loop_point.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
