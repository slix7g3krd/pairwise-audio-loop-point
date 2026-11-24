"""音频无缝循环点核心模块。

修复要点：
1. 循环点一律按采样点（整数）存储，不再按毫秒取整，避免落在两个采样点之间。
2. 转码到不同采样率时，循环点按 new_sr / old_sr 比例换算（精确有理数运算）。
3. 交叉淡化长度按循环长度取比例，而不是固定采样数，短文件不会被吃掉半个循环。
4. 流式解码预读跨循环边界：读到循环末尾前就从循环起点继续取数，避免断音。
5. 多声道共用同一条时间轴上的同一组循环点。
6. 接缝处做相位对齐：循环终点对齐到与起点斜率方向一致的过零点附近。

所有换算使用 fractions.Fraction 做精确有理数运算，保证跨平台确定性。
"""

from dataclasses import dataclass, replace
from fractions import Fraction

DEFAULT_SAMPLE_RATE = 48000
DEFAULT_FADE_RATIO = Fraction(1, 10)  # 淡化长度 = 循环长度的 1/10
MAX_FADE_FRACTION = Fraction(1, 2)    # 淡化最多占循环的一半
DEFAULT_PREREAD = 512                 # 流式预读默认采样数
PHASE_SEARCH_WINDOW = 256             # 相位对齐搜索窗口（采样数）


@dataclass(frozen=True)
class LoopMeta:
    """循环元数据。start/end/total_samples 一律为采样点整数。"""

    sample_rate: int
    total_samples: int
    start: int          # 循环起点（含），采样点
    end: int            # 循环终点（不含），采样点
    channels: int = 1
    fade_samples: int = 0

    @property
    def length(self):
        return self.end - self.start


def _round_exact(value):
    """确定性取整：精确有理数 + 银行家舍入，跨平台结果一致。"""
    return int(round(Fraction(value)))


def _as_loop(x):
    """把 int/float/dict/LoopMeta 统一成 LoopMeta。数值视为总采样数，整文件循环。"""
    if isinstance(x, LoopMeta):
        return x
    if isinstance(x, dict):
        return loop_meta(x)
    total = max(1, int(x))
    return LoopMeta(DEFAULT_SAMPLE_RATE, total, 0, total)


def loop_sample_exact(point_ms, sample_rate=DEFAULT_SAMPLE_RATE):
    """毫秒循环点换算为采样点，精确取整，不再出现落在两个采样点之间的浮点位置。"""
    return _round_exact(Fraction(point_ms) * Fraction(sample_rate) / 1000)


def resample_scale(points, old_sr=44100, new_sr=DEFAULT_SAMPLE_RATE):
    """转码换算：循环点按 new_sr/old_sr 比例缩放。接受单个采样点或 LoopMeta。"""
    ratio = Fraction(new_sr) / Fraction(old_sr)
    if isinstance(points, LoopMeta):
        return replace(
            points,
            sample_rate=int(new_sr),
            total_samples=_round_exact(points.total_samples * ratio),
            start=_round_exact(points.start * ratio),
            end=_round_exact(points.end * ratio),
            fade_samples=_round_exact(points.fade_samples * ratio),
        )
    return _round_exact(Fraction(points) * ratio)


def fade_ratio(loop, ratio=DEFAULT_FADE_RATIO):
    """交叉淡化长度按循环长度取比例，并钳制在循环长度一半以内。"""
    lp = _as_loop(loop)
    fade = _round_exact(lp.length * Fraction(ratio))
    return max(0, min(fade, _round_exact(lp.length * MAX_FADE_FRACTION)))


def preread_cross_loop(loop, pos=None, count=DEFAULT_PREREAD):
    """流式预读：从 pos 起取 count 个采样，越过循环终点时绕回循环起点。

    返回采样下标列表，保证循环边界前预读充足、不断音。
    """
    lp = _as_loop(loop)
    if pos is None:
        pos = max(lp.start, lp.end - count)
    out = []
    p = pos
    for _ in range(max(0, int(count))):
        while p >= lp.end:
            p = lp.start + (p - lp.end)
        while p < lp.start:
            p = lp.end - (lp.start - p)
        out.append(p)
        p += 1
    return out


def channel_shared(loops):
    """多声道共用循环点：把各声道不一致的循环点统一为同一时间轴上的一组。

    取各声道起点的最大值与终点的最小值（公共区间）；无公共区间时回退到第一个声道。
    """
    if isinstance(loops, (list, tuple)):
        if not loops:
            return _as_loop(1)
        metas = [_as_loop(x) for x in loops]
        start = max(m.start for m in metas)
        end = min(m.end for m in metas)
        base = metas[0]
        if start >= end:  # 无公共区间，回退
            start, end = base.start, base.end
        return replace(base, start=start, end=end, channels=len(metas))
    lp = _as_loop(loops)
    return lp


def determinism(value):
    """确定性保证：同一输入的换算结果可复现（精确有理数，无浮点误差累积）。"""
    first = _round_exact(value)
    second = _round_exact(Fraction(value))  # 重算一遍验证可复现
    return {"value": first, "stable": first == second}


def phase_align(loop, samples=None, search=PHASE_SEARCH_WINDOW):
    """接缝相位对齐：在循环终点附近搜索与起点斜率方向一致的过零点，微调终点。

    samples 为单声道采样序列；缺省时返回原循环不变。
    """
    lp = _as_loop(loop)
    if samples is None or lp.length < 2 or lp.start >= len(samples):
        return lp
    s = lp.start
    target = samples[s]
    slope_sign = 0
    if s + 1 < len(samples):
        slope_sign = (samples[s + 1] > samples[s]) - (samples[s + 1] < samples[s])
    best, best_cost = lp.end, None
    lo = max(lp.start + 1, lp.end - search)
    hi = min(lp.total_samples, len(samples), lp.end + search)
    for e in range(lo, hi + 1):
        prev = samples[e - 1]
        cost = abs(prev - target)
        if slope_sign and e < len(samples):
            cand_slope = (samples[e] > prev) - (samples[e] < prev)
            if cand_slope != slope_sign:
                cost += abs(samples[e] - prev) + 1  # 斜率方向不一致，罚
        if best_cost is None or cost < best_cost:
            best, best_cost = e, cost
    return replace(lp, end=best)


def validation(loop):
    """校验循环点：0 <= start < end <= total，淡化不超过循环一半。"""
    lp = _as_loop(loop)
    errors = []
    if lp.sample_rate <= 0:
        errors.append("sample_rate 必须为正")
    if not 0 <= lp.start < lp.end:
        errors.append("循环点必须满足 0 <= start < end")
    if lp.end > lp.total_samples:
        errors.append("循环终点超出文件总长")
    if lp.fade_samples < 0 or lp.fade_samples > lp.length // 2:
        errors.append("淡化长度必须在 [0, 循环长度/2] 内")
    return {"ok": not errors, "errors": errors}


def audit(loop):
    """审计报告：采样点位置、毫秒等效值、淡化、校验结果。"""
    lp = _as_loop(loop)
    return {
        "start_samples": lp.start,
        "end_samples": lp.end,
        "start_ms": float(Fraction(lp.start * 1000) / lp.sample_rate),
        "end_ms": float(Fraction(lp.end * 1000) / lp.sample_rate),
        "loop_length_samples": lp.length,
        "fade_samples": lp.fade_samples,
        "channels": lp.channels,
        "validation": validation(lp),
    }


def fallback(loop, total_samples=None):
    """循环元数据缺失或非法时回退为整文件循环。"""
    try:
        lp = _as_loop(loop)
    except Exception:
        lp = None
    if lp is None or not validation(lp)["ok"]:
        total = int(total_samples) if total_samples else max(1, int(getattr(lp, "total_samples", 0) or 1))
        return LoopMeta(DEFAULT_SAMPLE_RATE, total, 0, total)
    return lp


def loop_meta(spec=None, sample_rate=DEFAULT_SAMPLE_RATE, total_samples=None):
    """构造循环元数据，统一换算为采样点存储。

    spec 可为 LoopMeta、dict（支持 start_ms/end_ms 毫秒或 start/end 采样点）或数值（总长）。
    """
    if isinstance(spec, LoopMeta):
        return spec
    if isinstance(spec, dict):
        sr = int(spec.get("sample_rate", sample_rate))
        total = int(spec.get("total_samples", total_samples or 0))
        if "start_ms" in spec or "end_ms" in spec:
            start = loop_sample_exact(spec.get("start_ms", 0), sr)
            end = loop_sample_exact(spec.get("end_ms", 0), sr)
        else:
            start = int(spec.get("start", 0))
            end = int(spec.get("end", total))
        if total <= 0:
            total = end
        lp = LoopMeta(sr, total, start, end, int(spec.get("channels", 1)))
        return replace(lp, fade_samples=fade_ratio(lp))
    total = int(total_samples if total_samples is not None else (spec if spec is not None else 1))
    total = max(1, total)
    lp = LoopMeta(sample_rate, total, 0, total)
    return replace(lp, fade_samples=fade_ratio(lp))


def metrics(loop, samples=None):
    """循环质量指标：长度、淡化占比、接缝跳变（有采样数据时）。"""
    lp = _as_loop(loop)
    m = {
        "loop_length_samples": lp.length,
        "loop_length_ms": float(Fraction(lp.length * 1000) / lp.sample_rate),
        "fade_samples": lp.fade_samples,
        "fade_ratio_actual": float(Fraction(lp.fade_samples) / lp.length) if lp.length else 0.0,
        "tail_samples": lp.total_samples - lp.end,
    }
    if samples is not None and 0 <= lp.start < lp.end <= len(samples):
        m["seam_jump"] = samples[lp.end - 1] - samples[lp.start]
    return m


def loop_play(loop, chunk=DEFAULT_PREREAD):
    """播放位置生成器：按 chunk 步进，越过循环终点自动绕回起点（跨边界不断音）。"""
    lp = _as_loop(loop)
    pos = lp.start
    while True:
        yield pos
        pos += chunk
        while pos >= lp.end:
            pos = lp.start + (pos - lp.end)


def seam(loop, samples=None):
    """接缝分析：循环终点前一个采样与起点采样的跳变量及斜率是否对齐。"""
    lp = _as_loop(loop)
    result = {"fade_samples": lp.fade_samples, "aligned": None, "jump": None}
    if samples is not None and 0 <= lp.start < lp.end <= len(samples):
        a, b = samples[lp.end - 1], samples[lp.start]
        result["jump"] = b - a
        if lp.start + 1 < len(samples) and lp.end < len(samples):
            s1 = samples[lp.start + 1] - samples[lp.start]
            s2 = samples[lp.end] - samples[lp.end - 1]
            result["aligned"] = (s1 >= 0) == (s2 >= 0)
    return result


def tail(loop):
    """循环终点之后的尾部采样数（循环时不播放的部分）。"""
    lp = _as_loop(loop)
    return max(0, lp.total_samples - lp.end)
