import os
import sys
import fcntl
import time
import logging
from collections import deque, namedtuple
from threading import Thread
from subprocess import Popen, PIPE
from select import select
from tempfile import NamedTemporaryFile
import hashlib

import bottle
bottle.debug(True)

from bottle import route, run, redirect, request, abort, get, view
import Cookie
import json
import cStringIO

logging.basicConfig(filename = "/tmp/thunderbatch.log",
        format = "%(asctime)s %(threadName)s(%(thread)s):%(name)s:%(message)s",
                            level = logging.DEBUG)

DEFAULT_DOWN_DIR = os.path.expanduser("~/Downloads")

if not os.path.isdir(DEFAULT_DOWN_DIR):
    DEFAULT_DOWN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Downloads")
    if not os.path.exists(DEFAULT_DOWN_DIR):
        os.mkdir(DEFAULT_DOWN_DIR)


logger = logging.getLogger()

THREAD_OBJ = namedtuple('Point', ['uid', 'tasktype', 'filename', 'dl_url', 'gdriveid', 'cookies_file', 'dl_thread'])

def LogException(func):
    def __check(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except:
            logger.debug("Exception", exc_info = True)
            raise
    return __check


class DownloadThread(Thread):

    def __init__(self, cmd_args, cwd = None):
        super(DownloadThread, self).__init__()
        self.logger = logging.getLogger(type(self).__name__)
        self.daemon = True
        self.cmd_args = cmd_args
        self.cwd = cwd
        self.deque = deque(maxlen = 2048)
        self.retcode = None

    def run(self):
        logger = self.logger
        logger.debug("init")
        p = Popen(self.cmd_args, bufsize = 4096, cwd = self.cwd, stdout=PIPE, stderr=PIPE, close_fds=True)
        out, err= p.stdout, p.stderr
        wait_list = [out, err]
         
        #async
        fcntl.fcntl(out, fcntl.F_SETFL, os.O_NONBLOCK)
        fcntl.fcntl(err, fcntl.F_SETFL, os.O_NONBLOCK)

        logger.debug("run cmd: " + str(self.cmd_args))

        while True:
            try:
                rlist, wl, el = select(wait_list, [], [], 8)
                for fd in rlist:
                    o = fd.read()
                    self.deque.append(o)

            except IOError, e:
                logger.debug("IOError" + str(e))
                continue

            if p.poll() is not None:
                ret = self.retcode = p.returncode
                last_log = ''

                for pipe in wait_list:
                    o = pipe.read()
                    if len(o) > 0:
                        self.deque.append(o)
                        last_log += o
                    pipe.close()

                logger.debug("wget ended. exit code: '%d'" % ret)
                if ret != 0:
                    logger.debug("wget lastlog: " + last_log)

                break

    pop = lambda s:s.deque.popleft()

    @property
    def status(self):
        status = ''
        if self.is_alive():
            status = "Running"
        else:
            if self.retcode == 0:
                status = "Done"
            elif self.retcode in (3,8):
                status = "HD Error"
            else:
                status = "Error"

        return status

class ThunderTaskManager(object):

    def __init__(self):
        self.logger = logging.getLogger(type(self).__name__)
        self.cookies_pool = {}
        self.thread_pool = {}

    def make_cookies_file(self, tasktype, cookies_values):
        cookie_line = {"thunder":".vip.xunlei.com\tTRUE\t/\tFALSE\t0\t%s\t%s\n",
                        "qq":".qq.com\tTRUE\t/\tFALSE\t0\t%s\t%s\n"}

        key = hashlib.md5(tasktype + "".join(map(lambda l:l[1], cookies_values))).hexdigest()

        if key not in self.cookies_pool:

            tmp_file = NamedTemporaryFile(suffix='.txt', delete=False)
            for k, v in cookies_values:
                line = cookie_line[tasktype] % (k, v)
                tmp_file.write(line)
            tmp_file.close()
            self.cookies_pool[key] = tmp_file.name

        return self.cookies_pool[key]

    def new_thunder_task(self, tasktype, filename, dl_url, cookies_values):
        log = self.logger
        cookies_file = self.make_cookies_file(tasktype, cookies_values)
        wget_cmd = ['/usr/bin/wget', '--continue', '-O', filename, 
                '--progress=dot', '--load-cookies', cookies_file,  dl_url]

        log.debug("cmd shell: " + str(wget_cmd))

        dl_thread = DownloadThread(wget_cmd, DEFAULT_DOWN_DIR)
        dl_thread.start()

        uid = str(time.time()).replace('.', '')

        self.thread_pool[uid] = (THREAD_OBJ(uid, tasktype, filename, dl_url, cookies_values, cookies_file, dl_thread))

        log.debug("thread id :" + str(dl_thread.ident))
        return uid

    def list_all_tasks(self):
        p = self.thread_pool
        self.logger.debug(self.thread_pool.keys())
        return map(lambda k:dict(uid = p[k].uid, 
            tasktype = p[k].tasktype,
            filename = p[k].filename, 
            status = p[k].dl_thread.status), 
            sorted(self.thread_pool.keys()))


@route("/thunder_single_task")
@LogException
def thunder_single_task():
    filename = request.GET.get("name")
    dl_url = request.GET.get("url")
    cookies_str = request.GET.get("cookies")
    cookie = Cookie.BaseCookie(cookies_str)
    gdriveid = cookie["gdriveid"].value
    tid = task_mgr.new_thunder_task("thunder", filename, dl_url, [("gdriveid", gdriveid)])
    return dict(tid = tid)

@route("/qq_single_task")
@LogException
def qq_single_task():
    filename = request.GET.get("name")
    dl_url = request.GET.get("url")
    cookies_str = request.GET.get("cookies")
    cookie = Cookie.BaseCookie(cookies_str)
    tid = task_mgr.new_thunder_task("qq", filename, dl_url, 
            [("FTN5K", cookie["FTN5K"].value)])
    return dict(tid = tid)

@route("/list_all_tasks")
@LogException
def list_all_tasks():
    return dict(tasks = task_mgr.list_all_tasks())


@route("/query_task_log/:tid")
@LogException
def query_task_log(tid = None):
    assert tid is not None, "need tid"
    thread = task_mgr.thread_pool[tid]
    output = cStringIO.StringIO()

    while True:
        try:
            line = thread.dl_thread.pop()
            output.write(line)
        except IndexError:
            break
   
    line = output.getvalue()
    output.close()

    return dict(status = thread.dl_thread.status, line = line)

@route("/")
@view('mointor')
def root():
    return {}


if __name__ == "__main__":

    print "Default Download Dir: '%s'" % DEFAULT_DOWN_DIR

    task_mgr = ThunderTaskManager()

    import webbrowser
    webbrowser.open_new_tab("http://127.0.0.1:8080")
    run(host='0.0.0.0', port=8080)


import unittest

class test_wget(unittest.TestCase):
    def setUp(self):

        import shlex
        cmd = 'wget -O /dev/null --progress=dot http://ftp.tw.debian.org/debian/ls-lR.gz'
        cmd = 'wget -O /dev/null --progress=dot http://ftp.tw.debian.org/debian/ls-lR.patch.gz'

        self.shell_cmd = shlex.split(cmd)
        print self.shell_cmd

    def test_wget(self):
        t = DownloadThread(self.shell_cmd)
        t.start()

        while True:
            try:
                c, o = t.pop()
                print c, o

            except IndexError:
                if not t.is_alive():
                    break

                time.sleep(.1)


    def test_notget(self):

        t = DownloadThread(self.shell_cmd)
        t.start()
        t.join()

    def test_taskmgr(self):

        filename = "13. Hiding My Heart.mp3"
        dl_url = "http://gdl.lixian.vip.xunlei.com/download?fid=qyh37P2CIFsIwt/RgbLulXGzJo27TH8AAAAAAHLX9VaTzNJL1DkuNS5iEzxbyGLZ&mid=666&threshold=150&tid=F4F3C6EE85C70547FDA4D027E0E895D5&srcid=4&verno=1&g=72D7F55693CCD24BD4392E352E62133C5BC862D9&scn=t7&i=71F8AE377D07F36D04350171D3BB8FD33E162150&t=6&ui=169602995&ti=42518759750&s=8342715&m=0&n=015002CA7F486964690F56C41279204865004390716D703300"
        gdriveid = "7617B8D05D955EA55C05EF3908D8162F"

        m = ThunderTaskManager()
        tid = m.new_thunder_task(filename, dl_url, gdriveid)
        t = m.thread_pool[tid].dl_thread

        while True:
            try:
                c, o = t.pop()
                if c == "OUT":
                    sys.stdout.write(o)
                else:
                    print c, o

            except IndexError:
                if not t.is_alive():
                    break

                time.sleep(.1)




