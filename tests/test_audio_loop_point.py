import pytest
import media.audio_loop_point as mod

CASES = [
    ("loop_sample_exact", (1,)),
    ("resample_scale", (1,)),
    ("fade_ratio", (1,)),
    ("preread_cross_loop", (1,)),
    ("channel_shared", (1,)),
    ("determinism", (1,)),
    ("phase_align", (1,)),
    ("validation", (1,)),
    ("audit", (1,)),
    ("fallback", (1,)),
    ("loop_meta", (1,)),
    ("metrics", (1,)),
    ("loop_play", (1,)),
    ("seam", (1,)),
    ("tail", (1,)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
