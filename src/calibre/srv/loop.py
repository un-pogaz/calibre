#!/usr/bin/env python


__license__ = 'GPL v3'
__copyright__ = '2015, Kovid Goyal <kovid at kovidgoyal.net>'

import ipaddress
import os
import select
import socket
import ssl
import traceback
from contextlib import suppress
from functools import lru_cache, partial
from io import BytesIO

from calibre import as_unicode
from calibre.constants import iswindows
from calibre.ptempfile import TemporaryDirectory
from calibre.srv.errors import JobQueueFull
from calibre.srv.jobs import JobsManager
from calibre.srv.opts import Options
from calibre.srv.pool import PluginPool, ThreadPool
from calibre.srv.utils import (
    DESIRED_SEND_BUFFER_SIZE,
    HandleInterrupt,
    create_sock_pair,
    socket_errors_eintr,
    socket_errors_nonblocking,
    socket_errors_socket_closed,
    start_cork,
    stop_cork,
)
from calibre.utils.localization import _
from calibre.utils.logging import ThreadSafeLog
from calibre.utils.mdns import get_external_ip
from calibre.utils.monotonic import monotonic
from calibre.utils.network import get_fallback_server_addr
from calibre.utils.socket_inheritance import set_socket_inherit
from polyglot.builtins import iteritems
from polyglot.queue import Empty, Full

READ, WRITE, RDWR, WAIT = 'READ', 'WRITE', 'RDWR', 'WAIT'
WAKEUP, JOB_DONE = b'\0', b'\x01'
IPPROTO_IPV6 = getattr(socket, 'IPPROTO_IPV6', 41)


class ReadBuffer:  # {{{

    ' A ring buffer used to speed up the readline() implementation by minimizing recv() calls '

    __slots__ = ('ba', 'buf', 'full_state', 'read_pos', 'write_pos')

    def __init__(self, size=4096):
        self.ba = bytearray(size)
        self.buf = memoryview(self.ba)
        self.read_pos = 0
        self.write_pos = 0
        self.full_state = WRITE

    @property
    def has_data(self):
        return self.read_pos != self.write_pos or self.full_state is READ

    @property
    def has_space(self):
        return self.read_pos != self.write_pos or self.full_state is WRITE

    def read(self, size):
        # Read from this buffer, retuning the read bytes as a bytestring
        if self.read_pos == self.write_pos and self.full_state is WRITE:
            return b''
        if self.read_pos < self.write_pos:
            sz = min(self.write_pos - self.read_pos, size)
            npos = self.read_pos + sz
            ans = self.buf[self.read_pos:npos].tobytes()
            self.read_pos = npos
            if self.read_pos == self.write_pos:
                self.full_state = WRITE
        else:
            sz = min(size, len(self.buf) - self.read_pos)
            ans = self.buf[self.read_pos:self.read_pos + sz].tobytes()
            self.read_pos = (self.read_pos + sz) % len(self.buf)
            if self.read_pos == self.write_pos:
                self.full_state = WRITE
            if size > sz and self.read_pos < self.write_pos:
                ans += self.read(size - len(ans))
        return ans

    def recv_from(self, socket):
        # Write into this buffer from socket, return number of bytes written
        if self.read_pos == self.write_pos and self.full_state is READ:
            return 0
        if self.write_pos < self.read_pos:
            num = socket.recv_into(self.buf[self.write_pos:self.read_pos])
            self.write_pos += num
        else:
            num = socket.recv_into(self.buf[self.write_pos:])
            self.write_pos = (self.write_pos + num) % len(self.buf)
        if self.write_pos == self.read_pos:
            self.full_state = READ
        return num

    def readline(self):
        # Return whatever is in the buffer up to (and including) the first \n
        # If no \n is present, returns everything
        if self.read_pos == self.write_pos and self.full_state is WRITE:
            return b''
        if self.read_pos < self.write_pos:
            pos = self.ba.find(b'\n', self.read_pos, self.write_pos)
            if pos < 0:
                pos = self.write_pos - 1
            ans = self.buf[self.read_pos:pos + 1].tobytes()
            self.read_pos = (pos + 1) % len(self.buf)
            if self.read_pos == self.write_pos:
                self.full_state = WRITE
        else:
            pos = self.ba.find(b'\n', self.read_pos)
            if pos < 0:
                pos = self.ba.find(b'\n', 0, self.write_pos)
                if pos < 0:
                    pos = self.write_pos - 1
                ans = self.buf[self.read_pos:].tobytes() + self.buf[:pos+1].tobytes()
                self.read_pos = (pos + 1) % len(self.buf)
                if self.read_pos == self.write_pos:
                    self.full_state = WRITE
            else:
                ans = self.buf[self.read_pos:pos + 1].tobytes()
                self.read_pos = (pos + 1) % len(self.buf)
                if self.read_pos == self.write_pos:
                    self.full_state = WRITE
        return ans
    # }}}


class BadIPSpec(ValueError):
    pass


def parse_trusted_ips(spec):
    for part in as_unicode(spec).split(','):
        part = part.strip()
        try:
            if '/' in part:
                yield ipaddress.ip_network(part)
            else:
                yield ipaddress.ip_address(part)
        except Exception as e:
            raise BadIPSpec(_('{0} is not a valid IP address/network, with error: {1}').format(part, e))


def is_ip_trusted(remote_addr, trusted_ips):
    remote_addr = getattr(remote_addr, 'ipv4_mapped', None) or remote_addr
    for tip in trusted_ips:
        if hasattr(tip, 'hosts'):
            if remote_addr in tip:
                return True
        else:
            if tip == remote_addr:
                return True
    return False


def is_local_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address | None):
    if addr is None:
        return False
    if addr.is_loopback:
        return True
    ipv4_mapped = getattr(addr, 'ipv4_mapped', None)
    return getattr(ipv4_mapped, 'is_loopback', False)


class Connection:  # {{{

    def __init__(self, socket, opts, ssl_context, tdir, addr, pool, log, access_log, wakeup):
        self.opts, self.pool, self.log, self.wakeup, self.access_log = opts, pool, log, wakeup, access_log
        try:
            self.remote_addr = addr[0]
            self.remote_port = addr[1]
            self.parsed_remote_addr = ipaddress.ip_address(as_unicode(self.remote_addr))
        except Exception:
            # In case addr is None, which can occasionally happen
            self.remote_addr = self.remote_port = self.parsed_remote_addr = None
        self.is_trusted_ip = bool(self.opts.local_write and is_local_address(self.parsed_remote_addr))
        if not self.is_trusted_ip and self.opts.trusted_ips and self.parsed_remote_addr is not None:
            self.is_trusted_ip = is_ip_trusted(self.parsed_remote_addr, parsed_trusted_ips(self.opts.trusted_ips))
        self.orig_send_bufsize = self.send_bufsize = 4096
        self.tdir = tdir
        self.wait_for = READ
        self.response_started = False
        self.read_buffer = ReadBuffer()
        self.handle_event = None
        self.ssl_context = ssl_context
        self.ssl_handshake_done = False
        self.ssl_terminated = False
        if self.ssl_context is not None:
            self.ready = False
            self.socket = self.ssl_context.wrap_socket(socket, server_side=True, do_handshake_on_connect=False)
            self.set_state(RDWR, self.do_ssl_handshake)
        else:
            self.socket = socket
            self.connection_ready()
        self.last_activity = monotonic()
        self.ready = True

    def optimize_for_sending_packet(self):
        start_cork(self.socket)
        self.orig_send_bufsize = self.send_bufsize = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
        if self.send_bufsize < DESIRED_SEND_BUFFER_SIZE:
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, DESIRED_SEND_BUFFER_SIZE)
            except OSError:
                pass
            else:
                self.send_bufsize = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)

    def end_send_optimization(self):
        stop_cork(self.socket)
        if self.send_bufsize != self.orig_send_bufsize:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.orig_send_bufsize)

    def set_state(self, wait_for, func, *args, **kwargs):
        self.wait_for = wait_for
        if args or kwargs:
            pfunc = partial(func, *args, **kwargs)
            pfunc.__name__ = func.__name__
            func = pfunc
        self.handle_event = func

    def do_ssl_handshake(self, event):
        try:
            self.socket._sslobj.do_handshake()
        except ssl.SSLWantReadError:
            self.set_state(READ, self.do_ssl_handshake)
        except ssl.SSLWantWriteError:
            self.set_state(WRITE, self.do_ssl_handshake)
        else:
            self.ssl_handshake_done = True
            self.connection_ready()

    def send(self, data):
        try:
            ret = self.socket.send(data) if self.ssl_context is None else self.socket.write(data)
            self.last_activity = monotonic()
            return ret
        except ssl.SSLWantWriteError:
            return 0
        except OSError as e:
            if e.errno in socket_errors_nonblocking or e.errno in socket_errors_eintr:
                return 0
            elif e.errno in socket_errors_socket_closed:
                self.log.error('Failed to send all data in state:', self.state_description, 'with error:', e)
                self.ready = False
                return 0
            raise

    def recv(self, amt):
        # If there is data in the read buffer we have to return only that,
        # since we don't know if the socket has signalled it is ready for
        # reading
        if self.read_buffer.has_data:
            return self.read_buffer.read(amt)
        # read buffer is empty, so read directly from socket
        try:
            data = self.socket.recv(amt)
            self.last_activity = monotonic()
            if not data:
                # a closed connection is indicated by signaling
                # a read condition, and having recv() return 0.
                self.ready = False
                return b''
            return data
        except ssl.SSLWantReadError:
            return b''
        except OSError as e:
            if e.errno in socket_errors_nonblocking or e.errno in socket_errors_eintr:
                return b''
            if e.errno in socket_errors_socket_closed:
                self.ready = False
                return b''
            raise

    def recv_into(self, buf, amt=0):
        amt = amt or len(buf)
        if self.read_buffer.has_data:
            data = self.read_buffer.read(amt)
            buf[0:len(data)] = data
            return len(data)
        try:
            bytes_read = self.socket.recv_into(buf, amt)
            self.last_activity = monotonic()
            if bytes_read == 0:
                # a closed connection is indicated by signaling
                # a read condition, and having recv() return 0.
                self.ready = False
                return 0
            return bytes_read
        except ssl.SSLWantReadError:
            return 0
        except OSError as e:
            if e.errno in socket_errors_nonblocking or e.errno in socket_errors_eintr:
                return 0
            if e.errno in socket_errors_socket_closed:
                self.ready = False
                return 0
            raise

    def fill_read_buffer(self):
        try:
            num = self.read_buffer.recv_from(self.socket)
            self.last_activity = monotonic()
            if not num:
                # a closed connection is indicated by signaling
                # a read condition, and having recv() return 0.
                self.ready = False
        except ssl.SSLWantReadError:
            return
        except OSError as e:
            if e.errno in socket_errors_nonblocking or e.errno in socket_errors_eintr:
                return
            if e.errno in socket_errors_socket_closed:
                self.ready = False
                return
            raise

    def drain_ssl_buffer(self):
        try:
            self.read_buffer.recv_from(self.socket)
        except ssl.SSLWantReadError:
            return
        except ssl.SSLError as e:
            self.log.error(f'Error while reading SSL data from client: {as_unicode(e)}')
            self.ready = False
            return
        except OSError as e:
            if e.errno in socket_errors_nonblocking or e.errno in socket_errors_eintr:
                return
            if e.errno in socket_errors_socket_closed:
                self.ready = False
                return
            raise

    def close(self):
        self.ready = False
        self.handle_event = None  # prevent reference cycles
        try:
            self.socket.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass

    def queue_job(self, func, *args):
        if args:
            func = partial(func, *args)
        try:
            self.pool.put_nowait(self.socket.fileno(), func)
        except Full:
            raise JobQueueFull()
        self.set_state(WAIT, self._job_done)

    def _job_done(self, event):
        self.job_done(*event)

    def job_done(self, ok, result):
        raise NotImplementedError()

    @property
    def state_description(self):
        return ''

    def report_unhandled_exception(self, e, formatted_traceback):
        pass

    def report_busy(self):
        pass

    def connection_ready(self):
        raise NotImplementedError()

    def handle_timeout(self):
        return False
# }}}


@lru_cache(maxsize=2)
def parsed_trusted_ips(raw):
    return tuple(parse_trusted_ips(raw)) if raw else ()


class ServerLoop:

    LISTENING_MSG = 'calibre server listening on'

    def __init__(
        self,
        handler,
        opts=None,
        plugins=(),
        # A calibre logging object. If None, a default log that logs to
        # stdout is used
        log=None,
        # A calibre logging object for access logging, by default no access
        # logging is performed
        access_log=None
    ):
        self.ready = False
        self.handler = handler
        self.opts = opts or Options()
        self.log = log or ThreadSafeLog(level=ThreadSafeLog.DEBUG)
        self.jobs_manager = JobsManager(self.opts, self.log)
        self.access_log = access_log

        ba = (self.opts.listen_on, int(self.opts.port))
        if not ba[0]:
            # AI_PASSIVE does not work with host of '' or None
            ba = (get_fallback_server_addr(), ba[1])
        self.bind_address = ba
        self.bound_address = None
        self.connection_map = {}

        self.ssl_context = None
        if self.opts.ssl_certfile is not None and self.opts.ssl_keyfile is not None:
            self.ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            self.ssl_context.load_cert_chain(certfile=self.opts.ssl_certfile, keyfile=self.opts.ssl_keyfile)
            self.ssl_context.set_servername_callback(self.on_ssl_servername)

        self.pre_activated_socket = None
        self.socket_was_preactivated = False
        if self.opts.allow_socket_preallocation:
            from calibre.srv.pre_activated import pre_activated_socket
            self.pre_activated_socket = pre_activated_socket()
            if self.pre_activated_socket is not None:
                set_socket_inherit(self.pre_activated_socket, False)
                self.bind_address = self.pre_activated_socket.getsockname()

        self.create_control_connection()
        self.pool = ThreadPool(self.log, self.job_completed, count=self.opts.worker_count)
        self.plugin_pool = PluginPool(self, plugins)

    def on_ssl_servername(self, socket, server_name, ssl_context):
        c = self.connection_map.get(socket.fileno())
        if getattr(c, 'ssl_handshake_done', False):
            c.ready = False
            c.ssl_terminated = True
            # We do not allow client initiated SSL renegotiation
            return ssl.ALERT_DESCRIPTION_NO_RENEGOTIATION

    def create_control_connection(self):
        if iswindows:
            self.control_in, self.control_out = create_sock_pair()
        else:
            r, w = os.pipe()
            os.set_blocking(r, False)
            os.set_blocking(w, True)
            self.control_in =  open(w, 'wb')
            self.control_out = open(r, 'rb')

    def close_control_connection(self):
        with suppress(Exception):
            self.control_in.close()
        with suppress(Exception):
            self.control_out.close()

    def __str__(self):
        return f'{self.__class__.__name__}({self.bind_address!r})'
    __repr__ = __str__

    @property
    def num_active_connections(self):
        return len(self.connection_map)

    def do_bind(self):
        # Get the correct address family for our host (allows IPv6 addresses)
        host, port = self.bind_address
        try:
            info = socket.getaddrinfo(
                host, port, socket.AF_UNSPEC,
                socket.SOCK_STREAM, 0, socket.AI_PASSIVE)
        except socket.gaierror:
            if ':' in host:
                info = [(socket.AF_INET6, socket.SOCK_STREAM,
                        0, '', self.bind_address + (0, 0))]
            else:
                info = [(socket.AF_INET, socket.SOCK_STREAM,
                        0, '', self.bind_address)]

        self.socket = None
        msg = 'No socket could be created'
        for res in info:
            af, socktype, proto, canonname, sa = res
            try:
                self.bind(af, socktype, proto)
            except OSError as serr:
                msg = f'{msg} -- ({sa}: {as_unicode(serr)})'
                if self.socket:
                    self.socket.close()
                self.socket = None
                continue
            break
        if not self.socket:
            raise OSError(msg)

    def initialize_socket(self):
        if self.pre_activated_socket is None:
            self.socket_was_preactivated = False
            try:
                self.do_bind()
            except OSError as err:
                if not self.opts.fallback_to_detected_interface:
                    raise
                ip = get_external_ip()
                if ip == self.bind_address[0]:
                    raise
                self.log.warn(f'Failed to bind to {self.bind_address[0]} with error: {as_unicode(err)}. Trying to bind to the default interface: {ip} instead')
                self.bind_address = (ip, self.bind_address[1])
                self.do_bind()
        else:
            self.socket = self.pre_activated_socket
            self.socket_was_preactivated = True
            self.pre_activated_socket = None
            self.setup_socket()

    def serve(self):
        from calibre.utils.network import format_addr_for_url

        self.connection_map = {}
        if not self.socket_was_preactivated:
            self.socket.listen(min(socket.SOMAXCONN, 128))
        self.bound_address = ba = self.socket.getsockname()
        ba_str = ''
        if isinstance(ba, tuple):
            addr = format_addr_for_url(str(ba[0]))
            ba_str = f'{addr}:{ba[1]}'
        self.pool.start()
        with TemporaryDirectory(prefix='srv-') as tdir:
            self.tdir = tdir
            if self.LISTENING_MSG:
                self.log(self.LISTENING_MSG, ba_str)
            self.plugin_pool.start()
            self.ready = True

            while self.ready:
                try:
                    self.tick()
                except SystemExit:
                    self.shutdown()
                    raise
                except KeyboardInterrupt:
                    break
                except Exception:
                    self.log.exception('Error in ServerLoop.tick')
            self.shutdown()

    def serve_forever(self):
        ''' Listen for incoming connections. '''
        self.initialize_socket()
        self.serve()

    def setup_socket(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # If listening on the IPV6 any address ('::' = IN6ADDR_ANY),
        # activate dual-stack.
        if (hasattr(socket, 'AF_INET6') and self.socket.family == socket.AF_INET6 and
                self.bind_address[0] in ('::', '::0', '::0.0.0.0')):
            try:
                self.socket.setsockopt(IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except (AttributeError, OSError):
                # Apparently, the socket option is not available in
                # this machine's TCP stack
                pass
        self.socket.setblocking(0)

    def bind(self, family, atype, proto=0):
        '''Create (or recreate) the actual socket object.'''
        self.socket = socket.socket(family, atype, proto)
        set_socket_inherit(self.socket, False)
        self.setup_socket()
        self.socket.bind(self.bind_address)

    def tick(self):
        now = monotonic()
        read_needed, write_needed, readable, remove, close_needed = [], [], [], [], []
        has_ssl = self.ssl_context is not None
        for s, conn in iteritems(self.connection_map):
            if now - conn.last_activity > self.opts.timeout:
                if conn.handle_timeout():
                    conn.last_activity = now
                else:
                    remove.append((s, conn))
                    continue
            wf = conn.wait_for
            if wf is READ or wf is RDWR:
                if wf is RDWR:
                    write_needed.append(s)
                if conn.read_buffer.has_data:
                    readable.append(s)
                else:
                    if has_ssl:
                        conn.drain_ssl_buffer()
                        if conn.ready:
                            (readable if conn.read_buffer.has_data else read_needed).append(s)
                        else:
                            close_needed.append((s, conn))
                    else:
                        read_needed.append(s)
            elif wf is WRITE:
                write_needed.append(s)

        for s, conn in remove:
            self.log(f'Closing connection because of extended inactivity: {conn.state_description}')
            self.close(s, conn)

        for x, conn in close_needed:
            self.close(s, conn)

        if readable:
            writable = []
        else:
            try:
                readable, writable, _ = select.select([self.socket.fileno(), self.control_out.fileno()] + read_needed, write_needed, [], self.opts.timeout)
            except ValueError:  # self.socket.fileno() == -1
                self.ready = False
                self.log.error('Listening socket was unexpectedly terminated')
                return
            except OSError as e:
                # select.error has no errno attribute. errno is instead
                # e.args[0]
                if getattr(e, 'errno', e.args[0]) in socket_errors_eintr:
                    return
                for s, conn in tuple(iteritems(self.connection_map)):
                    try:
                        select.select([s], [], [], 0)
                    except OSError as e:
                        if getattr(e, 'errno', e.args[0]) not in socket_errors_eintr:
                            self.close(s, conn)  # Bad socket, discard
                return

        if not self.ready:
            return

        ignore = set()
        for s, conn, event in self.get_actions(readable, writable):
            if s in ignore:
                continue
            try:
                conn.handle_event(event)
                if not conn.ready:
                    self.close(s, conn)
            except JobQueueFull:
                self.log.exception(f'Server busy handling request: {conn.state_description}')
                if conn.ready:
                    if conn.response_started:
                        self.close(s, conn)
                    else:
                        try:
                            conn.report_busy()
                        except Exception:
                            self.close(s, conn)
            except Exception as e:
                ignore.add(s)
                ssl_terminated = getattr(conn, 'ssl_terminated', False)
                if ssl_terminated:
                    self.log.warn('Client tried to initiate SSL renegotiation, closing connection')
                    self.close(s, conn)
                else:
                    self.log.exception(f'Unhandled exception in state: {conn.state_description}')
                    if conn.ready:
                        if conn.response_started:
                            self.close(s, conn)
                        else:
                            try:
                                conn.report_unhandled_exception(e, traceback.format_exc())
                            except Exception:
                                self.close(s, conn)
                    else:
                        self.log.error(f'Error in SSL handshake, terminating connection: {as_unicode(e)}')
                        self.close(s, conn)

    def write_to_control(self, what):
        if iswindows:
            self.control_in.sendall(what)
        else:
            self.control_in.write(what)
            self.control_in.flush()

    def wakeup(self):
        self.write_to_control(WAKEUP)

    def job_completed(self):
        self.write_to_control(JOB_DONE)

    def dispatch_job_results(self):
        while True:
            try:
                s, ok, result = self.pool.get_nowait()
            except Empty:
                break
            conn = self.connection_map.get(s)
            if conn is not None:
                yield s, conn, (ok, result)

    def close(self, s, conn):
        self.connection_map.pop(s, None)
        conn.close()

    def get_actions(self, readable, writable):
        listener = self.socket.fileno()
        control = self.control_out.fileno()
        for s in readable:
            if s == listener:
                sock, addr = self.accept()
                if sock is not None:
                    s = sock.fileno()
                    if s > -1:
                        self.connection_map[s] = conn = self.handler(
                            sock, self.opts, self.ssl_context, self.tdir, addr, self.pool, self.log, self.access_log, self.wakeup)
                        if self.ssl_context is not None:
                            yield s, conn, RDWR
            elif s == control:
                f = self.control_out.recv if iswindows else self.control_out.read
                try:
                    c = f(1)
                except OSError as e:
                    if not self.ready:
                        return
                    self.log.error('Control connection raised an error:', e)
                    raise
                if c == JOB_DONE:
                    for s, conn, event in self.dispatch_job_results():
                        yield s, conn, event
                elif c == WAKEUP:
                    pass
                elif not c:
                    if not self.ready:
                        return
                    self.log.error('Control connection failed to read after signalling ready')
                    raise Exception('Control connection failed to read, something bad happened')
            else:
                yield s, self.connection_map[s], READ
        for s in writable:
            try:
                conn = self.connection_map[s]
            except KeyError:
                continue  # Happens if connection was closed during read phase
            yield s, conn, WRITE

    def accept(self):
        try:
            sock, addr = self.socket.accept()
            set_socket_inherit(sock, False), sock.setblocking(False)
            return sock, addr
        except OSError:
            return None, None

    def stop(self):
        self.ready = False
        self.wakeup()

    def shutdown(self):
        self.jobs_manager.shutdown()
        with suppress(socket.error):
            if getattr(self, 'socket', None):
                self.socket.close()
                self.socket = None
        for s, conn in tuple(iteritems(self.connection_map)):
            self.close(s, conn)
        wait_till = monotonic() + self.opts.shutdown_timeout
        for pool in (self.plugin_pool, self.pool):
            pool.stop(wait_till)
            if pool.workers:
                self.log.warn(f'Failed to shutdown {len(pool.workers)} workers in {pool.__class__.__name__} cleanly')
        self.jobs_manager.wait_for_shutdown(wait_till)


class EchoLine(Connection):  # {{{

    bye_after_echo = False

    def connection_ready(self):
        self.rbuf = BytesIO()
        self.set_state(READ, self.read_line)

    def read_line(self, event):
        data = self.recv(1)
        if data:
            self.rbuf.write(data)
            if b'\n' == data:
                if self.rbuf.tell() < 3:
                    # Empty line
                    self.rbuf = BytesIO(b'bye' + self.rbuf.getvalue())
                    self.bye_after_echo = True
                self.set_state(WRITE, self.echo)
                self.rbuf.seek(0)

    def echo(self, event):
        pos = self.rbuf.tell()
        self.rbuf.seek(0, os.SEEK_END)
        left = self.rbuf.tell() - pos
        self.rbuf.seek(pos)
        sent = self.send(self.rbuf.read(512))
        if sent == left:
            self.rbuf = BytesIO()
            self.set_state(READ, self.read_line)
            if self.bye_after_echo:
                self.ready = False
        else:
            self.rbuf.seek(pos + sent)
# }}}


def main():
    print('Starting Echo server')
    s = ServerLoop(EchoLine)
    with HandleInterrupt(s.stop):
        s.serve_forever()


if __name__ == '__main__':
    main()
