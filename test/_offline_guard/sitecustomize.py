# -*- coding: utf-8 -*-
"""
離線 Regression 網路阻擋器（由 run_offline_regression.py 經 PYTHONPATH 注入）
============================================================================
Python 啟動時會自動 import 同名模組，因此把這個目錄放進 PYTHONPATH，就能讓
**受測行程與它啟動的每一個 python 子行程**都套用同一道阻擋 —— 不需要修改任何受測程式。

阻擋範圍：
  socket.socket.connect / connect_ex → 記錄後丟 ConnectionRefusedError（不實際連線）
  涵蓋所有建在 socket 之上的用法（requests / urllib / http.client / SM2 登入 …）。

**為什麼要把 socket 從 sys.modules 移走**
  部分測試會快照 `set(sys.modules)` 來證明「某模組不具備網路能力」，例如
  test_phase6_safety_gate / last_control_store / control_authority / field_measure /
  pcs_control_executor / pcs_control_integration。若阻擋器把 socket 留在 sys.modules，
  那些斷言會變成假 FAIL。為了**不放寬任何既有斷言**，這裡在修補完成後把 socket 移出
  sys.modules，並改用 meta_path finder：之後任何人 import socket 都會拿到同一份
  **已修補**的模組物件，但在沒有人 import 之前，sys.modules 裡看不到它。

已知限制（阻擋不到，因此 Runner 另外保留排除清單）：
  - 非 python 的子行程（chrome.exe、git、bash 測試腳本）不受這裡影響。
  - 本機 socket（127.0.0.1）同樣會被擋下；若未來有測試需要本機伺服器，要另行處理。
"""
import importlib.util
import sys

_LOG = __import__("os").environ.get("OFFLINE_GUARD_LOG")

import socket as _socket_mod                                        # noqa: E402

_orig_connect = _socket_mod.socket.connect
_orig_connect_ex = _socket_mod.socket.connect_ex


def _note(addr):
    if not _LOG:
        return
    try:
        import os
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write("%d\t%r\n" % (os.getpid(), addr))
    except Exception:
        pass


def _connect(self, address):
    _note(address)
    raise ConnectionRefusedError(
        "offline regression guard：已阻擋對外連線 %r（本輪為純離線 Regression）" % (address,))


def _connect_ex(self, address):
    _note(address)
    return 10061                                    # WSAECONNREFUSED


_socket_mod.socket.connect = _connect
_socket_mod.socket.connect_ex = _connect_ex


class _PatchedSocketLoader:
    """把已修補的 socket 模組原樣交出去，不重新執行 socket.py（否則修補會失效）。"""

    def create_module(self, spec):
        return _socket_mod

    def exec_module(self, module):
        pass


class _PatchedSocketFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "socket":
            return None
        return importlib.util.spec_from_loader("socket", _PatchedSocketLoader())


sys.modules.pop("socket", None)                     # 讓 sys.modules 快照看不到 socket
sys.meta_path.insert(0, _PatchedSocketFinder())
