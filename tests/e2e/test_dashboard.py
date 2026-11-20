"""End-to-end dashboard flows with an isolated external-workspace database."""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest


playwright_sync = pytest.importorskip("playwright.sync_api")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _chrome_path() -> str | None:
    candidate = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return str(candidate) if candidate.exists() else None


def test_dashboard_core_actions() -> None:
    with tempfile.TemporaryDirectory(prefix=".test-p4-", dir=PROJECT_ROOT) as test_dir:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment.update(
            ADMIN_USER="e2e-admin", ADMIN_PASS="e2e-strong-password",
            APP_SECRET="e2e-local-secret", PYTHONPATH=str(PROJECT_ROOT),
            TMPDIR=test_dir, XDG_CACHE_HOME=test_dir,
            PYTHONPYCACHEPREFIX=str(Path(test_dir) / "pycache"),
            PLAYWRIGHT_BROWSERS_PATH=environment.get("PLAYWRIGHT_BROWSERS_PATH", str(Path(test_dir) / "browsers")),
        )
        with (Path(test_dir) / "server.log").open("w") as log_file:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                 "--port", str(port), "--no-proxy-headers"],
                cwd=test_dir, env=environment, stdout=log_file, stderr=subprocess.STDOUT,
            )
            try:
                for _ in range(100):
                    try:
                        if httpx.get(f"{base}/health", timeout=0.5).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if process.poll() is not None:
                        raise AssertionError((Path(test_dir) / "server.log").read_text())
                    time.sleep(0.1)
                else:
                    raise AssertionError("Dashboard test server did not start")

                with playwright_sync.sync_playwright() as driver:
                    launch = {"headless": True}
                    if chrome := _chrome_path():
                        launch["executable_path"] = chrome
                    browser = driver.chromium.launch(**launch)
                    page = browser.new_page(viewport={"width": 1365, "height": 900})
                    errors: list[str] = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(base, wait_until="domcontentloaded")
                    page.get_by_placeholder("管理员账号").fill("e2e-admin")
                    page.get_by_placeholder("管理员密码").fill("e2e-strong-password")
                    page.get_by_role("button", name="登录", exact=True).click()
                    page.get_by_role("button", name="添加设备").wait_for()
                    page.get_by_role("button", name="切换语言 EN").click()
                    page.get_by_role("heading", name="Live target wall").wait_for()
                    assert page.locator("html").get_attribute("lang") == "en"
                    page.get_by_role("button", name="Switch language 中文").click()
                    page.get_by_role("heading", name="监控目标实时墙").wait_for()
                    page.get_by_role("button", name="浅色").click()
                    assert "light" in page.locator("body").get_attribute("class")
                    page.get_by_role("button", name="深色").click()
                    page.keyboard.press("/")
                    assert page.get_by_label("搜索设备名称或 IP").evaluate("el => document.activeElement === el")

                    page.get_by_role("button", name="添加设备").click()
                    page.get_by_placeholder("设备名称", exact=True).fill("E2E Gateway")
                    page.get_by_text("请填写有效 IP 地址或主机名，不能包含空格、斜杠").wait_for()
                    page.get_by_placeholder("IP地址或主机名").fill("127.0.0.1")
                    page.get_by_role("button", name="新增设备").click()
                    page.get_by_text("E2E Gateway").first.wait_for()

                    page.get_by_role("button", name="添加设备").click()
                    page.get_by_role("button", name="批量导入", exact=True).click()
                    page.locator(".dialog textarea").fill(
                        "Valid,127.0.0.2,Test\nInvalid,not/host,Test\n"
                    )
                    page.get_by_role("button", name="预览并校验").click()
                    page.get_by_text("可导入 1 行，需修正 1 行").wait_for()
                    page.get_by_role("button", name="确认导入 1 行").click()
                    page.get_by_text("导入完成: 新增 1，跳过 0").wait_for()
                    page.get_by_role("dialog", name="资产管理").get_by_role("button", name="关").click()

                    page.get_by_label("搜索设备名称或 IP").fill("E2E Gateway")
                    page.wait_for_function("document.querySelectorAll('.device').length === 1")
                    page.locator(".device").click()
                    drawer = page.locator(".drawer")
                    drawer.get_by_role("button", name="30 分钟").click()
                    assert drawer.locator("input[type=datetime-local]").input_value()
                    drawer.get_by_role("button", name="保存维护").click()
                    page.get_by_text("维护模式已保存").wait_for()
                    drawer.get_by_role("button", name="关").click()

                    page.get_by_label("搜索设备名称或 IP").fill("")
                    page.wait_for_function("document.querySelectorAll('.device').length === 2")
                    page.get_by_role("button", name="批量模式").click()
                    page.get_by_role("button", name="选择当前可见").click()
                    page.get_by_role("button", name="删除", exact=True).click()
                    page.get_by_placeholder("输入 2 确认删除").fill("2")
                    page.get_by_role("button", name="确认删除").click()
                    page.get_by_role("button", name="撤销删除").wait_for()
                    page.get_by_role("button", name="撤销删除").click()
                    page.get_by_text("已恢复 2 台，跳过 0 台").wait_for()
                    assert page.locator(".device").count() == 2
                    assert not errors, errors
                    browser.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
