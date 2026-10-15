#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SnowLuma 一键登录 / 登录窗恢复助手（在 snowluma 容器内运行，DISPLAY=:1）。

分类与动作（login 模式，2026-10-14 增强）：
  * 蓝带 ≥ 1500  → 「一键登录」界面（头像+自动登录+登录按钮）→ 勾自动登录 + 点登录
  * 中心暗像素占比 ≥ 0.12 → 真二维码 → 点卡片内「刷新」拿新码
  * 两者都不是（如「下线通知 您的账号已在别处登陆」弹窗、空白过渡窗）
        → 先点弹窗的「确定」把它关掉（关掉后窗口会回到一键登录 / 二维码界面）；
          还不行才重启该账号的 supervisor program。
          **重启前必须清掉脱离 supervisor 进程组的残留 QQ**（QQ 会自己
          `/opt/QQ/qq --relaunch` 拉一个 ppid=1 的新进程，stopasgroup 管不到），
          否则同号双客户端立刻再次互踢，窗口又回到「下线通知」。

logout 模式（2026-10-14 新增，用户点「退出登录」时调用）：
  停程序 → 清残留 QQ 进程 → 把 `$HOME/.config/QQ` 整个挪到 `QQ.logout-<ts>`
  （可逆备份）→ 起程序。⚠️ 只删 `auth/login.enc` 不够（QQ 会从 `nt_qq_*` 里
  把凭据重建回来），必须挪走整个配置目录才会真正回到扫码界面。
  用于「登错账号」：清完即回到扫码界面，用户重新扫正确的号。

标定（2026-10-14 实测）：
  一键登录界面 蓝带 = 10024；过渡窗 = 0；阈值 1500（6.7 倍余量）
  坐标：○自动登录 (132,268)、登录按钮 (160,327)、卡片内刷新 (160,280)
  二维码区域（窗口内）：x 70..250, y 155..275

注意：截图必须走**根窗口 + 裁剪**。QQ 登录窗内容在子窗口里且窗口无 backing store，
`xwd -id <win>` 在被遮挡时返回全白空图（本项目已实测踩过）。

用法：
  python3 snowluma-oneclick.py classify <png路径>
  python3 snowluma-oneclick.py login    <supervisor程序名>   # 分类并自动处置
  python3 snowluma-oneclick.py probe    <supervisor程序名>   # 只读
  python3 snowluma-oneclick.py logout   <supervisor程序名>   # 退出登录→回扫码界面
"""
import ctypes  # noqa: F401  (保留：提取器内部会用)
import os
import runpy
import shutil
import subprocess
import sys
import time

os.environ.setdefault('DISPLAY', ':1')

WIN_W, WIN_H = 320, 460
BLUE_BAND = (305, 350)
BLUE_MIN = 1500
QR_BOX = (70, 250, 155, 275)      # x0, x1, y0, y1（窗口内）
QR_DARK_MIN = 0.12                # 中心暗像素占比阈值
RX_TICK, RY_TICK = 132, 268
RX_LOGIN, RY_LOGIN = 160, 327
RX_REFRESH, RY_REFRESH = 160, 280
# 「下线通知」弹窗的「确定」按钮（窗口相对坐标，2026-10-14 实测）
RX_POPUP_OK, RY_POPUP_OK = 237, 290
EXTRACTOR = '/tmp/dump-qr.py'

_M = None


def _mod():
    global _M
    if _M is None:
        _M = runpy.run_path(EXTRACTOR)
    return _M


def blue_from_raw(raw, w=WIN_W, h=WIN_H):
    """统计 y=305..350 区间内的「QQ 蓝」像素数。"""
    if not raw or len(raw) < w * h * 3:
        return -1
    y0, y1 = BLUE_BAND
    n = 0
    for y in range(y0, min(y1, h)):
        base = y * w * 3
        for x in range(w):
            o = base + x * 3
            r, g, b = raw[o], raw[o + 1], raw[o + 2]
            if b > 180 and r < 150 and g < 200:
                n += 1
    return n


def qr_dark_ratio(raw, w=WIN_W, h=WIN_H):
    """二维码区域内的暗像素占比（真二维码约 0.2~0.4，弹窗/空白 <0.05）。"""
    if not raw or len(raw) < w * h * 3:
        return -1.0
    x0, x1, y0, y1 = QR_BOX
    dark = tot = 0
    for y in range(y0, min(y1, h), 2):
        base = y * w * 3
        for x in range(x0, min(x1, w), 2):
            o = base + x * 3
            if (raw[o] + raw[o + 1] + raw[o + 2]) / 3 < 110:
                dark += 1
            tot += 1
    return dark / max(1, tot)


def _decode(src, dst):
    subprocess.run('ffmpeg -y -v error -i %s -f rawvideo -pix_fmt rgb24 %s' % (src, dst),
                   shell=True, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with open(dst, 'rb') as fh:
            return fh.read()
    except Exception:                                   # noqa: BLE001
        return b''


def classify_png(png):
    return blue_from_raw(_decode(png, '/tmp/_oc_png.raw'))


def _abs_pos(wid):
    info = subprocess.check_output(['xwininfo', '-id', wid]).decode()
    x = int([l.split(':')[1] for l in info.splitlines() if 'Absolute upper-left X' in l][0])
    y = int([l.split(':')[1] for l in info.splitlines() if 'Absolute upper-left Y' in l][0])
    return x, y


def _nudge(wid):
    """在窗口内点一下「刷新」位置，触发重绘。

    QQ 登录窗没有 backing store：raise 之后若不产生输入事件，窗口内容可能仍是
    「未绘制」状态（截出来全白，实测 (6,6) 点无效）。点 (160,280) 会触发 QQ 重绘，
    且对三种状态都安全/有益：
      * 二维码界面   -> 等于刷新二维码（本来就要做的）
      * 一键登录界面 -> 落在自动登录与登录按钮之间的空白区，无副作用
      * 下线通知弹窗 -> 大概率命中弹窗按钮，直接把弹窗关掉
    """
    try:
        _click(wid, RX_REFRESH, RY_REFRESH)
    except Exception:                                   # noqa: BLE001
        pass
    time.sleep(2.0)


def grab_win(wid):
    """抓窗口内容：**根窗口截图 + 按窗口矩形裁剪**（避免无 backing store 时全白）。"""
    ax, ay = _abs_pos(wid)
    subprocess.run('xwd -root -silent -out /tmp/_oc_root.xwd', shell=True, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(
        'ffmpeg -y -v error -f xwd_pipe -i /tmp/_oc_root.xwd -vf "crop=%d:%d:%d:%d" '
        '-f rawvideo -pix_fmt rgb24 /tmp/_oc_win.raw' % (WIN_W, WIN_H, ax, ay),
        shell=True, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with open('/tmp/_oc_win.raw', 'rb') as fh:
            return fh.read()
    except Exception:                                   # noqa: BLE001
        return b''


def _click(wid, rx, ry):
    m = _mod()
    xtst, dpy, x11 = m['xtst'], m['dpy'], m['x11']
    ax, ay = _abs_pos(wid)
    x, y = ax + rx, ay + ry
    xtst.XTestFakeMotionEvent(dpy, -1, int(x), int(y), 0)
    x11.XFlush(dpy)
    time.sleep(0.3)
    xtst.XTestFakeButtonEvent(dpy, 1, True, 0)
    x11.XFlush(dpy)
    time.sleep(0.15)
    xtst.XTestFakeButtonEvent(dpy, 1, False, 0)
    x11.XFlush(dpy)
    time.sleep(0.5)


def _pids_for_home(home):
    """所有 HOME 指向该账号的进程（含 supervisor 管不到的孤儿）。"""
    out = []
    for p in os.listdir('/proc'):
        if not p.isdigit():
            continue
        try:
            env = open('/proc/%s/environ' % p, 'rb').read().split(b'\0')
        except Exception:                                   # noqa: BLE001
            continue
        for kv in env:
            if kv.startswith(b'HOME=') and kv[5:].decode() == home:
                out.append(int(p))
                break
    return out


def kill_orphans(home):
    """清掉该账号残留的 QQ 进程（SIGTERM → SIGKILL）。

    QQ 登录后会以 `/opt/QQ/qq --relaunch` 拉一个脱离原进程组的新进程（ppid=1），
    supervisor 的 stopasgroup/killasgroup 杀不到它。于是 restart 之后同号双客户端
    互相踢，登录窗永远停在「下线通知」。重启/退出登录前必须先清干净。
    """
    import signal
    killed = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = _pids_for_home(home)
        if not pids:
            break
        for p in pids:
            try:
                os.kill(p, sig)
                killed.append(p)
            except Exception:                               # noqa: BLE001
                pass
        time.sleep(2.0)
    return killed


def clear_login_state(home):
    """删掉该账号盘上的登录凭据 = 退出登录（下次启动回到扫码界面）。"""
    removed = []
    auth = os.path.join(home, '.config', 'QQ', 'auth')
    names = os.listdir(auth) if os.path.isdir(auth) else []
    for name in names:
        p = os.path.join(auth, name)
        try:
            os.remove(p)
            removed.append(p)
        except Exception:                                   # noqa: BLE001
            pass
    return removed


def _resolve(prog):
    m = _mod()
    acct = m['ACCOUNTS'].get(prog)
    if not acct:
        return None, None, None
    img, home = acct
    win = m['login_wins']().get(img)
    return img, home, win


def do_login(prog):
    """一键登录。**退出码契约**（面板据此判定 ok，不再靠猜）：

    0 = 确实点了「自动登录 + 登录」（真一键登录）
    4 = 窗口是扫码登录界面，只刷新了二维码（需要用户扫码，并未完成登录）
    3 = 没有 320x460 登录窗（已登录 / 程序未运行 / 窗口尚未映射）
    2 = 账号未配置
    """
    m = _mod()
    img, home, win = _resolve(prog)
    if img is None:
        print('RESULT=error REASON=unknown-program %s' % prog)
        return 2
    if not win:
        # 2026-10-15 修复：旧实现这里 return 0 —— 一步没做却向面板报「成功」，
        # 面板 ok=true、operation_logs 记 ok=1（假成功，排查时完全看不出真相）。
        # 现在返回非 0，并把「没有登录窗」这个事实如实报出去，由面板结合权威
        # 登录态（网关日志 / 心跳）决定提示文案。
        print('RESULT=no-window img=%s home=%s （无 320x460 登录窗：已登录 / 未运行 / '
              '窗口未映射）' % (img, home))
        return 3
    wid, _x, _y = win
    m['isolate'](wid)               # 独占位置：否则会点到/截到别人的登录窗
    time.sleep(1.0)
    _nudge(wid)
    raw = grab_win(wid)
    blue = blue_from_raw(raw)
    ratio = qr_dark_ratio(raw)
    print('DETECT img=%s wid=%s home=%s 蓝带=%d 暗占比=%.3f' % (img, wid, home, blue, ratio))

    if blue >= BLUE_MIN:
        print('ACTION=tick+login REASON=oneclick-ui')
        _click(wid, RX_TICK, RY_TICK)
        _click(wid, RX_LOGIN, RY_LOGIN)
        print('RESULT=clicked img=%s home=%s' % (img, home))
        return 0

    if ratio >= QR_DARK_MIN:
        print('ACTION=refresh-qr REASON=qr-detected')
        _click(wid, RX_REFRESH, RY_REFRESH)
        print('RESULT=refreshed img=%s home=%s' % (img, home))
        return 4

    # 既不是一键登录界面，也不是二维码 ⇒ 多半是「下线通知」弹窗 / 空白过渡窗。
    # 2026-10-14 修复：先点弹窗的「确定」把它关掉；关掉后窗口会回到一键登录 / 二维码。
    print('ACTION=dismiss-popup REASON=not-qr-not-oneclick 暗占比=%.3f 蓝带=%d' % (ratio, blue))
    _click(wid, RX_POPUP_OK, RY_POPUP_OK)
    time.sleep(2.5)
    raw2 = grab_win(wid)
    blue2 = blue_from_raw(raw2)
    ratio2 = qr_dark_ratio(raw2)
    print('DETECT2 img=%s wid=%s 蓝带=%d 暗占比=%.3f' % (img, wid, blue2, ratio2))
    if blue2 >= BLUE_MIN:
        print('ACTION=tick+login REASON=oneclick-after-dismiss')
        _click(wid, RX_TICK, RY_TICK)
        _click(wid, RX_LOGIN, RY_LOGIN)
        print('RESULT=clicked img=%s home=%s' % (img, home))
        return 0
    if ratio2 >= QR_DARK_MIN:
        print('ACTION=refresh-qr REASON=qr-after-dismiss')
        _click(wid, RX_REFRESH, RY_REFRESH)
        print('RESULT=refreshed img=%s home=%s' % (img, home))
        return 4
    # 关掉弹窗还不行 ⇒ 重启程序；重启前必须清掉脱离 supervisor 进程组的残留 QQ，
    # 否则同号双客户端立刻再次互踢，窗口又回到「下线通知」。
    print('ACTION=restart-program REASON=still-not-qr-not-oneclick 暗占比=%.3f 蓝带=%d'
          % (ratio2, blue2))
    killed = kill_orphans(home)
    print('STEP=kill-orphans pids=%s' % killed)
    r = subprocess.run(['supervisorctl', 'restart', prog], capture_output=True)
    out = (r.stdout or b'').decode(errors='replace').strip()
    print('RESULT=restarted img=%s home=%s rc=%d %s' % (img, home, r.returncode, out[:200]))
    return 0


def do_logout(prog):
    """退出登录 → 回到扫码界面（清除该账号在本机的 QQ 配置）。

    登错账号时用：停程序 → 清残留 QQ 进程 → 把 `$HOME/.config/QQ` 整个挪到
    `$HOME/.config/QQ.logout-<ts>`（可逆备份）→ 起程序。

    ⚠️ 只删 `auth/login.enc` **不够**：2026-10-14 实测删掉后 15s 内就被重建回
    425B（QQ 从 `nt_qq_*` 里的会话数据恢复），账号仍是已登录态、窗口照旧不回到
    二维码。必须把整个 QQ 配置目录挪走，QQ 才会以全新身份启动并显示二维码
    （实测：`login.enc` 变成 42B、窗口 kind=qr、fetch_qr 返回真码）。
    """
    img, home, _win = _resolve(prog)
    if img is None:
        print('RESULT=error REASON=unknown-program %s' % prog)
        return 2
    r = subprocess.run(['supervisorctl', 'stop', prog], capture_output=True)
    print('STEP=stop rc=%d %s' % (r.returncode,
                                  (r.stdout or b'').decode(errors='replace').strip()[:120]))
    killed = kill_orphans(home)
    print('STEP=kill-orphans pids=%s' % killed)
    removed = clear_login_state(home)
    print('STEP=clear-login-state removed=%s' % removed)

    qqdir = os.path.join(home, '.config', 'QQ')
    cfg = os.path.dirname(qqdir)
    ts = time.strftime('%Y%m%d-%H%M%S')
    # 只保留最新一份备份，避免重复退出把磁盘堆满
    if os.path.isdir(cfg):
        for name in os.listdir(cfg):
            if name.startswith('QQ.logout-'):
                try:
                    shutil.rmtree(os.path.join(cfg, name))
                except Exception:                           # noqa: BLE001
                    pass
    moved = None
    if os.path.isdir(qqdir):
        moved = '%s.logout-%s' % (qqdir, ts)
        try:
            os.rename(qqdir, moved)
        except Exception as exc:                            # noqa: BLE001
            print('STEP=move-profile FAILED %s' % exc)
            moved = None
    print('STEP=move-profile %s -> %s' % (qqdir, moved))

    r = subprocess.run(['supervisorctl', 'start', prog], capture_output=True)
    print('STEP=start rc=%d %s' % (r.returncode,
                                   (r.stdout or b'').decode(errors='replace').strip()[:120]))
    print('RESULT=logged-out img=%s home=%s profile_backup=%s' % (img, home, moved))
    return 0


def do_probe(prog):
    img, home, win = _resolve(prog)
    if img is None:
        print('RESULT=error REASON=unknown-program %s' % prog)
        return 2
    if not win:
        print('RESULT=no-window img=%s home=%s' % (img, home))
        return 0
    wid, _x, _y = win
    m = _mod()
    m['isolate'](wid)               # 独占位置：否则会点到/截到别人的登录窗
    time.sleep(1.0)
    _nudge(wid)
    raw = grab_win(wid)
    blue = blue_from_raw(raw)
    ratio = qr_dark_ratio(raw)
    kind = 'oneclick' if blue >= BLUE_MIN else ('qr' if ratio >= QR_DARK_MIN else 'other')
    print('RESULT=window img=%s wid=%s home=%s 蓝带=%d 暗占比=%.3f kind=%s'
          % (img, wid, home, blue, ratio, kind))
    m['restore'](wid)
    return 0


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    mode, arg = sys.argv[1], sys.argv[2]
    if mode == 'classify':
        print('BLUEBAND=%d' % classify_png(arg))
        return 0
    if mode == 'login':
        return do_login(arg)
    if mode == 'probe':
        return do_probe(arg)
    if mode == 'logout':
        return do_logout(arg)
    print('unknown mode: %s' % mode)
    return 2


if __name__ == '__main__':
    sys.exit(main())
