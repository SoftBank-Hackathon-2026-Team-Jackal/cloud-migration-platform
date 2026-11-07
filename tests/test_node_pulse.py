"""Agent가 터널 상태 보고에 얹는 노드 CPU·메모리(웹 위젯의 이전 서버 값)."""

from cmp_providers.node_pulse import NodeMetrics, cpu_percent, parse_cpu, parse_meminfo

STAT_A = "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 1 1 1 0 0 0 0 0 0\n"
STAT_B = "cpu  150 0 150 900 0 0 0 0 0 0\n"  # +100 busy of +200 total -> 50%
MEMINFO = "MemTotal:        8000000 kB\nMemFree: 100 kB\nMemAvailable:    6000000 kB\n"


def proc(tmp_path, stat, meminfo=MEMINFO):
    (tmp_path / "stat").write_text(stat)
    (tmp_path / "meminfo").write_text(meminfo)


def test_parsers_read_busy_time_and_memory_in_mb():
    assert parse_cpu(STAT_A) == (200, 1000)
    assert cpu_percent(parse_cpu(STAT_A), parse_cpu(STAT_B)) == 50.0
    assert parse_meminfo(MEMINFO) == (7812, 1953)  # total MB, used MB (MemAvailable based)
    assert parse_cpu("nope") is None and parse_meminfo("MemTotal: 0 kB") is None


def test_first_sample_has_no_cpu_so_nothing_is_reported_then_the_next_one_is(tmp_path):
    metrics = NodeMetrics(tmp_path)
    proc(tmp_path, STAT_A)
    assert metrics.sample() is None
    proc(tmp_path, STAT_B)
    assert metrics.sample() == {"cpu_percent": 50.0, "mem_used_mb": 1953, "mem_total_mb": 7812}


def test_unreadable_proc_is_none_not_an_error(tmp_path):
    assert NodeMetrics(tmp_path / "missing").sample() is None
