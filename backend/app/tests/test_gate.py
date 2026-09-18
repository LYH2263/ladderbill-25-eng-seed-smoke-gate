"""门禁检查测试。

- 离线用例：用桩函数替换 gate._get_json，断言各种异常下 run_gate 返回的
  缺项稳定名称准确（不依赖起服，可在任意环境重复执行）。
- live 用例（标记 ``live``）：docker compose 起服后对真实服务做端到端检查；
  服务未启动时自动 skip。运行方式见 README。
"""

import os
import socket
from urllib.parse import urlparse

import pytest

from app import gate


def _payload_router(payloads):
    """构造一个按 path 返回固定 JSON 的 _get_json 桩。"""

    def fake_get_json(base_url, path, timeout):
        if path not in payloads:
            raise AssertionError(f"未预期的请求路径: {path}")
        return payloads[path]

    return fake_get_json


GOOD_HEALTH = {"ok": True, "project": "ladderbill"}
GOOD_ACCOUNTS = {
    "items": [
        {"id": 1, "name": "张家", "meter_no": "M-1001", "note": "对照：正常用量"},
        {"id": 2, "name": "李家(种子偏高)", "meter_no": "M-1002", "note": "高用量+尖峰"},
    ]
}
GOOD_TIERS = {
    "items": [
        {"id": 1, "up_to": 180, "price": 0.52, "sort_order": 1},
        {"id": 2, "up_to": 260, "price": 0.62, "sort_order": 2},
        {"id": 3, "up_to": None, "price": 0.82, "sort_order": 3},
    ]
}


def _set_good(monkeypatch):
    monkeypatch.setattr(
        gate,
        "_get_json",
        _payload_router(
            {
                "/api/health": GOOD_HEALTH,
                "/api/accounts": GOOD_ACCOUNTS,
                "/api/tiers": GOOD_TIERS,
            }
        ),
    )


# ---------------------------------------------------------------- 离线用例


def test_gate_all_good(monkeypatch):
    _set_good(monkeypatch)
    missing, reasons = gate.run_gate()
    assert missing == []
    assert reasons == {}


def test_missing_dirty_account_reports_name(monkeypatch):
    """删掉 dirty 户：只报 account.dirty，不能笼统失败。"""
    payloads = {
        "/api/health": GOOD_HEALTH,
        "/api/accounts": {"items": [GOOD_ACCOUNTS["items"][0]]},  # 只剩 clean
        "/api/tiers": GOOD_TIERS,
    }
    monkeypatch.setattr(gate, "_get_json", _payload_router(payloads))
    missing, reasons = gate.run_gate()
    assert missing == ["account.dirty"]
    assert "account.dirty" in reasons


def test_missing_clean_account_reports_name(monkeypatch):
    payloads = {
        "/api/health": GOOD_HEALTH,
        "/api/accounts": {"items": [GOOD_ACCOUNTS["items"][1]]},  # 只剩 dirty
        "/api/tiers": GOOD_TIERS,
    }
    monkeypatch.setattr(gate, "_get_json", _payload_router(payloads))
    missing, _ = gate.run_gate()
    assert missing == ["account.clean"]


def test_health_ok_false_reports_name(monkeypatch):
    payloads = {
        "/api/health": {"ok": False, "project": "ladderbill"},
        "/api/accounts": GOOD_ACCOUNTS,
        "/api/tiers": GOOD_TIERS,
    }
    monkeypatch.setattr(gate, "_get_json", _payload_router(payloads))
    missing, _ = gate.run_gate()
    assert missing == ["health.ok"]


def test_health_project_mismatch_reports_name(monkeypatch):
    payloads = {
        "/api/health": {"ok": True, "project": "other"},
        "/api/accounts": GOOD_ACCOUNTS,
        "/api/tiers": GOOD_TIERS,
    }
    monkeypatch.setattr(gate, "_get_json", _payload_router(payloads))
    missing, _ = gate.run_gate()
    assert missing == ["health.project"]


def test_tier_count_not_three_reports_name(monkeypatch):
    payloads = {
        "/api/health": GOOD_HEALTH,
        "/api/accounts": GOOD_ACCOUNTS,
        "/api/tiers": {"items": GOOD_TIERS["items"][:2]},  # 少一档
    }
    monkeypatch.setattr(gate, "_get_json", _payload_router(payloads))
    missing, _ = gate.run_gate()
    assert missing == ["tier.count"]


def test_tier_up_to_not_monotonic_reports_name(monkeypatch):
    bad_tiers = {
        "items": [
            {"id": 1, "up_to": 260, "price": 0.52, "sort_order": 1},
            {"id": 2, "up_to": 180, "price": 0.62, "sort_order": 2},  # 回退
            {"id": 3, "up_to": None, "price": 0.82, "sort_order": 3},
        ]
    }
    payloads = {
        "/api/health": GOOD_HEALTH,
        "/api/accounts": GOOD_ACCOUNTS,
        "/api/tiers": bad_tiers,
    }
    monkeypatch.setattr(gate, "_get_json", _payload_router(payloads))
    missing, _ = gate.run_gate()
    assert missing == ["tier.up_to_order"]


def test_service_down_reports_all_live_checks(monkeypatch):
    """停服务：所有依赖在线接口的检查项都要以稳定名称报出。"""

    def boom(base_url, path, timeout):
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(gate, "_get_json", boom)
    missing, reasons = gate.run_gate()
    assert missing == [
        "health.ok",
        "health.project",
        "account.dirty",
        "account.clean",
        "tier.count",
        "tier.up_to_order",
    ]
    assert reasons  # 每项都带有原因，不是笼统失败


def test_cli_nonzero_exit_and_stderr_names(monkeypatch, capsys):
    payloads = {
        "/api/health": GOOD_HEALTH,
        "/api/accounts": {"items": [GOOD_ACCOUNTS["items"][0]]},
        "/api/tiers": GOOD_TIERS,
    }
    monkeypatch.setattr(gate, "_get_json", _payload_router(payloads))
    rc = gate.main(["--base-url", "http://unused"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "account.dirty" in err


def test_malformed_health_body_reports_names(monkeypatch):
    payloads = {
        "/api/health": ["not", "an", "object"],
        "/api/accounts": GOOD_ACCOUNTS,
        "/api/tiers": GOOD_TIERS,
    }
    monkeypatch.setattr(gate, "_get_json", _payload_router(payloads))
    missing, _ = gate.run_gate()
    assert missing == ["health.ok", "health.project"]


def test_malformed_items_body_reports_names(monkeypatch):
    payloads = {
        "/api/health": GOOD_HEALTH,
        "/api/accounts": {"items": None},
        "/api/tiers": {"items": "oops"},
    }
    monkeypatch.setattr(gate, "_get_json", _payload_router(payloads))
    missing, _ = gate.run_gate()
    assert missing == [
        "account.dirty",
        "account.clean",
        "tier.count",
        "tier.up_to_order",
    ]


def test_cli_success_zero_exit(monkeypatch, capsys):
    _set_good(monkeypatch)
    rc = gate.main(["--base-url", "http://unused"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[]" in out


# ---------------------------------------------------------------- live 用例


def _service_reachable(base_url: str) -> bool:
    parsed = urlparse(base_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


@pytest.mark.live
def test_gate_live_service():
    """docker compose up 后对真实服务执行门禁：缺项必须为空。"""
    base_url = os.environ.get("GATE_BASE_URL", gate.DEFAULT_BASE_URL)
    if not _service_reachable(base_url):
        pytest.skip(f"服务未启动（{base_url}），先执行 docker compose up --build")
    missing, reasons = gate.run_gate(base_url)
    assert missing == [], f"门禁缺项: {missing}; 原因: {reasons}"
