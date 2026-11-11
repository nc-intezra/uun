"""Render demo-mode screenshots (SVG) of the TUI: python3 tests/shots.py"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # use this checkout, not an installed copy

from uun.app import UunApp
from uun.config import load_config


async def shoot(theme, name, keys=(), size=(150, 46)):
    cfg = load_config()
    cfg.theme = theme
    app = UunApp(cfg, demo=True)
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.5)
        for k in keys:
            await pilot.press(k)
            await pilot.pause(0.2)
        await pilot.pause(0.4)
        app.save_screenshot(f"docs/previews/{name}.svg")
        print(name, app.tmux.name, app.tmux.errors)


async def main():
    await shoot("default", "01-dashboard-default")
    await shoot("default", "02-mysql-no-update-inbound", keys=("down", "down"))
    await shoot("nord", "03-redis-alternatives-nord", keys=("down",))
    await shoot("gruvbox", "04-update-all-confirm-gruvbox", keys=("U",))
    await shoot("solarized-dark", "05-update-apt-solarized", keys=("shift+tab", "u"))
    await shoot("dracula", "06-help-dracula", keys=("question_mark",))
    await shoot("mono", "07-python-mono-16colour", keys=("down",) * 5)


asyncio.run(main())
