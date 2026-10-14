#!/usr/bin/env python3
"""
NapCat QQ Manager - 一键启动部署脚本
用法:
    python start.py              # 默认启动 (端口 8000)
    python start.py --port 9000  # 指定端口
    python start.py --skip-build # 跳过前端构建
    python start.py --force-build # 强制重新构建前端
    python start.py --dev        # 开发模式 (热重载)
"""
import os
import sys
import subprocess
import argparse
import shutil
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
FRONTEND_DIST = os.path.join(FRONTEND_DIR, "dist")
PROJECT_VENV_DIR = os.path.join(BASE_DIR, ".venv")
UV_BOOTSTRAP_MARK = "NCQQ_UV_BOOTSTRAPPED"


def _find_uv_bin() -> str:
    uv_bin = os.environ.get("UV_BIN") or shutil.which("uv")
    if not uv_bin:
        fail("未检测到 uv，请先安装：https://docs.astral.sh/uv/")
        sys.exit(1)
    return uv_bin


def _in_virtualenv() -> bool:
    return (
        hasattr(sys, "real_prefix")
        or sys.prefix != getattr(sys, "base_prefix", sys.prefix)
        or bool(os.environ.get("VIRTUAL_ENV"))
    )


def ensure_uv_runtime() -> None:
    """默认使用 uv 管理运行环境；未检测到虚拟环境时自动创建并重启。"""
    if os.environ.get(UV_BOOTSTRAP_MARK) == "1":
        return

    step("检测 Python 运行环境")
    info(f"当前解释器: {sys.executable}")
    if _in_virtualenv():
        info("已处于虚拟环境，继续执行 uv 依赖同步")
        return

    uv_bin = _find_uv_bin()
    info(f"uv 可执行文件: {uv_bin}")
    if not os.path.isdir(PROJECT_VENV_DIR):
        info("未发现项目 .venv，正在使用 uv 创建")
        r = subprocess.run([uv_bin, "venv", PROJECT_VENV_DIR], capture_output=True, text=True, cwd=BASE_DIR)
        if r.returncode != 0:
            fail("uv venv 创建失败:\n" + (r.stderr or r.stdout))
            sys.exit(1)
    else:
        info("检测到项目已有 .venv，跳过创建")

    target_python = os.path.join(PROJECT_VENV_DIR, "Scripts", "python.exe") if sys.platform == "win32" else os.path.join(PROJECT_VENV_DIR, "bin", "python")
    info(f"目标解释器: {target_python}")
    info("使用 uv 重新拉起 start.py")
    env = {**os.environ, UV_BOOTSTRAP_MARK: "1"}
    cmd = [uv_bin, "run", "python", os.path.join(BASE_DIR, "start.py"), *sys.argv[1:]]
    rr = subprocess.run(cmd, cwd=BASE_DIR, env=env)
    sys.exit(rr.returncode)


# 关键 Python 依赖（安装后验证可导入）
REQUIRED_MODULES = [
    "fastapi", "uvicorn", "docker", "aiohttp", "aiodocker", "orjson",
    "multipart", "PIL", "websockets", "wsproto"
]

# ─── 终端彩色输出 ───
def _c(text: str, code: str) -> str:
    if sys.platform == "win32":
        _ = os.system("")  # 启用 Windows ANSI
    return f"\033[{code}m{text}\033[0m"

def info(msg: str) -> None:  print(_c(f"[✓] {msg}", "32"))
def warn(msg: str) -> None:  print(_c(f"[!] {msg}", "33"))
def fail(msg: str) -> None:  print(_c(f"[✗] {msg}", "31"))
def step(msg: str) -> None:  print(_c(f"\n>>> {msg}", "36;1"))

BANNER = r"""
 _   _             ____      _     __  __
| \ | | __ _ _ __ / ___|__ _| |_  |  \/  | __ _ _ __   __ _  __ _  ___ _ __
|  \| |/ _` | '_ \ |   / _` | __| | |\/| |/ _` | '_ \ / _` |/ _` |/ _ \ '__|
| |\  | (_| | |_) | |__| (_| | |_  | |  | | (_| | | | | (_| | (_| |  __/ |
|_| \_|\__,_| .__/ \____\__,_|\__| |_|  |_|\__,_|_| |_|\__,_|\__, |\___|_|
             |_|                                               |___/
"""

# ─── 检查项 ───

def check_python():
    """检查 Python 版本 >= 3.10"""
    step("检查 Python 环境")
    v = sys.version_info
    if v < (3, 10):
        fail(f"需要 Python >= 3.10，当前版本: {v.major}.{v.minor}.{v.micro}")
        sys.exit(1)
    info(f"Python {v.major}.{v.minor}.{v.micro}")


def check_pip_deps():
    """使用 uv 同步 Python 依赖并验证关键模块"""
    step("同步 Python 依赖")
    req = os.path.join(BASE_DIR, "requirements.txt")
    pyproject = os.path.join(BASE_DIR, "pyproject.toml")
    uv_bin = _find_uv_bin()

    if os.path.exists(pyproject):
        r = subprocess.run([uv_bin, "sync", "--frozen"], capture_output=True, text=True, cwd=BASE_DIR)
        if r.returncode != 0:
            r = subprocess.run([uv_bin, "sync"], capture_output=True, text=True, cwd=BASE_DIR)
            if r.returncode != 0:
                fail("uv sync 失败:\n" + (r.stderr or r.stdout))
                sys.exit(1)
        info("uv sync 完成")
    elif os.path.exists(req):
        r = subprocess.run([uv_bin, "pip", "install", "-q", "-r", req], capture_output=True, text=True, cwd=BASE_DIR)
        if r.returncode != 0:
            fail("uv pip install 失败:\n" + (r.stderr or r.stdout))
            sys.exit(1)
        info("Python 依赖已通过 uv 安装")
    else:
        fail("未找到 pyproject.toml 或 requirements.txt")
        sys.exit(1)

    # 验证关键模块可导入
    missing = []
    for mod in REQUIRED_MODULES:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        fail(f"以下模块安装后仍无法导入: {', '.join(missing)}")
        fail("请执行: uv sync")
        fail("Ubuntu 常见依赖: uvicorn[standard]（websockets/wsproto）、python-multipart、pillow、wsproto")
        fail("若 Node.js < 18，请升级后再构建前端（建议 Node.js 20 LTS）")
        sys.exit(1)

    info(f"关键依赖验证通过 ({len(REQUIRED_MODULES)} 个模块)")


def check_node():
    """检查 Node.js / npm（前端构建要求 Node.js >= 18）"""
    step("检查 Node.js 环境")
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        warn("未检测到 Node.js / npm，无法构建前端")
        warn("请安装 Node.js >= 18: https://nodejs.org/")
        return False

    v = subprocess.run([node, "--version"], capture_output=True, text=True)
    version_raw = v.stdout.strip()
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version_raw)
    if not m:
        warn(f"无法解析 Node.js 版本: {version_raw}")
        warn("请使用 Node.js >= 18")
        return False

    major = int(m.group(1))
    info(f"Node.js {version_raw}")
    if major < 18:
        warn(f"Node.js 版本过低: {version_raw}，前端构建需要 >= 18")
        warn("请升级后重试（建议 Node.js 20 LTS）")
        return False

    return True


def build_frontend():
    """构建前端"""
    step("构建前端资源")
    if not os.path.exists(os.path.join(FRONTEND_DIR, "package.json")):
        warn("frontend/package.json 不存在，跳过构建")
        return
    # npm install
    info("正在安装前端依赖 (npm install)...")
    r = subprocess.run(["npm", "install"], cwd=FRONTEND_DIR)
    if r.returncode != 0:
        fail("npm install 失败")
        sys.exit(1)
    # npm run build
    info("正在构建前端 (npm run build)...")
    r = subprocess.run(["npm", "run", "build"], cwd=FRONTEND_DIR)
    if r.returncode != 0:
        fail("前端构建失败")
        sys.exit(1)
    info("前端构建成功")


def check_docker():
    """检查 Docker 可用性与版本"""
    step("检查 Docker 环境")
    docker = shutil.which("docker")
    if not docker:
        warn("未检测到 Docker，容器管理功能将不可用")
        warn("请安装 Docker >= 20.10: https://docs.docker.com/get-docker/")
        return
    r = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if r.returncode != 0:
        warn("Docker 已安装但未运行或无权限")
        return
    # 检查版本号
    rv = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                        capture_output=True, text=True)
    ver_str = rv.stdout.strip() if rv.returncode == 0 else ""
    m = re.match(r"(\d+)\.(\d+)", ver_str)
    if m:
        major, minor = int(m.group(1)), int(m.group(2))
        if major < 20 or (major == 20 and minor < 10):
            warn(f"Docker 版本 {ver_str} 较旧，建议升级到 >= 20.10")
        else:
            info(f"Docker {ver_str}")
    else:
        info("Docker 运行正常")




def start_server(port: int, dev: bool):
    """启动后端服务"""
    # 从配置读取 host（首次初始化设置中用户选择的绑定地址）
    from services.config import app_config, APP_VERSION
    host = app_config.get("host", "0.0.0.0")
    configured_port = app_config.get("port", 8000)
    # 命令行 --port 优先；否则使用配置文件中的端口
    actual_port = port if port != 8000 else configured_port

    step(f"启动 NapCat Manager v{APP_VERSION}")
    if not os.path.exists(FRONTEND_DIST):
        warn("前端未构建 (frontend/dist 不存在)，页面将显示提示信息")

    addr = f"http://{'localhost' if host == '0.0.0.0' else host}:{actual_port}"
    info(f"面板地址: {addr}")
    info(f"健康检查: {addr}/api/health")
    info(f"用户控制台: {addr}/user")

    if not app_config.get("initialized", False):
        info("首次启动 — 请打开浏览器完成初始化设置")

    # 架构信息
    step("运行时架构")
    info("Docker API: aiodocker 纯异步 (热路径 + CRUD)")
    info("集群通信: aiohttp 异步 HTTP")
    info("WS 推送: /ws/public 按页订阅 + orjson 序列化")
    info("事件驱动: Docker Events 实时感知 + 自适应轮询兜底")
    info("状态引擎: ContainerStateEngine 后台异步刷新")

    info("\n按 Ctrl+C 停止服务\n")
    try:
        import uvicorn
        uvicorn.run(
            "main:app",
            host=host,
            port=actual_port,
            reload=dev,
            log_level="info",
        )
    except KeyboardInterrupt:
        info("\n服务已停止")


# ─── 主流程 ───

def main():
    print(BANNER)
    parser = argparse.ArgumentParser(description="NapCat QQ Manager 一键启动")
    parser.add_argument("--port", type=int, default=8000, help="服务端口 (默认 8000)")
    parser.add_argument("--skip-build", action="store_true", help="跳过前端构建")
    parser.add_argument("--force-build", action="store_true", help="强制重新构建前端")
    parser.add_argument("--dev", action="store_true", help="开发模式 (热重载)")
    args = parser.parse_args()

    os.chdir(BASE_DIR)

    ensure_uv_runtime()
    check_python()
    check_pip_deps()
    check_docker()

    if not args.skip_build:
        if check_node():
            if args.force_build or not os.path.exists(FRONTEND_DIST) or args.dev:
                build_frontend()
            else:
                info("前端已构建，使用 --force-build 强制重构建")
    else:
        info("已跳过前端构建 (--skip-build)")

    start_server(args.port, args.dev)


if __name__ == "__main__":
    main()