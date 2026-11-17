# -*- coding: utf-8 -*-
"""LoginLimiter unit tests on a fake clock — no sleeps, deterministic."""
from app.login_guard import LoginLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make_limiter():
    clock = FakeClock()
    return LoginLimiter(max_fails=5, window_sec=900, clock=clock), clock


def test_blocks_only_after_max_failures():
    lim, _ = make_limiter()
    for _ in range(5):
        assert not lim.blocked("1.2.3.4")
        lim.record_failure("1.2.3.4")
    assert lim.blocked("1.2.3.4")


def test_window_slides_and_frees_the_address():
    lim, clock = make_limiter()
    for i in range(5):
        clock.now = float(i)  # failures at t=0..4
        lim.record_failure("1.2.3.4")
    assert lim.blocked("1.2.3.4")
    clock.now = 900.0  # only the t=0 failure is at the window edge → expired
    assert not lim.blocked("1.2.3.4")
    lim.record_failure("1.2.3.4")
    assert lim.blocked("1.2.3.4")  # 4 recent + 1 new


def test_addresses_are_independent():
    lim, _ = make_limiter()
    for _ in range(5):
        lim.record_failure("1.2.3.4")
    assert lim.blocked("1.2.3.4")
    assert not lim.blocked("5.6.7.8")
    lim.record_failure("5.6.7.8")
    assert not lim.blocked("5.6.7.8")


def test_reset_clears_the_address():
    lim, _ = make_limiter()
    for _ in range(4):
        lim.record_failure("1.2.3.4")
    lim.reset("1.2.3.4")
    for _ in range(4):
        lim.record_failure("1.2.3.4")
    assert not lim.blocked("1.2.3.4")  # 8 total, but only 4 since the reset


def test_retry_after_counts_down():
    lim, clock = make_limiter()
    assert lim.retry_after("1.2.3.4") == 0
    for _ in range(5):
        lim.record_failure("1.2.3.4")
    assert lim.retry_after("1.2.3.4") == 901
    clock.now = 800.0
    assert lim.retry_after("1.2.3.4") == 101
    clock.now = 901.0
    assert lim.retry_after("1.2.3.4") == 0


def test_expired_addresses_do_not_accumulate():
    lim, clock = make_limiter()
    for i in range(100):
        lim.record_failure(f"10.0.0.{i}")
    clock.now = 901.0
    for i in range(100):
        assert not lim.blocked(f"10.0.0.{i}")
    assert lim._fails == {}
