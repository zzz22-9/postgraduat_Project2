"""Windows 演示启动/只读自检。后台与启动窗口分离，不依赖聊天终端。"""
import argparse
import errno
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8000"
LOGS = ROOT / "logs"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def stamp():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")


def fetch(path):
    with OPENER.open(URL + path, timeout=2) as response:
        return response.read().decode("utf-8")


def healthy():
    try:
        health = json.loads(fetch("/health"))
        return health.get("status") == "ok" and health.get("module") == "erp+mes"
    except (OSError, ValueError):
        return False


def occupied():
    # Windows 上无监听的连接也可能超时；直接短暂绑定确认能否使用端口。
    # 只做占用探测，不监听、不接收数据；启动失败仍由子进程日志报告。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", 8000))
            return False
        except OSError as error:
            if error.errno == errno.EADDRINUSE or error.winerror == 10048:
                return True
            raise RuntimeError(f"无法使用 8000 端口：{error}。未启动或终止服务。") from error


@contextmanager
def launch_lock():
    import msvcrt
    LOGS.mkdir(exist_ok=True)
    with (LOGS / "launcher.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("另一个启动检查正在执行，请稍候再试。") from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def ensure_server():
    if healthy():
        print("已找到运行中的工厂服务，复用现有服务。")
        print("如果它由旧终端启动，仍需保留该终端；只有本工具新启动的后台才与窗口分离。")
        return
    if occupied():
        raise RuntimeError("8000 端口被占用，但不是可用的工厂服务。未终止任何进程，请检查 logs 或联系维护者。")
    dependencies = subprocess.run(
        [sys.executable, "-B", "-c", "import fastapi, uvicorn, simpy"],
        capture_output=True, text=True, cwd=ROOT, timeout=15,
    )
    if dependencies.returncode:
        raise RuntimeError("依赖检查失败，请按 README 安装 requirements.txt。\n" + dependencies.stderr)
    log_path = LOGS / ("server-" + stamp() + ".log")
    with log_path.open("ab", buffering=0) as output:
        child = subprocess.Popen(
            [sys.executable, "-B", "-m", "uvicorn", "erp.app:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    print(f"已启动独立后台，PID {child.pid}；日志：{log_path}")
    (LOGS / "last-launch.json").write_text(json.dumps({
        "pid": child.pid, "started_beijing": stamp(), "log": str(log_path), "url": URL,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"后台启动失败，退出码 {child.returncode}。请查看 {log_path}")
        if healthy():
            return
        time.sleep(.3)
    raise RuntimeError(f"后台在 25 秒内未通过健康检查。不要反复启动，请查看 {log_path}")


def preflight():
    checks = []
    endpoints = [
        ("健康状态", "/health", lambda body: json.loads(body).get("status") == "ok" and json.loads(body).get("module") == "erp+mes"),
        ("首页与仿真入口", "/", lambda body: 'data-page="simulation"' in body),
        ("仿真页面脚本", "/web/simulation.js", lambda body: "renderSimulation" in body),
        ("ERP 订单读取", "/orders", lambda body: isinstance(json.loads(body), list)),
        ("MES 工单读取", "/mes/work-orders", lambda body: isinstance(json.loads(body), list)),
        ("仿真历史读取", "/simulation/runs", lambda body: isinstance(json.loads(body), list)),
    ]
    for name, path, validate in endpoints:
        try:
            if not validate(fetch(path)):
                raise ValueError("响应内容不符合预期")
            checks.append({"name": name, "ok": True})
        except (OSError, ValueError) as error:
            checks.append({"name": name, "ok": False, "error": str(error)})
        print(("[PASS] " if checks[-1]["ok"] else "[FAIL] ") + name)
    LOGS.mkdir(exist_ok=True)
    report = LOGS / "last-check.json"
    report.write_text(json.dumps({"checked_beijing": stamp(), "checks": checks}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"检查记录：{report}")
    return all(item["ok"] for item in checks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查访问，不启动后台、不写业务数据")
    parser.add_argument("--no-browser", action="store_true", help="不打开浏览器")
    args = parser.parse_args()
    try:
        if os.name != "nt":
            raise RuntimeError("此启动器用于 Windows；其他系统使用 README 中的 uvicorn 命令。")
        with launch_lock():
            if not args.check:
                ensure_server()
            if not preflight():
                raise RuntimeError("自检未通过。请查看上面的失败项和日志文件。")
        print("访问自检通过；这不替代完整业务演练。")
        if not args.check and not args.no_browser:
            webbrowser.open(URL)
        return 0
    except Exception as error:
        print(f"[FAIL] {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
