"""种子与健康门禁（可重复执行的检查入口）。

仅通过公开 HTTP 接口做只读检查，不修改任何种子数据：

- health.ok           GET /api/health  返回 {"ok": true, "project": "ladderbill"}
- health.project      同上，project 字段必须等于 ladderbill
- account.dirty       GET /api/accounts 中至少一户名字含 "种子"
- account.clean       GET /api/accounts 中至少一户名字不含 "种子"
- tier.count          GET /api/tiers 按 sort_order 恰好三档
- tier.up_to_order    三档 up_to 严格单调递增，且末档开放（up_to 为空）

任一检查不通过 -> run_gate() 返回该检查的稳定名称（stable_id），
``python -m app.gate`` 时以退出码 1 退出，并把缺项名称逐行打到 stderr。

用法（docker compose 起服后，在 backend 目录下）::

    python -m app.gate                       # 检查默认地址 http://localhost:9100
    python -m app.gate --base-url http://localhost:9100
    GATE_BASE_URL=http://host:port python -m app.gate
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

DEFAULT_BASE_URL = "http://localhost:9100"
PROJECT_NAME = "ladderbill"
DIRTY_MARKER = "种子"

# 检查项的稳定名称（失败时打印的就是这些标识，不打印笼统失败）
HEALTH_OK = "health.ok"
HEALTH_PROJECT = "health.project"
ACCOUNT_DIRTY = "account.dirty"
ACCOUNT_CLEAN = "account.clean"
TIER_COUNT = "tier.count"
TIER_UP_TO_ORDER = "tier.up_to_order"

ALL_CHECKS = [
    HEALTH_OK,
    HEALTH_PROJECT,
    ACCOUNT_DIRTY,
    ACCOUNT_CLEAN,
    TIER_COUNT,
    TIER_UP_TO_ORDER,
]


def _get_json(base_url: str, path: str, timeout: float) -> dict:
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return json.loads(resp.read().decode(charset))


def _items(payload: dict) -> list[dict] | None:
    """从 {"items": [...]} 取列表；形状不对时返回 None（交由调用方报缺项）。"""
    items = payload.get("items", [])
    return items if isinstance(items, list) else None


def run_gate(base_url: str = DEFAULT_BASE_URL, timeout: float = 5.0) -> tuple[list[str], dict[str, str]]:
    """执行全部门禁检查。

    返回 ``(missing, reasons)``：
    - ``missing`` 为未通过检查项的稳定名称列表，顺序固定；为空即全部通过。
    - ``reasons`` 为每项失败的具体原因，仅用于 stderr/诊断，不替代缺项名称。
    """
    missing: list[str] = []
    reasons: dict[str, str] = {}

    # ---- /api/health ---------------------------------------------------
    health: dict | None = None
    try:
        payload = _get_json(base_url, "/api/health", timeout)
        health = payload if isinstance(payload, dict) else None
    except Exception as exc:  # 服务停掉 / 连接被拒 / 超时 / 非 JSON
        reason = f"GET /api/health 失败: {type(exc).__name__}: {exc}"
        missing.extend([HEALTH_OK, HEALTH_PROJECT])
        reasons[HEALTH_OK] = reason
        reasons[HEALTH_PROJECT] = reason

    if health is None and HEALTH_OK not in reasons:
        reason = "GET /api/health 响应不是 JSON 对象"
        missing.extend([HEALTH_OK, HEALTH_PROJECT])
        reasons[HEALTH_OK] = reason
        reasons[HEALTH_PROJECT] = reason

    if health is not None:
        if health.get("ok") is not True:
            missing.append(HEALTH_OK)
            reasons[HEALTH_OK] = f"ok 字段不是 true，实际值={health.get('ok')!r}"
        if health.get("project") != PROJECT_NAME:
            missing.append(HEALTH_PROJECT)
            reasons[HEALTH_PROJECT] = f"project 字段必须为 {PROJECT_NAME!r}，实际值={health.get('project')!r}"

    # ---- /api/accounts -------------------------------------------------
    accounts: list[dict] | None = None
    try:
        accounts = _items(_get_json(base_url, "/api/accounts", timeout))
    except Exception as exc:
        reason = f"GET /api/accounts 失败: {type(exc).__name__}: {exc}"
        missing.extend([ACCOUNT_DIRTY, ACCOUNT_CLEAN])
        reasons[ACCOUNT_DIRTY] = reason
        reasons[ACCOUNT_CLEAN] = reason
    if accounts is None and ACCOUNT_DIRTY not in reasons:
        reason = "GET /api/accounts 响应缺少 items 列表"
        missing.extend([ACCOUNT_DIRTY, ACCOUNT_CLEAN])
        reasons[ACCOUNT_DIRTY] = reason
        reasons[ACCOUNT_CLEAN] = reason

    if accounts is not None:
        has_dirty = any(DIRTY_MARKER in str(a.get("name", "")) for a in accounts)
        has_clean = any(DIRTY_MARKER not in str(a.get("name", "")) for a in accounts)
        if not has_dirty:
            missing.append(ACCOUNT_DIRTY)
            reasons[ACCOUNT_DIRTY] = f"至少需要一户名字含 {DIRTY_MARKER!r} 的 dirty 种子户（共 {len(accounts)} 户）"
        if not has_clean:
            missing.append(ACCOUNT_CLEAN)
            reasons[ACCOUNT_CLEAN] = f"至少需要一户名字不含 {DIRTY_MARKER!r} 的 clean 对照户（共 {len(accounts)} 户）"

    # ---- /api/tiers ----------------------------------------------------
    tiers: list[dict] | None = None
    try:
        tiers = _items(_get_json(base_url, "/api/tiers", timeout))
    except Exception as exc:
        reason = f"GET /api/tiers 失败: {type(exc).__name__}: {exc}"
        missing.extend([TIER_COUNT, TIER_UP_TO_ORDER])
        reasons[TIER_COUNT] = reason
        reasons[TIER_UP_TO_ORDER] = reason
    if tiers is None and TIER_COUNT not in reasons:
        reason = "GET /api/tiers 响应缺少 items 列表"
        missing.extend([TIER_COUNT, TIER_UP_TO_ORDER])
        reasons[TIER_COUNT] = reason
        reasons[TIER_UP_TO_ORDER] = reason

    if tiers is not None:
        # 数量：必须恰好三档（多了/少了都算缺项，不允许删断言项假绿）
        if len(tiers) != 3:
            missing.append(TIER_COUNT)
            reasons[TIER_COUNT] = f"默认阶梯必须为三档，实际 {len(tiers)} 档: {[t.get('up_to') for t in tiers]}"
        else:
            # 末档必须开放（up_to 为空），前两档 up_to 严格单调递增
            up_to_values = [t.get("up_to") for t in tiers]
            ordered_bounds = up_to_values[:-1]
            monotonic = (
                len(ordered_bounds) == 2
                and ordered_bounds[0] is not None
                and ordered_bounds[1] is not None
                and ordered_bounds[0] < ordered_bounds[1]
            )
            last_open = up_to_values[-1] is None
            if not (monotonic and last_open):
                missing.append(TIER_UP_TO_ORDER)
                reasons[TIER_UP_TO_ORDER] = (
                    f"up_to 必须严格单调递增且末档开放，实际 up_to={up_to_values}"
                )

    # 按 ALL_CHECKS 的固定顺序去重输出，避免异常分支造成顺序/重复问题
    seen: set[str] = set()
    ordered_missing = [name for name in ALL_CHECKS if name in missing and not (name in seen or seen.add(name))]
    return ordered_missing, reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ladderbill 种子与健康门禁")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("GATE_BASE_URL", DEFAULT_BASE_URL),
        help="服务地址（默认 %(default)s，也可用环境变量 GATE_BASE_URL）",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="单次请求超时秒数")
    args = parser.parse_args(argv)

    missing, reasons = run_gate(args.base_url, timeout=args.timeout)

    if missing:
        # 缺项名称打到 stderr（逐行，稳定标识），随后附上具体原因
        print("门禁失败，缺项:", file=sys.stderr)
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
        for name in missing:
            if name in reasons:
                print(f"    {name}: {reasons[name]}", file=sys.stderr)
        return 1

    print("门禁通过，缺项列表: []")
    return 0


if __name__ == "__main__":
    sys.exit(main())
