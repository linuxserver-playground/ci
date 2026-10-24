from unittest.mock import MagicMock

from ci.probe import TCP_LISTEN, UDP_UNCONNECTED, parse_proc_net, parse_rss, probe

TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:231D 00000000:0000 0A 00000000:00000000 00:00000000 00000000   911        0 12345 1 0000000000000000 100 0 0 10 0
   1: 0100007F:0CEA 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12346 1 0000000000000000 100 0 0 10 0
   2: AC110002:231D AC110001:D43A 01 00000000:00000000 00:00000000 00000000   911        0 12347 1 0000000000000000 20 4 30 10 -1
  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000000000000:231D 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000   911        0 12348 1 0000000000000000 100 0 0 10 0
"""
UDP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops
  100: 00000000:0035 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 1 2 0000000000000000 0
"""
STATUS = "VmRSS:\t    1024 kB\nVmRSS:\t     512 kB\nVmSize:\t 99999 kB\n"


def test_parse_proc_net() -> None:
    assert parse_proc_net(TCP, TCP_LISTEN) == {8989, 3306}
    assert parse_proc_net(UDP, UDP_UNCONNECTED) == {53}
    assert parse_proc_net("", TCP_LISTEN) == set()


def test_parse_rss() -> None:
    assert parse_rss(STATUS) == 1536 * 1024
    assert parse_rss("") == 0


def test_probe_assembles_runtime_block() -> None:
    container = MagicMock()
    outputs = [STATUS, "11\n", TCP, UDP, "svc-cron\nsvc-sonarr\n"]
    container.exec_run.side_effect = [MagicMock(exit_code=0, output=o.encode()) for o in outputs]
    container.diff.return_value = [{"Path": "/config", "Kind": 1}] * 189
    runtime = probe(container)
    assert runtime == {
        "rss_bytes": 1536 * 1024,
        "pids": 11,
        "listening_tcp": [3306, 8989],
        "listening_udp": [53],
        "s6_services": ["svc-cron", "svc-sonarr"],
        "changed_paths": 189,
    }
