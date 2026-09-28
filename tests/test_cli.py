# SPDX-License-Identifier: GPL-2.0-or-later
import importlib
import sys

from panelbeater import cli
from panelbeater.config import Config


def test_config_write_without_a_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("PANELBEATER_CONFIG", raising=False)
    assert cli.main(["config", "--write"]) == 0
    assert (tmp_path / "panelbeater" / "config").is_file()


def test_auto_transport_without_pyusb(monkeypatch):
    # None in sys.modules makes `import usb` raise ImportError.
    monkeypatch.setitem(sys.modules, "usb", None)
    for name in [m for m in sys.modules if m.startswith("panelbeater.usb")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(cli, "usb_present", lambda: False)
    importlib.import_module("panelbeater.usb")
    assert cli.pick_transport(Config({"transport": "auto"})) == "network"
