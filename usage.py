"""docbridge · 用量统计

按天 + 按 IP 记录任务数、字符数与接口调用次数，落盘 usage.json。
与 同域项目 的 usage.json 分开（同域不同服务，各自算各自的账），
管理员用 ADMIN_TOKEN 在 /api/usage 查看。

成本只是**粗估**：字符 → token 用经验系数，单价可用环境变量覆盖。
"""

from __future__ import annotations

import json
import os
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
USAGE_FILE = os.environ.get("DOCBRIDGE_USAGE_FILE") or os.path.join(ROOT, "usage.json")

# 粗估单价（元 / 百万 token），可用环境变量覆盖
PRICE_IN = float(os.environ.get("TR_PRICE_IN", "1.0"))
PRICE_OUT = float(os.environ.get("TR_PRICE_OUT", "2.0"))

_EMPTY = {"jobs": 0, "charsIn": 0, "charsOut": 0, "calls": 0,
          "pages": 0, "filesIn": 0, "failed": 0,
          "tokensIn": 0, "tokensOut": 0}   # 接口回的真实用量，优先用它算钱


def _new_day() -> dict:
    return {"day": time.strftime("%Y-%m-%d"), "total": dict(_EMPTY), "perIp": {}}


class Usage:
    def __init__(self, path: str = USAGE_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._dirty = False
        self.data = self._load()
        t = threading.Thread(target=self._flush_loop, daemon=True)
        t.start()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "total" in data:
                for key, val in _EMPTY.items():
                    data["total"].setdefault(key, val)
                return data
        except (OSError, ValueError):
            pass
        return _new_day()

    def _roll(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self.data.get("day") != today:
            self.data = _new_day()

    def record(self, ip: str, **fields) -> None:
        with self._lock:
            self._roll()
            bucket = self.data["perIp"].setdefault(ip or "-", dict(_EMPTY))
            for key, val in fields.items():
                if key in _EMPTY and val:
                    self.data["total"][key] = self.data["total"].get(key, 0) + val
                    bucket[key] = bucket.get(key, 0) + val
            self._dirty = True

    def snapshot(self) -> dict:
        with self._lock:
            self._roll()
            total = dict(self.data["total"])
            return {
                "day": self.data["day"],
                "total": total,
                "cost": self._cost(total),
                "ips": len(self.data["perIp"]),
                "perIp": self.data["perIp"],
            }

    @staticmethod
    def _cost(total: dict) -> dict:
        """成本估算：有真实 token 就用真实值，没有才退回按字符折算。"""
        real = bool(total.get("tokensIn") or total.get("tokensOut"))
        tokens_in = int(total.get("tokensIn") or total.get("charsIn", 0) / 4.0)
        tokens_out = int(total.get("tokensOut") or total.get("charsOut", 0) / 1.6)
        yuan = tokens_in / 1e6 * PRICE_IN + tokens_out / 1e6 * PRICE_OUT
        return {
            "tokensIn": tokens_in,
            "tokensOut": tokens_out,
            "yuan": round(yuan, 4),
            "priceIn": PRICE_IN,
            "priceOut": PRICE_OUT,
            "exact": real,          # true = 用的是接口回的真实 token
        }

    def reset(self) -> None:
        with self._lock:
            self.data = _new_day()
            self._dirty = True
        self.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            payload = json.dumps(self.data, ensure_ascii=False)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _flush_loop(self) -> None:
        while True:
            time.sleep(15)
            try:
                self.flush()
            except Exception:                                  # noqa: BLE001
                pass
