#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vodctl —— 18080/18766 两个后端的启停控制（唯一实现，run.sh / start-all.sh 都只是包装）。

为什么要它：两个服务的主程序都叫 server.py，`pkill -f server.py` 会一起杀掉，
也没法只重启一个。这里按「进程 cwd == 该服务的目录」认领（旧实例没写 pidfile 也能认出来）。

  python3 vodctl.py status
  python3 vodctl.py start    [18080|18766|all]
  python3 vodctl.py stop     [18080|18766|all]
  python3 vodctl.py restart  [18080|18766|all]
  python3 vodctl.py ensure   [18080|18766|all]    # 只拉起没在跑的那个（给 cron 用）
"""
import os, signal, socket, subprocess, sys, time, fcntl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, 'backend', 'run')
SERVICES = {
    18080: {'dir': os.path.join(ROOT, 'backend'), 'log': '/tmp/huanyuan.log', 'name': '主后端'},
    18766: {'dir': os.path.join(ROOT, 'frontend', 'original-vod-prototype'), 'log': '/tmp/keda18766.log', 'name': '科达 SPA 宿主'},
}


def _pidfile(port):
    return os.path.join(RUN, '%d.pid' % port)


def port_open(port, host='127.0.0.1', timeout=0.4):
    s = socket.socket()
    try:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def find_pids(port):
    """先看 pidfile，再按 cwd 认领（覆盖没写 pidfile 的旧实例）。"""
    pids = []
    pf = _pidfile(port)
    if os.path.isfile(pf):
        try:
            pid = int(open(pf).read().strip())
            if _alive(pid) and _is_ours(pid, port):
                pids.append(pid)
        except (ValueError, OSError):
            pass
    want = os.path.realpath(SERVICES[port]['dir'])
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid() or pid in pids or not _alive(pid):
            continue
        try:
            cmd = open('/proc/%d/cmdline' % pid, 'rb').read().split(b'\0')
            if not any(c.endswith(b'server.py') for c in cmd if c):
                continue
            if os.path.realpath('/proc/%d/cwd' % pid) == want:
                pids.append(pid)
        except OSError:
            continue
    return pids


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_ours(pid, port):
    try:
        return os.path.realpath('/proc/%d/cwd' % pid) == os.path.realpath(SERVICES[port]['dir'])
    except OSError:
        return False


def start(port, wait=3.0):
    svc = SERVICES[port]
    if port_open(port):
        print('  %d 已在监听，跳过启动' % port)
        return True
    if not os.path.isdir(RUN):
        os.makedirs(RUN)
    log = open(svc['log'], 'ab', 0)
    log.write(('\n=== %s vodctl start %d (v via /api/version) ===\n' % (time.strftime('%F %T'), port)).encode())
    env = dict(os.environ, VOD_PORT=str(port))
    proc = subprocess.Popen(['python3', '-u', 'server.py'], cwd=svc['dir'], env=env,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            start_new_session=True, close_fds=True)
    open(_pidfile(port), 'w').write('%d\n' % proc.pid)
    deadline = time.time() + wait
    while time.time() < deadline:
        if port_open(port):
            print('  %d 启动 OK (pid %d, %s)' % (port, proc.pid, svc['log']))
            return True
        if proc.poll() is not None:
            print('  %d 启动失败（进程已退出，看 %s）' % (port, svc['log']))
            return False
        time.sleep(0.2)
    print('  %d 启动后 %gs 内未监听（仍在跑？pid %d）' % (port, wait, proc.pid))
    return port_open(port)


def stop(port, grace=4.0):
    pids = find_pids(port)
    if not pids:
        print('  %d 没有在跑的实例' % port)
        _cleanup_pidfile(port)
        return True
    for pid in pids:
        try: os.kill(pid, signal.SIGTERM)
        except OSError: pass
    deadline = time.time() + grace
    while time.time() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.15)
    for pid in pids:
        if _alive(pid):
            try: os.kill(pid, signal.SIGKILL)
            except OSError: pass
    _cleanup_pidfile(port)
    print('  %d 已停止 (pid %s)' % (port, ','.join(map(str, pids))))
    return True


def _cleanup_pidfile(port):
    try: os.unlink(_pidfile(port))
    except OSError: pass


def status():
    ok = True
    for port in sorted(SERVICES):
        svc = SERVICES[port]
        pids = find_pids(port)
        up = port_open(port)
        ok = ok and up
        print('%d %-8s %-14s pid=%-12s listen=%s' % (
            port, 'OK' if up else 'FAIL', svc['name'],
            ','.join(map(str, pids)) or '-', 'yes' if up else 'no'))
    return ok


def targets(arg):
    if arg in (None, 'all'):
        return sorted(SERVICES)
    port = int(arg)
    if port not in SERVICES:
        raise SystemExit('未知端口 %s（可选 18080 / 18766 / all）' % arg)
    return [port]


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    arg = sys.argv[2] if len(sys.argv) > 2 else 'all'
    if cmd == 'status':
        sys.exit(0 if status() else 1)
    if cmd in ('start', 'stop', 'restart'):
        for port in targets(arg):
            print('==> %s %d' % (cmd, port))
            if cmd == 'stop':
                stop(port)
            elif cmd == 'start':
                start(port)
            else:
                stop(port); start(port)
        print('==> 状态:')
        status()
        return
    if cmd == 'ensure':
        # cron 用：只在没监听时拉起，重复执行无副作用
        lock = open(os.path.join(RUN, 'ensure.lock'), 'w') if os.path.isdir(RUN) else None
        if lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError: return          # 上一轮还在跑
        for port in targets(arg):
            if port_open(port):
                continue
            print('%s 拉起 %d' % (time.strftime('%F %T'), port))
            start(port)
        return
    raise SystemExit(__doc__)


if __name__ == '__main__':
    main()
