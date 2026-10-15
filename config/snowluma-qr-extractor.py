#!/usr/bin/env python3
"""SnowLuma QR extractor (DISPLAY :1, Xvfb 1920x1080).

Flow:
  1) 320x460 login window for the account (PID->HOME): move+raise, click the
     in-window refresh button, capture.
  2) NEW (2026-10-14): if no 320x460 window exists, QQ may render the login
     screen as a 320x460 card CENTRED inside the account's main window (this is
     what a session-expired account looks like). In that case capture the root
     window and crop the centred card -- no supervisor restart involved.
  3) Fallback (--restart): supervisorctl restart the instance so a fresh login
     window is spawned topmost, then refresh+capture.
"""
import os, subprocess, time, ctypes, sys

os.environ['DISPLAY'] = ':1'

OUT = '/tmp/qr-%s.png'
ACCOUNTS = {  # supervisor prog -> (img name, HOME)
    'qq-aemeathdtr'      : ('qq-3553104492', '/app/qq-3553104492'),
    'qq-always'          : ('qq-1577812682', '/app/qq-1577812682'),
    'qq-bmyjsl'          : ('qq-3677268915', '/app/qq-3677268915'),
    'qq-baobao'          : ('qq-1809274611', '/app/qq-1809274611'),
    'qq-dfn'             : ('qq-3884114579', '/app/qq-3884114579'),
    'qq-dmlh'            : ('qq-3939309590', '/app/qq-3939309590'),
    'qq-fox'             : ('qq-3876621705', '/app/qq-3876621705'),
    'qq-gbna5'           : ('qq-864439782', '/app/qq-864439782'),
    'qq-gbna6'           : ('qq-3208809890', '/app/qq-3208809890'),
    'qq-hyy'             : ('qq-3964074385', '/app/qq-3964074385'),
    'qq-msx'             : ('qq-2889600671', '/app/qq-2889600671'),
    'qq-qq-1040987793'   : ('qq-2123474675', '/app/qq-2123474675'),
    'qq-qq-1548866093'   : ('qq-2811969179', '/app/qq-2811969179'),
    'qq-qq-1610677706'   : ('qq-3836059536', '/app/qq-3836059536'),
    'qq-qq-213437027'    : ('qq-1326135700', '/app/qq-1326135700'),
    'qq-qq-2154700056-2' : ('qq-1933406749', '/app/qq-1933406749'),
    'qq-qq-2154700056'   : ('qq-3790928530', '/app/qq-3790928530'),
    'qq-qq-2293270861'   : ('qq-1363627724', '/app/qq-1363627724'),
    'qq-qq-2329701564'   : ('qq-1219321852', '/app/qq-1219321852'),
    'qq-qq-2542872932'   : ('qq-3628805748', '/app/qq-3628805748'),
    'qq-qq-2953552416'   : ('qq-3756506941', '/app/qq-3756506941'),
    'qq-qq-3234171279'   : ('qq-2136074134', '/app/qq-2136074134'),
    'qq-qq-3409247004'   : ('qq-3670406718', '/app/qq-3670406718'),
    'qq-qq-3632794275'   : ('qq-3875997170', '/app/qq-3875997170'),
    'qq-qq-3752895474'   : ('qq-3859754500', '/app/qq-3859754500'),
    'qq-qq-616490834'    : ('qq-1497984418', '/app/qq-1497984418'),
    'qq-sghj'            : ('qq-310305581', '/app/qq-310305581'),
    'qq-xyj-3228731821'  : ('qq-3228731821', '/app/qq-3228731821'),
    'qq-xyqx'            : ('qq-179067589', '/app/qq-179067589'),
    'qq-ymlq-2'          : ('qq-474405282', '/app/qq-474405282'),
    'qq-yunowo'          : ('qq-3208223790', '/app/qq-3208223790'),
    'qq-yun'             : ('qq-2313357961', '/app/qq-2313357961'),
    'qq-lx'              : ('qq-3639258463', '/app/qq-3639258463'),
    'qq-shinomiya'       : ('qq-3031632149', '/app/qq-3031632149'),
    'qq-yg'              : ('qq-1486758163', '/app/qq-1486758163'),
}
HOME2IMG = {v[1]: v[0] for v in ACCOUNTS.values()}

# 登录卡片固定尺寸：QQ 无论主窗多大，登录界面都是 320x460 且在主窗内居中。
CARD_W, CARD_H = 320, 460
# 卡片内「刷新」按钮中心（卡片相对坐标，实测 2026-10-14）
CARD_REFRESH = (160, 280)

# ---- 680x440 双栏登录窗（左栏「账密登录」| 右栏「扫码登录」）----
# 2026-10-15 实测标定（苏州 qq-dfn 真机 680x440 截图）。
# 这是「二维码暂不可用（登录窗口未就绪）」的真正根因：旧 login_wins() 只认
# 320x460，双栏窗被判成「没有登录窗」→ 走 spawn_fresh 兜底 → 而面板的 strict
# 包装把 spawn_fresh 阉掉（绝不重启）→ exit 1 → 前端只能报「窗口未就绪」。
DUAL_W, DUAL_H = 680, 440
DUAL_QR_CARD = (462, 96, 156, 166)   # 右栏二维码卡片 (x, y, w, h)，窗口相对
DUAL_REFRESH = (540, 188)            # 卡片内「刷新」按钮中心，窗口相对
# 双栏窗的候选落点（必须让 680x440 完整落在屏幕内）
_DUAL_CANDIDATES = [(600, 300), (1100, 300), (600, 560), (1100, 560), (100, 300), (100, 560)]

# ---- 「活码 vs 已过期占位卡」判据（2026-10-15 双样本标定）----
# 真二维码：暗像素(均值<80)占比 0.166；「二维码已过期」占位卡：0.007（相差 24 倍）。
# 阈值 0.05：比占位卡高 7 倍、比活码低 3.3 倍。
# 必须用 <80 而不是旧判据的 <110 —— 占位卡里红色「二维码已过期」文字均值约 100，
# 用 110 会把它误判成二维码。
DARK_PIXEL_MAX = 80
DUAL_QR_MIN_DARK = 0.05

# ---- 「下线通知」弹窗处置（2026-10-14 实测标定）----
# 账号被别的终端挤下线时，320x460 登录窗里显示的是「下线通知」弹窗而不是二维码。
# 旧逻辑不校验窗里是什么就截图返回，面板拿到 status=ok 却是一张扫不出来的图。
POPUP_OK_BTN = (237, 290)     # 弹窗「确定」按钮中心（窗口相对坐标）
QR_RATIO_MIN = 0.12           # 中心暗占比：真二维码 ~0.21 / 弹窗 ~0.07 / 一键登录页 ~0.00
ONECLICK_BLUE_MIN = 1500      # y=305..350 的「QQ 蓝」像素数阈值（一键登录页 ~10024）

x11  = ctypes.CDLL('libX11.so.6')
xtst = ctypes.CDLL('libXtst.so.6')
dpy  = x11.XOpenDisplay(b':1')

def sh(c):
    return subprocess.check_output(c, shell=True).decode()

def win_list():
    """-> [(wid_str, x, y, w, h)] top-level windows"""
    out = []
    for l in sh('xwininfo -root -children').splitlines():
        ls = l.split()
        if len(ls) > 2 and ls[0].startswith('0x') and '+0+0' not in l:
            try:
                g = [p for p in ls if p.startswith('0x') is False and 'x' in p and '+' in p][0]
                dims, xs, ys = g.split('+')
                w, h = map(int, dims.split('x'))
                out.append((ls[0], int(xs), int(ys), w, h))
            except Exception:
                pass
    return out

def pid_of(wid):
    """_NET_WM_PID of wid or its children"""
    try:
        o = sh(f'xprop -id {wid} _NET_WM_PID')
        if 'CARDINAL' in o:
            return int(o.strip().rsplit('=', 1)[1])
    except Exception:
        pass
    try:
        for l in sh(f'xwininfo -id {wid} -children').splitlines():
            ls = l.split()
            if len(ls) > 2 and ls[0].startswith('0x'):
                o = sh(f'xprop -id {ls[0]} _NET_WM_PID')
                if 'CARDINAL' in o:
                    return int(o.strip().rsplit('=', 1)[1])
    except Exception:
        pass
    return None

def home_of(pid):
    try:
        env = open(f'/proc/{pid}/environ', 'rb').read().split(b'\0')
        for kv in env:
            if kv.startswith(b'HOME='):
                return kv[5:].decode()
    except Exception:
        pass
    return ''

def login_wins():
    """-> {img_name: (wid, x, y)} for each existing 320x460 login window"""
    m = {}
    for wid, x, y, w, h in win_list():
        if w == 320 and h == 460:
            pid = pid_of(wid)
            img = HOME2IMG.get(home_of(pid) if pid else '')
            if img:
                m[img] = (wid, x, y)
    return m


def dual_login_wins():
    """-> {img_name: (wid, x, y, w, h)} 每个账号的 680x440 双栏登录窗。

    **刻意与 login_wins() 分开**：login_wins() 的调用方（一键登录 do_login /
    面板探针）都按 320x460 的窗口内坐标点击，若把 680x440 也混进去，点击坐标
    会全部错位。需要双栏窗的地方显式调本函数。
    """
    m = {}
    for wid, x, y, w, h in win_list():
        if w == DUAL_W and h == DUAL_H:
            pid = pid_of(wid)
            img = HOME2IMG.get(home_of(pid) if pid else '')
            if img:
                m[img] = (wid, x, y, w, h)
    return m

def wmap(wid):
    x11.XMapWindow(dpy, int(wid, 16)); x11.XFlush(dpy)

def move(wid, x, y):
    x11.XMoveWindow(dpy, int(wid, 16), x, y); x11.XFlush(dpy)

def raise_(wid):
    x11.XRaiseWindow(dpy, int(wid, 16)); x11.XFlush(dpy)

def abs_pos(wid):
    info = subprocess.check_output(['xwininfo', '-id', wid]).decode()
    x = int([l.split(':')[1] for l in info.splitlines() if 'Absolute upper-left X' in l][0])
    y = int([l.split(':')[1] for l in info.splitlines() if 'Absolute upper-left Y' in l][0])
    return x, y

def click_at(x, y):
    xtst.XTestFakeMotionEvent(dpy, -1, int(x), int(y), 0)
    x11.XFlush(dpy); time.sleep(0.3)
    xtst.XTestFakeButtonEvent(dpy, 1, True, 0);  x11.XFlush(dpy); time.sleep(0.15)
    xtst.XTestFakeButtonEvent(dpy, 1, False, 0); x11.XFlush(dpy); time.sleep(0.4)

def click_refresh(wid):
    """click the in-window refresh button at (160,280)"""
    x, y = abs_pos(wid)
    click_at(x + 160, y + 280)

def capture(wid, img):
    subprocess.run(f'xwd -id {wid} -silent -out /tmp/{img}.xwd', shell=True, check=True)
    subprocess.run(f'ffmpeg -y -v error -f xwd_pipe -i /tmp/{img}.xwd /tmp/qr-{img}.png',
                   shell=True, check=True)
    print('qr-' + img + '.png')

def capture_region(x, y, w, h, img):
    """抓根窗口后裁剪矩形。

    `xwd -id <win>` 不会带上子窗口，而 QQ 的登录卡片是主窗的**子窗口**，
    所以内嵌卡片形态必须走根窗口截图 + 裁剪。
    """
    subprocess.run('xwd -root -silent -out /tmp/_root.xwd', shell=True, check=True)
    subprocess.run(
        f'ffmpeg -y -v error -f xwd_pipe -i /tmp/_root.xwd '
        f'-vf "crop={w}:{h}:{x}:{y}" /tmp/qr-{img}.png',
        shell=True, check=True)
    print('qr-' + img + '.png')

def blank(img):
    p = f"/tmp/qr-{img}.png"
    return not os.path.exists(p) or os.path.getsize(p) < 8000


# ---- 双栏窗取码原语（2026-10-15 新增）----
def root_size():
    """屏幕尺寸（按实测取值，不硬编码）。"""
    try:
        o = sh('xwininfo -root')
        w = int([l.split(':')[1] for l in o.splitlines() if 'Width:' in l][0])
        h = int([l.split(':')[1] for l in o.splitlines() if 'Height:' in l][0])
        if w > 0 and h > 0:
            return w, h
    except Exception:                                       # noqa: BLE001
        pass
    return 1920, 1080


def capture_root_raw():
    """整屏 rgb24 原始像素（一次抓取，供内存判据 + 裁剪复用，不重复抓屏）。"""
    subprocess.run('xwd -root -silent -out /tmp/_rootraw.xwd', shell=True, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run('ffmpeg -y -v error -f xwd_pipe -i /tmp/_rootraw.xwd '
                   '-f rawvideo -pix_fmt rgb24 /tmp/_rootraw.raw',
                   shell=True, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with open('/tmp/_rootraw.raw', 'rb') as fh:
            return fh.read()
    except Exception:                                       # noqa: BLE001
        return b''


def dark_ratio_in(raw, rw, x, y, w, h, frac=0.72):
    """区域中心 frac 比例的「暗像素」占比（阈值见 DARK_PIXEL_MAX）；-1 = 数据不足。

    不依赖 320x460 卡片的固定坐标，所以双栏窗右栏卡片也能用同一判据。
    """
    if not raw or w <= 0 or h <= 0:
        return -1.0
    x0 = max(0, x + int(w * (1 - frac) / 2))
    x1 = min(x0 + int(w * frac), rw)
    y0 = max(0, y + int(h * (1 - frac) / 2))
    y1 = y0 + int(h * frac)
    dark = tot = 0
    for yy in range(y0, y1, 2):
        base = yy * rw * 3
        for xx in range(x0, x1, 2):
            o = base + xx * 3
            if o + 2 >= len(raw):
                continue
            if (raw[o] + raw[o + 1] + raw[o + 2]) / 3 < DARK_PIXEL_MAX:
                dark += 1
            tot += 1
    return (dark / tot) if tot else -1.0


def crop_raw_to_png(raw, rw, x, y, w, h, img):
    """从整屏 raw 裁出矩形并写 /tmp/qr-<img>.png（不再二次抓屏）。"""
    with open('/tmp/_crop.raw', 'wb') as fh:
        for yy in range(y, y + h):
            base = (yy * rw + x) * 3
            fh.write(raw[base:base + w * 3])
    subprocess.run('ffmpeg -y -v error -f rawvideo -pix_fmt rgb24 -s %dx%d '
                   '-i /tmp/_crop.raw /tmp/qr-%s.png' % (w, h, img),
                   shell=True, check=True)
    print('qr-' + img + '.png', flush=True)


def free_dual_pos(exclude_wid):
    """给 680x440 双栏窗挑一个完整落在屏内、且不与其它窗重叠的位置。"""
    rw, rh = root_size()
    try:
        wins = [w for w in win_list() if w[0] != exclude_wid]
    except Exception:                                       # noqa: BLE001
        wins = []
    for cx, cy in _DUAL_CANDIDATES:
        if cx + DUAL_W > rw or cy + DUAL_H > rh:
            continue
        if all(abs(x - cx) >= DUAL_W or abs(y - cy) >= DUAL_H
               for _w, x, y, _wd, _h in wins):
            return cx, cy
    return _DUAL_CANDIDATES[0]


def isolate_dual(wid):
    """把双栏窗挪到独占位置并置顶（isolate() 的 680x440 版）。"""
    cx, cy = free_dual_pos(wid)
    for _ in range(2):
        wmap(wid)
        move(wid, cx, cy)
        raise_(wid)
        time.sleep(0.6)
    return cx, cy

def acct_window(img):
    """账号自己的最大可见顶层窗口 (wid,x,y,w,h)，不限尺寸。"""
    best = None
    for wid, x, y, w, h in win_list():
        if w < 300 or h < 300:
            continue
        pid = pid_of(wid)
        if not pid or HOME2IMG.get(home_of(pid)) != img:
            continue
        if best is None or w * h > best[3] * best[4]:
            best = (wid, x, y, w, h)
    return best

# ---- 二维码存在性判据（2026-10-14 标定）----
# 真二维码在窗口中心区域（x70..250, y155..275）的暗像素占比约 0.07~0.21；
# 已登录的主界面/空白/弹窗 < 0.05。用它避免"把主窗口裁一块当二维码"的误报。
QR_BOX = (70, 250, 155, 275)
QR_DARK_MIN = 0.05


def _raw_region(x, y, w, h):
    """抓根窗口后裁剪出指定区域的 rgb24 原始像素。"""
    subprocess.run('xwd -root -silent -out /tmp/_qrq.xwd', shell=True, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(
        'ffmpeg -y -v error -f xwd_pipe -i /tmp/_qrq.xwd -vf "crop=%d:%d:%d:%d" '
        '-f rawvideo -pix_fmt rgb24 /tmp/_qrq.raw' % (w, h, x, y),
        shell=True, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with open('/tmp/_qrq.raw', 'rb') as fh:
            return fh.read()
    except Exception:                                   # noqa: BLE001
        return b''


def qr_dark_ratio(x, y, w=CARD_W, h=CARD_H):
    raw = _raw_region(x, y, w, h)
    if not raw or len(raw) < w * h * 3:
        return -1.0
    x0, x1, y0, y1 = QR_BOX
    dark = tot = 0
    for yy in range(y0, min(y1, h), 2):
        base = yy * w * 3
        for xx in range(x0, min(x1, w), 2):
            o = base + xx * 3
            if (raw[o] + raw[o + 1] + raw[o + 2]) / 3 < 110:
                dark += 1
            tot += 1
    return dark / max(1, tot)


def _blue_band_raw(raw, w=CARD_W, h=CARD_H):
    """y=305..350 区间内的「QQ 蓝」像素数（判定一键登录界面）。"""
    if not raw or len(raw) < w * h * 3:
        return -1
    n = 0
    for y in range(305, min(350, h)):
        base = y * w * 3
        for x in range(w):
            o = base + x * 3
            r, g, b = raw[o], raw[o + 1], raw[o + 2]
            if b > 180 and r < 150 and g < 200:
                n += 1
    return n


def classify_region(x, y):
    """-> (kind, ratio, blue)；kind ∈ 'qr' | 'oneclick' | 'other'。

    这是「窗里到底能不能扫」的唯一判据，供 run() 决定是否把图当二维码返回。
    """
    ratio = qr_dark_ratio(x, y)
    blue = _blue_band_raw(_raw_region(x, y, CARD_W, CARD_H))
    if blue >= ONECLICK_BLUE_MIN:
        return 'oneclick', ratio, blue
    if ratio >= QR_RATIO_MIN:
        return 'qr', ratio, blue
    return 'other', ratio, blue


# ---- 截图位置隔离（2026-10-14 修复）----
# 所有账号的登录窗都被摆到同一个位置（HOME_POS）。51 个窗互相重叠时，
# `capture_region` 抓的是**该坐标上最上层那个窗**——如果 raise 没真正改变层序，
# 就会把**别的账号**的登录窗截给用户（用户会扫到不属于自己的二维码、把大号
# 登进别人的容器）。因此截图前先把目标窗挪到一个**没有其它窗占用**的位置。
HOME_POS = (800, 300)
_CANDIDATES = [(1400, 560), (1400, 100), (100, 560), (100, 100),
               (560, 560), (1700, 560), (560, 100), (1700, 100)]


def free_capture_pos(exclude_wid):
    """挑一个与其它 320x460 登录窗不重叠的位置（找不到就退回第一个候选）。"""
    try:
        wins = [w for w in win_list() if w[0] != exclude_wid]
    except Exception:                                       # noqa: BLE001
        wins = []
    for cx, cy in _CANDIDATES:
        if all(abs(x - cx) >= CARD_W or abs(y - cy) >= CARD_H for _w, x, y, _wd, _h in wins):
            return cx, cy
    return _CANDIDATES[0]


def isolate(wid):
    """把窗口挪到独占位置并置顶，返回该位置。"""
    cx, cy = free_capture_pos(wid)
    for _ in range(2):
        wmap(wid)
        move(wid, cx, cy)
        raise_(wid)
        time.sleep(0.6)
    return cx, cy


def restore(wid):
    try:
        move(wid, HOME_POS[0], HOME_POS[1])
    except Exception:                                       # noqa: BLE001
        pass


def try_centred_card(img):
    """内嵌卡片形态：把账号主窗居中，点卡片内「刷新」，裁剪居中 320x460 卡片。

    成功返回 True。绝不重启任何 supervisor 程序。
    """
    aw = acct_window(img)
    if not aw:
        return False
    wid, x, y, w, h = aw
    print(f'{img}: no 320x460 window; trying centred login card in {w}x{h} window', flush=True)
    try:
        wmap(wid)
        raise_(wid)
        wx0, wy0 = abs_pos(wid)
        # 只在窗口没完整落在屏幕上时才移动，避免对已登录账号做无谓改动
        if wx0 < 0 or wy0 < 0 or wx0 + w > 1920 or wy0 + h > 1080:
            move(wid, max(0, (1920 - w) // 2), max(0, (1080 - h) // 2))
    except Exception as exc:                      # noqa: BLE001
        print(f'{img}: window adjust warn: {exc}', flush=True)
    time.sleep(1.0)
    for attempt in range(3):
        wx, wy = abs_pos(wid)
        cx = wx + (w - CARD_W) // 2
        cy = wy + (h - CARD_H) // 2
        click_at(cx + CARD_REFRESH[0], cy + CARD_REFRESH[1])
        time.sleep(2.0)
        ratio = qr_dark_ratio(cx, cy)
        capture_region(cx, cy, CARD_W, CARD_H, img)
        # 既要有内容，也**必须真的像二维码**——否则说明该号已登录/是主界面，
        # 返回假图会让用户以为"刷不出二维码"。此时直接放弃（面板会报 unavailable）。
        if not blank(img) and ratio >= QR_DARK_MIN:
            print(f'{img}: centred card accepted (qr_ratio={ratio:.3f})', flush=True)
            return True
        print(f'{img}: centred card rejected (qr_ratio={ratio:.3f}, '
              f'size={os.path.getsize("/tmp/qr-%s.png" % img) if os.path.exists("/tmp/qr-%s.png" % img) else 0})',
              flush=True)
        try:
            raise_(wid)
        except Exception:
            pass
        time.sleep(1.5)
    return False

def extract_dual(img, wid):
    """双栏登录窗取码：点右栏「刷新」→ 裁剪二维码卡片。成功返回 True。

    判据 = 卡片中心 72% 区域的暗像素占比（标定见 DUAL_QR_MIN_DARK）：
    「二维码已过期」占位卡 0.007 / 真二维码 0.166，阈值 0.05 —— 绝不返回占位图。
    """
    isolate_dual(wid)
    time.sleep(0.8)
    rw, rh = root_size()
    ratio = -1.0
    for attempt in range(3):
        wx, wy = abs_pos(wid)
        click_at(wx + DUAL_REFRESH[0], wy + DUAL_REFRESH[1])
        time.sleep(5.0)
        wx, wy = abs_pos(wid)
        raw = capture_root_raw()
        qx, qy = wx + DUAL_QR_CARD[0], wy + DUAL_QR_CARD[1]
        qw, qh = DUAL_QR_CARD[2], DUAL_QR_CARD[3]
        ratio = dark_ratio_in(raw, rw, qx, qy, qw, qh)
        print(f'{img}: dual-column card dark_ratio={ratio:.3f} (attempt {attempt + 1})',
              flush=True)
        if ratio >= DUAL_QR_MIN_DARK:
            crop_raw_to_png(raw, rw, qx, qy, qw, qh, img)
            restore(wid)
            return True
        wmap(wid); raise_(wid)
        time.sleep(1.5)
    restore(wid)
    print(f'{img}: dual-column window right column has no QR (dark_ratio={ratio:.3f})',
          file=sys.stderr, flush=True)
    return False


def spawn_fresh(prog, img):
    """restart prog, wait for its new login window (320x460 或 680x440 双栏)"""
    before = set(w for w, *_ in win_list())
    subprocess.run(['supervisorctl', 'restart', prog], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        time.sleep(1)
        nw = [(w, x, y, wd, h) for w, x, y, wd, h in win_list()
              if w not in before and (wd, h) in ((CARD_W, CARD_H), (DUAL_W, DUAL_H))]
        if nw:
            # confirm it belongs to this account
            for wid, x, y, wd, h in nw:
                pid = pid_of(wid)
                if HOME2IMG.get(home_of(pid) if pid else '') == img or len(nw) == 1:
                    return wid, x, y, wd, h
            return nw[0]
    return None

def run(prog, force_restart):
    img, home = ACCOUNTS[prog]
    win = None if force_restart else login_wins().get(img)
    if win:
        wid, x, y = win
        # 截图位置隔离（见 free_capture_pos 注释）：否则会把别人的登录窗截给用户
        isolate(wid)
        time.sleep(0.5)
        click_refresh(wid)
        time.sleep(6)
        # 注意：QQ 登录窗的内容在子窗口里，且窗口无 backing store——
        # 直接 `xwd -id <win>` 对被遮挡/子窗渲染的情况会返回**全白空图**。
        # 因此改为：raise 之后从**根窗口**截图再按窗口矩形裁剪（根窗口永远有有效内容）。
        #
        # 2026-10-14 修复：**必须校验窗里到底是不是二维码**。
        # 被踢下线的账号，这个 320x460 窗里是「下线通知」弹窗；旧逻辑直接截图返回，
        # 面板 status=ok 却给用户一张扫不出来的图。这里先分类，不是二维码就先点弹窗的
        # 「确定」把它关掉（关掉后窗口会回到一键登录 / 二维码界面）；实在拿不到可用界面
        # 就 exit(1)，让面板如实报 snowluma_qr_unavailable，绝不返回假图。
        for _ in range(3):
            wx, wy = abs_pos(wid)
            kind, ratio, blue = classify_region(wx, wy)
            if kind in ('qr', 'oneclick'):
                capture_region(wx, wy, CARD_W, CARD_H, img)
                if not blank(img):
                    print(f'{img}: kind={kind} ratio={ratio:.3f} blue={blue}', flush=True)
                    restore(wid)
                    return
                print(f'{img}: blank capture, retrying', flush=True)
            else:
                print(f'{img}: kind={kind} ratio={ratio:.3f} blue={blue} -> dismiss popup', flush=True)
                click_at(wx + POPUP_OK_BTN[0], wy + POPUP_OK_BTN[1])
                time.sleep(2.5)
                wx, wy = abs_pos(wid)
                kind, ratio, blue = classify_region(wx, wy)
                if kind in ('qr', 'oneclick'):
                    capture_region(wx, wy, CARD_W, CARD_H, img)
                    if not blank(img):
                        print(f'{img}: recovered kind={kind} ratio={ratio:.3f} blue={blue}', flush=True)
                        restore(wid)
                        return
            wmap(wid); raise_(wid)
            click_refresh(wid)
            time.sleep(6.0)
        restore(wid)
        print(f'{img}: no scannable login surface (popup/blank)', file=sys.stderr, flush=True)
        sys.exit(1)

    # ---- 680x440 双栏登录窗：从右栏二维码卡片取码（2026-10-15 新增）----
    # DFN 卡在「二维码暂不可用（登录窗口未就绪）」就是这个形态：窗口是双栏的，
    # login_wins() 只认 320x460 → 判成「没有登录窗」→ 走 spawn_fresh 兜底 →
    # 而面板 strict 包装把 spawn_fresh 阉掉（绝不重启）→ exit 1 → 前端只能报未就绪。
    # 这里直接从右栏卡片取：点「刷新」→ 整屏抓一次 → 内存判据（暗像素占比，
    # 标定见 DUAL_QR_MIN_DARK）→ 裁剪卡片。拿不到真码就 exit(1) 如实报不可用，
    # 绝不把「二维码已过期」占位卡当码返回。
    dwin = dual_login_wins().get(img)
    if dwin:
        wid, x, y, w, h = dwin
        print(f'{img}: dual-column {w}x{h} login window found', flush=True)
        if extract_dual(img, wid):
            return
        sys.exit(1)

    # 2026-10-14 决策：**取消"居中卡片"兜底**。
    # 原因：账号已登录时同样没有 320x460 登录窗，兜底会把主界面裁一块当二维码返回，
    # 用户看到的是"刷不出二维码"的假图；而像素上无法可靠区分"主界面"与"二维码"
    # （实测暗占比两者重叠）。因此没有真登录窗时**如实返回不可用**，
    # 由面板提示 + 用户点「一键登录」（该脚本会重启程序逼出全新登录窗）来恢复。

    print(f'{img}: no existing login window, restarting {prog}', flush=True)
    win = spawn_fresh(prog, img)
    if not win:
        print(f'{img}: window not found after restart', file=sys.stderr)
        sys.exit(1)
    wid, x, y, wd, h = win
    if (wd, h) == (DUAL_W, DUAL_H):
        print(f'{img}: fresh window is dual-column {wd}x{h}', flush=True)
        extract_dual(img, wid)
        return
    wmap(wid); move(wid, 800, 300); raise_(wid)
    time.sleep(1)
    click_refresh(wid)
    time.sleep(2)
    capture(wid, img)
    for _ in range(3):
        if not blank(img):
            break
        print(f"{img}: blank capture, retrying", flush=True)
        wmap(wid); raise_(wid); time.sleep(1.5)
        capture(wid, img)

if __name__ == '__main__':
    run(sys.argv[1], '--restart' in sys.argv[2:])
