from cfa635.server.pages import Arbiter, Page, PageStore


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def tick(self, secs):
        self.now += secs


def make(clock, **kw):
    store = PageStore()
    arb = Arbiter(store, rotation_secs=10, nav_hold_secs=30, clock=clock, **kw)
    return store, arb


def page(pid, clock, **kw):
    return Page(id=pid, name=pid, lines=[pid],
                created_at=clock.now, updated_at=clock.now, **kw)


def test_empty_store_is_idle():
    clock = Clock()
    _, arb = make(clock)
    assert arb.select() is None


def test_single_page_stays_up():
    clock = Clock()
    store, arb = make(clock)
    store.put(page("a", clock))
    assert arb.select() == "a"
    clock.tick(60)
    assert arb.select() == "a"


def test_rotation_in_created_order():
    clock = Clock()
    store, arb = make(clock)
    store.put(page("a", clock))
    clock.tick(1)
    store.put(page("b", clock))
    assert arb.select() == "a"
    clock.tick(10)
    assert arb.select() == "b"
    clock.tick(10)
    assert arb.select() == "a"
    clock.tick(5)
    assert arb.select() == "a"  # mid-interval: no change


def test_per_page_duration_overrides_rotation_dwell():
    clock = Clock()
    store, arb = make(clock)
    store.put(page("quick", clock, duration=2))
    clock.tick(1)
    store.put(page("slow", clock, duration=25))
    assert arb.select() == "quick"
    clock.tick(2)  # quick's own dwell elapsed
    assert arb.select() == "slow"
    clock.tick(10)  # global default would have rotated; slow holds on
    assert arb.select() == "slow"
    clock.tick(15)
    assert arb.select() == "quick"


def test_higher_priority_wins_rotation_group():
    clock = Clock()
    store, arb = make(clock)
    store.put(page("low", clock, priority=10))
    store.put(page("high", clock, priority=60))
    for _ in range(5):
        assert arb.select() == "high"
        clock.tick(10)


def test_alert_preempts_and_clears_pin():
    clock = Clock()
    store, arb = make(clock)
    store.put(page("normal", clock))
    assert arb.select() == "normal"
    arb.pin("normal")
    store.put(page("alarm", clock, priority=200))
    assert arb.select() == "alarm"
    assert arb.pinned is None
    store.delete("alarm")
    assert arb.select() == "normal"


def test_pin_holds_then_expires():
    clock = Clock()
    store, arb = make(clock)
    store.put(page("a", clock))
    clock.tick(1)
    store.put(page("b", clock))
    assert arb.select() == "a"
    arb.pin("b")
    assert arb.select() == "b"
    clock.tick(29)
    assert arb.select() == "b"  # still pinned
    clock.tick(2)  # pin expired (30 s hold)
    # back to automatic rotation among the top group
    assert arb.select() in ("a", "b")
    assert arb.pinned is None


def test_pin_missing_page_rejected():
    clock = Clock()
    _, arb = make(clock)
    assert arb.pin("ghost") is False


def test_nav_cycles_all_pages_and_pins():
    clock = Clock()
    store, arb = make(clock)
    store.put(page("a", clock, priority=50))
    clock.tick(1)
    store.put(page("b", clock, priority=10))  # lower priority: nav still reaches it
    assert arb.select() == "a"
    assert arb.nav(1) == "b"
    assert arb.select() == "b"
    assert arb.nav(1) == "a"
    assert arb.nav(-1) == "b"


def test_ttl_sweep():
    clock = Clock()
    store, arb = make(clock)
    store.put(page("mortal", clock, ttl=15))
    store.put(page("eternal", clock))
    assert arb.select() in ("mortal", "eternal")
    clock.tick(16)
    dead = store.sweep(clock.now)
    assert [p.id for p in dead] == ["mortal"]
    assert arb.select() == "eternal"


def test_ttl_refreshed_by_update():
    clock = Clock()
    store, _ = make(clock)
    p = page("hb", clock, ttl=15)
    store.put(p)
    clock.tick(10)
    p.updated_at = clock.now  # heartbeat
    clock.tick(10)
    assert store.sweep(clock.now) == []  # 10 s since refresh < 15 s ttl
    clock.tick(6)
    assert [d.id for d in store.sweep(clock.now)] == ["hb"]


def test_pinned_page_expiry_falls_back():
    clock = Clock()
    store, arb = make(clock)
    store.put(page("a", clock))
    store.put(page("brief", clock, ttl=5))
    arb.pin("brief")
    assert arb.select() == "brief"
    clock.tick(6)
    store.sweep(clock.now)
    assert arb.select() == "a"
