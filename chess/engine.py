# -*- coding: utf-8 -*-
#
# This file is part of the python-chess library.
# Copyright (C) 2012-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import abc
import asyncio
import collections
import concurrent.futures
import contextlib
import enum
import functools
import logging
import warnings
import shlex
import subprocess
import sys
import threading
import os

try:
    # Python 3.7
    from asyncio import get_running_loop as _get_running_loop
except ImportError:
    try:
        from asyncio import _get_running_loop
    except ImportError:
        # Python 3.4
        def _get_running_loop():
            return asyncio.get_event_loop()

try:
    # Python 3.7
    from asyncio import all_tasks as _all_tasks
except ImportError:
    _all_tasks = asyncio.Task.all_tasks

try:
    # Python 3.6
    _IntFlag = enum.IntFlag
except AttributeError:
    _IntFlag = enum.IntEnum

try:
    StopAsyncIteration
except NameError:
    # Python 3.4
    class StopAsyncIteration(Exception):
        pass

import chess


LOGGER = logging.getLogger(__name__)

KORK = object()

MANAGED_UCI_OPTIONS = ["uci_chess960", "uci_variant", "uci_analysemode", "multipv", "ponder"]

MANAGED_XBOARD_OPTIONS = ["MultiPV"]  # Case sensitive


class EventLoopPolicy(asyncio.DefaultEventLoopPolicy):
    """
    An event loop policy that ensures the event loop is capable of spawning
    and watching subprocesses, even when not running in the main thread.

    Windows: Creates a :class:`~asyncio.ProactorEventLoop`.

    Unix: Creates a :class:`~asyncio.SelectorEventLoop`. Child watchers are
    thread local. When not running on the main thread, the default child
    watchers use relatively slow polling to detect process termination.
    This does not affect communication.
    """
    class _ThreadLocal(threading.local):
        _watcher = None

    def __init__(self):
        super().__init__()
        self._thread_local = self._ThreadLocal()

    def get_child_watcher(self):
        if sys.platform == "win32" or threading.current_thread() == threading.main_thread():
            return super().get_child_watcher()

        class PollingChildWatcher(asyncio.SafeChildWatcher):
            def __init__(self):
                super().__init__()
                self._poll_handle = None
                self._poll_delay = 0.001

            def attach_loop(self, loop):
                assert loop is None or isinstance(loop, asyncio.AbstractEventLoop)

                if self._loop is not None and loop is None and self._callbacks:
                    warnings.warn("A loop is being detached from a child watcher with pending handlers", RuntimeWarning)

                if self._poll_handle is not None:
                    self._poll_handle.cancel()

                self._loop = loop
                if loop is not None:
                    self._poll_handle = self._loop.call_soon(self._poll)
                    self._do_waitpid_all()

            def _poll(self):
                if self._loop:
                    self._do_waitpid_all()
                    self._poll_delay = min(self._poll_delay * 2, 1.0)
                    self._poll_handle = self._loop.call_later(self._poll_delay, self._poll)

        if self._thread_local._watcher is None:
            self._thread_local._watcher = PollingChildWatcher()
        return self._thread_local._watcher

    def set_child_watcher(self, watcher):
        if sys.platform == "win32" or threading.current_thread() == threading.main_thread():
            return super().set_child_watcher(watcher)

        assert watcher is None or isinstance(watcher, asyncio.AbstractChildWatcher)

        if self._thread_local._watcher:
            self._thread_local._watcher.close()
        self._thread_local._watcher = watcher

    def new_event_loop(self):
        return asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.SelectorEventLoop()

    def set_event_loop(self, loop):
        super().set_event_loop(loop)

        if sys.platform != "win32" and threading.current_thread() != threading.main_thread():
            self.get_child_watcher().attach_loop(loop)


def run_in_background(coroutine, *, debug=False, _policy_lock=threading.Lock()):
    """
    Runs ``coroutine(future)`` in a new event loop on a background thread.

    Blocks and returns the *future* result as soon as it is resolved.
    The coroutine and all remaining tasks continue running in the background
    until it is complete.

    Note: This installs a :class:`chess.engine.EventLoopPolicy` for the entire
    process.
    """
    assert asyncio.iscoroutinefunction(coroutine)

    with _policy_lock:
        if not isinstance(asyncio.get_event_loop_policy(), EventLoopPolicy):
            asyncio.set_event_loop_policy(EventLoopPolicy())

    future = concurrent.futures.Future()

    def background():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.set_debug(debug)

        try:
            loop.run_until_complete(coroutine(future))
            future.cancel()
        except Exception as exc:
            future.set_exception(exc)
            return
        finally:
            try:
                # Finish all remaining tasks.
                pending = _all_tasks(loop)
                loop.run_until_complete(asyncio.gather(*pending, loop=loop, return_exceptions=True))

                # Shutdown async generators.
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except AttributeError:
                    # Before Python 3.6.
                    pass
            finally:
                loop.close()

    threading.Thread(target=background).start()
    return future.result()


class EngineError(RuntimeError):
    """Runtime error caused by a misbehaving engine or incorrect usage."""


class EngineTerminatedError(EngineError):
    """The engine process exited unexpectedly."""


class Option(collections.namedtuple("Option", "name type default min max var")):
    """Information about an available engine option."""

    def parse(self, value):
        if self.type == "check":
            return value and value != "false"
        elif self.type == "spin":
            try:
                value = int(value)
            except ValueError:
                raise EngineError("expected integer for spin option {!r}, got: {!r}".format(self.name, value))
            if self.min is not None and value < self.min:
                raise EngineError("expected value for option {!r} to be at least {}, got: {}".format(self.name, self.min, value))
            if self.max is not None and self.max < value:
                raise EngineError("expected value for option {!r} to be at most {}, got: {}".format(self.name, self.max, value))
            return value
        elif self.type == "combo":
            value = str(value)
            if value not in (self.var or []):
                raise EngineError("invalid value for combo option {!r}, got: {} (available: {})".format(self.name, value, ", ".join(self.var)))
            return value
        elif self.type in ["button", "reset", "save"]:
            return None
        elif self.type in ["string", "file", "path"]:
            value = str(value)
            if "\n" in value or "\r" in value:
                raise EngineError("invalid line-break in string option {!r}".format(self.name))
            return value
        else:
            raise EngineError("unknown option type: {}", self.type)

    def is_managed_uci(self):
        return self.name.lower() in MANAGED_UCI_OPTIONS

    def is_managed_xboard(self):
        return self.name in MANAGED_XBOARD_OPTIONS


class Limit:
    """Search termination condition."""

    def __init__(self, *, time=None, depth=None, nodes=None, mate=None, white_clock=None, black_clock=None, white_inc=None, black_inc=None, remaining_moves=None):
        self.time = time
        self.depth = depth
        self.nodes = nodes
        self.mate = mate
        self.white_clock = white_clock
        self.black_clock = black_clock
        self.white_inc = white_inc
        self.black_inc = black_inc
        self.remaining_moves = remaining_moves

    def __repr__(self):
        return "{}({})".format(
            type(self).__name__,
            ", ".join("{}={!r}".format(attr, getattr(self, attr))
                      for attr in ["time", "depth", "nodes", "mate", "white_clock", "black_clock", "white_inc", "black_inc", "remaining_moves"]
                      if getattr(self, attr) is not None))


class PlayResult:
    """Returned by :func:`chess.engine.EngineProtocol.play()`."""

    def __init__(self, move, ponder, info=None, draw_offered=False):
        self.move = move
        self.ponder = ponder
        self.info = info or {}
        self.draw_offered = draw_offered

    def __repr__(self):
        return "<{} at {:#x} (move={}, ponder={}, info={}, draw_offered={})>".format(type(self).__name__, id(self), self.move, self.ponder, self.info, self.draw_offered)


class Info(_IntFlag):
    """Select information sent by the chess engine."""
    NONE = 0
    BASIC = 1
    SCORE = 2
    PV = 4
    REFUTATION = 8
    CURRLINE = 16
    ALL = BASIC | SCORE | PV | REFUTATION | CURRLINE

INFO_NONE = Info.NONE
INFO_BASIC = Info.BASIC
INFO_SCORE = Info.SCORE
INFO_PV = Info.PV
INFO_REFUTATION = Info.REFUTATION
INFO_CURRLINE = Info.CURRLINE
INFO_ALL = Info.ALL


class PovScore:
    """A relative :class:`~chess.engine.Score` and the point of view."""

    def __init__(self, relative, turn):
        self.relative = relative
        self.turn = turn

    def white(self):
        """Get the score from White's point of view."""
        return self.pov(chess.WHITE)

    def black(self):
        """Get the score from Black's point of view."""
        return self.pov(chess.BLACK)

    def pov(self, color):
        """Get the score from the point of view of the given *color*."""
        return self.relative if self.turn == color else -self.relative

    def is_mate(self):
        """Tests if this is a mate score."""
        return self.relative.is_mate()

    def __repr__(self):
        return "PovScore({!r}, {})".format(self.relative, "WHITE" if self.turn else "BLACK")

    def __str__(self):
        return str(self.relative)

    def __eq__(self, other):
        try:
            return self.relative == other.relative and self.turn == other.turn
        except AttributeError:
            return NotImplemented

    def __ne__(self, other):
        try:
            return self.relative != other.relative or self.turn != other.turn
        except AttributeError:
            return NotImplemented


@functools.total_ordering
class Score(abc.ABC):
    """
    Evaluation of a position.

    The score can be :class:`~chess.engine.Cp` (centi-pawns),
    :class:`~chess.engine.Mate` or ``MateGiven``. A positive value indicates
    an advantage.

    There is a total order defined on centi-pawn and mate scores.

    >>> from chess.engine import Cp, Mate, MateGiven
    >>>
    >>> Mate(-0) < Mate(-1) < Cp(-50) < Cp(200) < Mate(4) < Mate(1) < MateGiven
    True

    Scores can be negated to change the point of view:

    >>> -Cp(20)
    Cp(-20)

    >>> -Mate(-4)
    Mate(+4)

    >>> -Mate(0)
    MateGiven
    """

    @abc.abstractmethod
    def score(self, *, mate_score=None):
        """
        Returns the centi-pawn score as an integer or ``None``.

        You can optionally pass a large value to convert mate scores to
        centi-pawn scores.

        >>> Cp(-300).score()
        -300
        >>> Mate(5).score() is None
        True
        >>> Mate(5).score(mate_score=100000)
        99995
        """

    @abc.abstractmethod
    def mate(self):
        """
        Returns the number of plies to mate, negative if we are getting
        mated, or ``None``.

        :warning: This conflates ``Mate(0)`` (we lost) and ``MateGiven``
            (we won) to ``0``.
        """

    def is_mate(self):
        """Tests if this is a mate score."""
        return self.mate() is not None

    @abc.abstractmethod
    def __neg__(self):
        pass

    def _score_tuple(self):
        return (
            isinstance(self, _MateGiven),
            self.is_mate() and self.mate() > 0,
            not self.is_mate(),
            -(self.mate() or 0),
            self.score(),
        )

    def __eq__(self, other):
        try:
            return self._score_tuple() == other._score_tuple()
        except AttributeError:
            return NotImplemented

    def __lt__(self, other):
        try:
            return self._score_tuple() < other._score_tuple()
        except AttributeError:
            return NotImplemented


class Cp(Score):
    """Centi-pawn score."""

    def __init__(self, cp):
        self.cp = cp

    def mate(self):
        return None

    def score(self, *, mate_score=None):
        return self.cp

    def __str__(self):
        return "+{:d}".format(self.cp) if self.cp > 0 else str(self.cp)

    def __repr__(self):
        return "Cp({})".format(self)

    def __neg__(self):
        return Cp(-self.cp)

    def __pos__(self):
        return Cp(self.cp)

    def __abs__(self):
        return Cp(abs(self.cp))


class Mate(Score):
    """Mate score."""

    def __init__(self, moves):
        self.moves = moves

    def mate(self):
        return self.moves

    def score(self, *, mate_score=None):
        if mate_score is None:
            return None
        elif self.moves > 0:
            return mate_score - self.moves
        else:
            return -mate_score - self.moves

    def __str__(self):
        return "#+{}".format(self.moves) if self.moves > 0 else "#-{}".format(abs(self.moves))

    def __repr__(self):
        return "Mate({})".format(str(self).lstrip("#"))

    def __neg__(self):
        return MateGiven if not self.moves else Mate(-self.moves)

    def __pos__(self):
        return Mate(self.moves)

    def __abs__(self):
        return MateGiven if not self.moves else Mate(abs(self.moves))

class _MateGiven(Score):
    """Winning mate score, equivalent to ``-Mate(0)``."""

    def mate(self):
        return 0

    def score(self, *, mate_score=None):
        return mate_score

    def __neg__(self):
        return Mate(0)

    def __pos__(self):
        return self

    def __abs__(self):
        return self

    def __repr__(self):
        return "MateGiven"

    def __str__(self):
        return "#+0"


MateGiven = _MateGiven()


class MockTransport:
    def __init__(self, protocol):
        self.protocol = protocol
        self.expectations = collections.deque()
        self.expected_pings = 0
        self.stdin_buffer = bytearray()
        self.protocol.connection_made(self)

    def expect(self, expectation, responses=[]):
        self.expectations.append((expectation, responses))

    def expect_ping(self):
        self.expected_pings += 1

    def assert_done(self):
        assert not self.expectations, "pending expectations: {}".format(self.expectations)

    def get_pipe_transport(self, fd):
        assert fd == 0, "expected 0 for stdin, got {}".format(fd)
        return self

    def write(self, data):
        self.stdin_buffer.extend(data)
        while b"\n" in self.stdin_buffer:
            line, self.stdin_buffer = self.stdin_buffer.split(b"\n", 1)
            line = line.decode("utf-8")

            if line.startswith("ping ") and self.expected_pings:
                self.expected_pings -= 1
                self.protocol.loop.call_soon(lambda: self.protocol.pipe_data_received(1, line.replace("ping ", "pong ").encode("utf-8") + b"\n"))
            else:
                assert self.expectations, "unexpected: {}".format(line)
                expectation, responses = self.expectations.popleft()
                assert expectation == line, "expected {}, got: {}".format(expectation, line)
                self.protocol.loop.call_soon(lambda: self.protocol.pipe_data_received(1, "\n".join(responses).encode("utf-8") + b"\n"))

    def get_pid(self):
        return id(self)

    def get_returncode(self):
        return None if self.expectations else 0


class EngineProtocol(asyncio.SubprocessProtocol, metaclass=abc.ABCMeta):
    """Protocol for communicating with a chess engine process."""

    def __init__(self):
        self.loop = _get_running_loop()
        self.transport = None

        self.buffer = {
            1: bytearray(),  # stdout
            2: bytearray(),  # stderr
        }

        self.command = None
        self.next_command = None

        self.returncode = asyncio.Future(loop=self.loop)

    def connection_made(self, transport):
        self.transport = transport
        LOGGER.debug("%s: Connection made", self)

    def connection_lost(self, exc):
        code = self.transport.get_returncode()
        LOGGER.debug("%s: Connection lost (exit code: %d, error: %s)", self, code, exc)

        # Terminate commands.
        if self.command is not None:
            self.command._engine_terminated(self, code)
            self.command = None
        if self.next_command is not None:
            self.next_command._engine_terminated(self, code)
            self.next_command = None

        self.returncode.set_result(code)

    def process_exited(self):
        LOGGER.debug("%s: Process exited", self)

    def send_line(self, line):
        LOGGER.debug("%s: << %s", self, line)
        stdin = self.transport.get_pipe_transport(0)
        stdin.write(line.encode("utf-8"))
        stdin.write(b"\n")

    def pipe_data_received(self, fd, data):
        self.buffer[fd].extend(data)
        while b"\n" in self.buffer[fd]:
            line, self.buffer[fd] = self.buffer[fd].split(b"\n", 1)
            line = line.decode("utf-8")
            if fd == 1:
                self._line_received(line)
            else:
                self.error_line_received(line)

    def error_line_received(self, line):
        LOGGER.warning("%s: stderr >> %s", self, line)

    def _line_received(self, line):
        LOGGER.debug("%s: >> %s", self, line)

        self.line_received(line)

        if self.command:
            self.command._line_received(self, line)

    def line_received(self, line):
        pass

    @asyncio.coroutine
    def communicate(self, command_factory):
        command = command_factory(self.loop)

        if self.returncode.done():
            raise EngineTerminatedError("engine process dead (exit code: {})".format(self.returncode.result()))

        assert command.state == CommandState.New

        if self.next_command is not None:
            self.next_command.result.cancel()
            self.next_command.finished.cancel()
            self.next_command._done()

        self.next_command = command

        def previous_command_finished(_):
            if self.command is not None:
                self.command._done()

            self.command, self.next_command = self.next_command, None
            if self.command is not None:
                cmd = self.command
                cmd.result.add_done_callback(lambda result: cmd._cancel(self) if cmd.result.cancelled() else None)
                cmd.finished.add_done_callback(previous_command_finished)
                cmd._start(self)

        if self.command is None:
            previous_command_finished(None)
        elif not self.command.result.done():
            self.command.result.cancel()
        elif not self.command.result.cancelled():
            self.command._cancel(self)

        return (yield from command.result)

    def __repr__(self):
        pid = self.transport.get_pid() if self.transport is not None else "?"
        return "<{} (pid={})>".format(type(self).__name__, pid)

    @abc.abstractmethod
    @asyncio.coroutine
    def _initialize(self):
        pass

    @abc.abstractmethod
    @asyncio.coroutine
    def ping(self):
        """
        Pings the engine and waits for a response. Used to ensure the engine
        is still alive and idle.
        """

    @abc.abstractmethod
    @asyncio.coroutine
    def configure(self, options):
        """Configures global engine options."""

    @abc.abstractmethod
    @asyncio.coroutine
    def play(self, board, limit, *, game=None, info=INFO_NONE, ponder=False, root_moves=None, options={}):
        """
        Play a position.

        :param board: The position. The entire move stack will be sent to the
            engine.
        :param limit: An instance of :class:`chess.engine.Limit` that
            determines when to stop thinking.
        :param game: Optional. An arbitrary object that identifies the game.
            Will automatically inform the engine if the object is not equal
            to the previous game (e.g. ``ucinewgame``, ``new``).
        :param info: Selects which additional information to retrieve from the
            engine. ``INFO_NONE``, ``INFO_BASE`` (basic information that is
            trivial to obtain), ``INFO_SCORE``, ``INFO_PV``,
            ``INFO_REFUTATION``, ``INFO_CURRLINE``, ``INFO_ALL`` or any
            bitwise combination. Some overhead is associated with parsing
            extra information.
        :param ponder: Whether the engine should keep analysing in the
            background even after the result has been returned.
        :param root_moves: Optional. Consider only root moves from this list.
        :param options: Optional. A dictionary of engine options for the
            analysis. The previous configuration will be restored after the
            analysis is complete. You can permanently apply a configuration
            with :func:`~chess.engine.EngineProtocol.configure()`.
        """

    @asyncio.coroutine
    def analyse(self, board, limit, *, multipv=None, game=None, info=INFO_ALL, root_moves=None, options={}):
        """
        Analyses a position and returns a dictionary of
        `information <#chess.engine.PlayResult.info>`_.

        :param board: The position to analyse. The entire move stack will be
            sent to the engine.
        :param limit: An instance of :class:`chess.engine.Limit` that
            determines when to stop the analysis.
        :param multipv: Optional. Analyse multiple root moves. Will return a list of
            at most *multipv* dictionaries rather than just a single
            info dictionary.
        :param game: Optional. An arbitrary object that identifies the game.
            Will automatically inform the engine if the object is not equal
            to the previous game (e.g. ``ucinewgame``, ``new``).
        :param info: Selects which information to retrieve from the
            engine. ``INFO_NONE``, ``INFO_BASE`` (basic information that is
            trivial to obtain), ``INFO_SCORE``, ``INFO_PV``,
            ``INFO_REFUTATION``, ``INFO_CURRLINE``, ``INFO_ALL`` or any
            bitwise combination. Some overhead is associated with parsing
            extra information.
        :param root_moves: Optional. Limit analysis to a list of root moves.
        :param options: Optional. A dictionary of engine options for the
            analysis. The previous configuration will be restored after the
            analysis is complete. You can permanently apply a configuration
            with :func:`~chess.engine.EngineProtocol.configure()`.
        """
        analysis = yield from self.analysis(board, limit, game=game, info=info, root_moves=root_moves, options=options)

        with analysis:
            yield from analysis.wait()

        return analysis.info if multipv is None else analysis.multipv

    @abc.abstractmethod
    @asyncio.coroutine
    def analysis(self, board, limit=None, *, multipv=None, game=None, info=INFO_ALL, root_moves=None, options={}):
        """
        Starts analysing a position.

        :param board: The position to analyse. The entire move stack will be
            sent to the engine.
        :param limit: Optional. An instance of :class:`chess.engine.Limit`
            that determines when to stop the analysis. Analysis is infinite
            by default.
        :param multipv: Optional. Analyse multiple root moves.
        :param game: Optional. An arbitrary object that identifies the game.
            Will automatically inform the engine if the object is not equal
            to the previous game (e.g. ``ucinewgame``, ``new``).
        :param info: Selects which information to retrieve from the
            engine. ``INFO_NONE``, ``INFO_BASE`` (basic information that is
            trivial to obtain), ``INFO_SCORE``, ``INFO_PV``,
            ``INFO_REFUTATION``, ``INFO_CURRLINE``, ``INFO_ALL`` or any
            bitwise combination. Some overhead is associated with parsing
            extra information.
        :param root_moves: Optional. Limit analysis to a list of root moves.
        :param options: Optional. A dictionary of engine options for the
            analysis. The previous configuration will be restored after the
            analysis is complete. You can permanently apply a configuration
            with :func:`~chess.engine.EngineProtocol.configure()`.

        Returns :class:`~chess.engine.AnalysisResult`, a handle that allows
        asynchronously iterating over the information sent by the engine
        and stopping the the analysis at any time.
        """

    @abc.abstractmethod
    @asyncio.coroutine
    def quit(self):
        """Asks the engine to shut down."""

    @classmethod
    @asyncio.coroutine
    def popen(cls, command, *, setpgrp=False, **kwargs):
        if not isinstance(command, list):
            command = [command]

        popen_args = {}
        if setpgrp:
            try:
                # Windows.
                popen_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            except AttributeError:
                # Unix.
                popen_args["preexec_fn"] = os.setpgrp
        popen_args.update(kwargs)

        loop = _get_running_loop()
        transport, protocol = yield from loop.subprocess_exec(cls, *command, **popen_args)
        yield from protocol._initialize()
        return transport, protocol


class CommandState(enum.Enum):
    New = 1
    Active = 2
    Cancelling = 3
    Done = 4


class BaseCommand:
    def __init__(self, loop):
        self.state = CommandState.New

        self.loop = loop
        self.result = asyncio.Future(loop=loop)
        self.finished = asyncio.Future(loop=loop)

    def _engine_terminated(self, engine, code):
        exc = EngineTerminatedError("engine process died unexpectedly (exit code: {})".format(code))
        self._handle_exception(engine, exc)

        if self.state in [CommandState.Active, CommandState.Cancelling]:
            self.engine_terminated(engine, exc)

    def _handle_exception(self, engine, exc):
        if not self.result.done():
            self.result.set_exception(exc)
        else:
            self.loop.call_exception_handler({
                "message": "engine command failed after returning preliminary result",
                "exception": exc,
                "protocol": engine,
                "transport": engine.transport,
            })

        if not self.finished.done():
            self.finished.set_result(None)

    def set_finished(self):
        assert self.state in [CommandState.Active, CommandState.Cancelling]
        if not self.result.done():
            self.result.set_result(None)
        self.finished.set_result(None)

    def _cancel(self, engine):
        assert self.state == CommandState.Active
        self.state = CommandState.Cancelling
        self.cancel(engine)

    def _start(self, engine):
        assert self.state == CommandState.New
        self.state = CommandState.Active
        try:
            self.start(engine)
        except EngineError as err:
            self._handle_exception(engine, err)

    def _done(self):
        assert self.state != CommandState.Done
        self.state = CommandState.Done

    def _line_received(self, engine, line):
        assert self.state in [CommandState.Active, CommandState.Cancelling]
        self.line_received(engine, line)

    def cancel(self, engine):
        pass

    def start(self, engine):
        raise NotImplementedError

    def line_received(self, engine, line):
        pass

    def engine_terminated(self, engine, exc):
        pass

    def __repr__(self):
        return "<{} at {:#x} (state={}, result={}, finished={}>".format(type(self).__name__, id(self), self.state, self.result, self.finished)


class UciProtocol(EngineProtocol):
    """
    An implementation of the
    `Universal Chess Interface <https://www.chessprogramming.org/UCI>`_
    protocol.
    """

    def __init__(self):
        super().__init__()
        self.options = UciOptionMap()
        self.config = UciOptionMap()
        self.id = {}
        self.board = chess.Board()
        self.game = None

    @asyncio.coroutine
    def _initialize(self):
        class Command(BaseCommand):
            def start(self, engine):
                engine.send_line("uci")

            def line_received(self, engine, line):
                if line == "uciok":
                    self.set_finished()
                elif line.startswith("option "):
                    self._option(engine, line.split(" ", 1)[1])
                elif line.startswith("id "):
                    self._id(engine, line.split(" ", 1)[1])

            def _option(self, engine, arg):
                current_parameter = None

                name = []
                type = []
                default = []
                min = None
                max = None
                current_var = None
                var = []

                for token in arg.split(" "):
                    if token == "name" and not name:
                        current_parameter = "name"
                    elif token == "type" and not type:
                        current_parameter = "type"
                    elif token == "default" and not default:
                        current_parameter = "default"
                    elif token == "min" and min is None:
                        current_parameter = "min"
                    elif token == "max" and max is None:
                        current_parameter = "max"
                    elif token == "var":
                        current_parameter = "var"
                        if current_var is not None:
                            var.append(" ".join(current_var))
                        current_var = []
                    elif current_parameter == "name":
                        name.append(token)
                    elif current_parameter == "type":
                        type.append(token)
                    elif current_parameter == "default":
                        default.append(token)
                    elif current_parameter == "var":
                        current_var.append(token)
                    elif current_parameter == "min":
                        try:
                            min = int(token)
                        except ValueError:
                            LOGGER.exception("exception parsing option min")
                    elif current_parameter == "max":
                        try:
                            max = int(token)
                        except ValueError:
                            LOGGER.exception("exception parsing option max")

                if current_var is not None:
                    var.append(" ".join(current_var))

                name = " ".join(name)
                type = " ".join(type)
                default = " ".join(default)

                without_default = Option(name, type, None, min, max, var)
                option = Option(name, type, without_default.parse(default), min, max, var)
                engine.options[option.name] = option

            def _id(self, engine, arg):
                key, value = arg.split(" ", 1)
                engine.id[key] = value

        return (yield from self.communicate(Command))

    def _isready(self):
        self.send_line("isready")

    def _ucinewgame(self):
        self.send_line("ucinewgame")

    def debug(self, on=True):
        """
        Switches debug move of the engine on or off. This does not interrupt
        other ongoing operations.
        """
        if on:
            self.send_line("debug on")
        else:
            self.send_line("debug off")

    @asyncio.coroutine
    def ping(self):
        class Command(BaseCommand):
            def start(self, engine):
                engine._isready()

            def line_received(self, engine, line):
                if line == "readyok":
                    self.set_finished()
                else:
                    LOGGER.warning("%s: Unexpected engine output: %s", engine, line)

        return (yield from self.communicate(Command))

    def _getoption(self, option, default=None):
        if option in self.config:
            return self.config[option]
        if option in self.options:
            return self.options[option].default
        return default

    def _setoption(self, name, value):
        try:
            value = self.options[name].parse(value)
        except KeyError:
            raise EngineError("engine does not support option {} (available options: {})".format(name, ", ".join(self.options)))

        if value is None or value != self._getoption(name):
            builder = ["setoption name", name]
            if value is False:
                builder.append("value false")
            elif value is True:
                builder.append("value true")
            elif value is not None:
                builder.append("value")
                builder.append(str(value))

            self.send_line(" ".join(builder))
            self.config[name] = value

    def _configure(self, options):
        for name, value in options.items():
            if name.lower() in MANAGED_UCI_OPTIONS:
                raise EngineError("cannot set {} which is automatically managed".format(name))
            else:
                self._setoption(name, value)

    @asyncio.coroutine
    def configure(self, options):
        class Command(BaseCommand):
            def start(self, engine):
                engine._configure(options)
                self.set_finished()

        return (yield from self.communicate(Command))

    def _position(self, board):
        # Select UCI_Variant and UCI_Chess960.
        uci_variant = type(board).uci_variant
        if uci_variant != self._getoption("UCI_Variant", "chess"):
            if "UCI_Variant" not in self.options:
                raise EngineError("engine does not support UCI_Variant")
            self._setoption("UCI_Variant", uci_variant)

        if board.chess960 != self._getoption("UCI_Chess960", False):
            if "UCI_Chess960" not in self.options:
                raise EngineError("engine does not support UCI_Chess960")
            self._setoption("UCI_Chess960", board.chess960)

        # Send starting position.
        builder = ["position"]
        root = board.root()
        fen = root.fen()
        if uci_variant == "chess" and fen == chess.STARTING_FEN:
            builder.append("startpos")
        else:
            builder.append("fen")
            builder.append(root.shredder_fen() if board.chess960 else fen)

        # Send moves.
        if board.move_stack:
            builder.append("moves")
            builder.extend(move.uci() for move in board.move_stack)

        self.send_line(" ".join(builder))
        self.board = board.copy(stack=False)

    def _go(self, limit, *, root_moves=None, ponder=False, infinite=False):
        builder = ["go"]

        if ponder:
            builder.append("ponder")

        if limit.white_clock is not None:
            builder.append("wtime")
            builder.append(str(int(limit.white_clock * 1000)))

        if limit.black_clock is not None:
            builder.append("btime")
            builder.append(str(int(limit.black_clock * 1000)))

        if limit.white_inc is not None:
            builder.append("winc")
            builder.append(str(int(limit.white_inc * 1000)))

        if limit.black_inc is not None:
            builder.append("binc")
            builder.append(str(int(limit.black_inc * 1000)))

        if limit.remaining_moves is not None and int(limit.remaining_moves) > 0:
            builder.append("movestogo")
            builder.append(str(int(limit.remaining_moves)))

        if limit.depth is not None:
            builder.append("depth")
            builder.append(str(int(limit.depth)))

        if limit.nodes is not None:
            builder.append("nodes")
            builder.append(str(int(limit.nodes)))

        if limit.mate is not None:
            builder.append("mate")
            builder.append(str(int(limit.mate)))

        if limit.time is not None:
            builder.append("movetime")
            builder.append(str(int(limit.time * 1000)))

        if infinite:
            builder.append("infinite")

        if root_moves:
            builder.append("searchmoves")
            builder.extend(move.uci() for move in root_moves)

        self.send_line(" ".join(builder))

    @asyncio.coroutine
    def play(self, board, limit, *, game=None, info=INFO_NONE, ponder=False, root_moves=None, options={}):
        previous_config = self.config.copy()

        class Command(BaseCommand):
            def start(self, engine):
                self.info = {}
                self.pondering = False

                if "UCI_AnalyseMode" in engine.options:
                    engine._setoption("UCI_AnalyseMode", False)
                if "Ponder" in engine.options:
                    engine._setoption("Ponder", ponder)
                if "MultiPV" in engine.options:
                    engine._setoption("MultiPV", engine.options["MultiPV"].default)

                engine._configure(options)

                if engine.game != game:
                    engine._ucinewgame()
                engine.game = game

                engine._position(board)
                engine._go(limit, root_moves=root_moves)

            def line_received(self, engine, line):
                if line.startswith("info "):
                    self._info(engine, line.split(" ", 1)[1])
                elif line.startswith("bestmove "):
                    self._bestmove(engine, line.split(" ", 1)[1])
                else:
                    LOGGER.warning("%s: Unexpected engine output: %s", engine, line)

            def _info(self, engine, arg):
                if not self.pondering:
                    self.info.update(_parse_uci_info(arg, engine.board, info))

            def _bestmove(self, engine, arg):
                try:
                    if self.pondering:
                        self.pondering = False
                    elif not self.result.cancelled():
                        tokens = arg.split(None, 2)

                        bestmove = None
                        if tokens[0] != "(none)":
                            try:
                                bestmove = engine.board.parse_uci(tokens[0])
                            except ValueError as err:
                                self.result.set_exception(EngineError(err))
                                return

                        pondermove = None
                        if bestmove is not None and len(tokens) >= 3 and tokens[1] == "ponder" and tokens[2] != "(none)":
                            engine.board.push(bestmove)
                            try:
                                pondermove = engine.board.push_uci(tokens[2])
                            except ValueError:
                                LOGGER.exception("engine sent invalid ponder move")

                        self.result.set_result(PlayResult(bestmove, pondermove, self.info, False))

                        if ponder and pondermove:
                            self.pondering = True
                            engine._position(engine.board)
                            engine._go(limit, ponder=True)
                finally:
                    if not self.pondering:
                        self.end(engine)

            def end(self, engine):
                for name, value in previous_config.items():
                    engine._setoption(name, value)
                for name, option in engine.options.items():
                    if name not in ["UCI_AnalyseMode", "Ponder"] and name not in previous_config and option.default is not None:
                        engine._setoption(name, option.default)

                self.set_finished()

            def cancel(self, engine):
                engine.send_line("stop")

        return (yield from self.communicate(Command))

    @asyncio.coroutine
    def analysis(self, board, limit=None, *, multipv=None, game=None, info=INFO_ALL, root_moves=None, options={}):
        previous_config = self.config.copy()

        class Command(BaseCommand):
            def start(self, engine):
                self.analysis = AnalysisResult(stop=lambda: self.cancel(engine))

                if "UCI_AnalyseMode" in engine.options:
                    engine._setoption("UCI_AnalyseMode", True)

                if "MultiPV" in engine.options or (multipv and multipv > 1):
                    engine._setoption("MultiPV", 1 if multipv is None else multipv)

                engine._configure(options)

                if engine.game != game:
                    engine._ucinewgame()
                engine.game = game

                engine._position(board)

                if limit:
                    engine._go(limit, root_moves=root_moves)
                else:
                    engine._go(Limit(), root_moves=root_moves, infinite=True)

                self.result.set_result(self.analysis)

            def line_received(self, engine, line):
                if line.startswith("info "):
                    self._info(engine, line.split(" ", 1)[1])
                elif line.startswith("bestmove "):
                    self._bestmove(engine, line.split(" ", 1)[1])
                else:
                    LOGGER.warning("%s: Unexpected engine output: %s", engine, line)

            def _info(self, engine, arg):
                self.analysis.post(_parse_uci_info(arg, engine.board, info))

            def _bestmove(self, engine, arg):
                for name, value in previous_config.items():
                    engine._setoption(name, value)
                for name, option in engine.options.items():
                    if name not in ["UCI_AnalyseMode", "Ponder", "MultiPV"] and name not in previous_config and option.default is not None:
                        engine._setoption(name, option.default)

                self.analysis.set_finished()
                self.set_finished()

            def cancel(self, engine):
                engine.send_line("stop")

            def engine_terminated(self, engine, exc):
                LOGGER.debug("%s: Closing analysis because engine has been terminated (error: %s)", engine, exc)
                self.analysis.set_exception(exc)

        return (yield from self.communicate(Command))

    @asyncio.coroutine
    def quit(self):
        self.send_line("quit")
        yield from self.returncode


def _parse_uci_info(arg, root_board, selector=INFO_ALL):
    info = {}
    if not selector:
        return info

    # Initialize parser state.
    board = None
    pv = None
    score_kind = None
    refutation_move = None
    refuted_by = []
    currline_cpunr = None
    currline_moves = []
    string = []

    # Parameters with variable length can only be handled when the
    # next parameter starts or at the end of the line.
    def end_of_parameter():
        if pv is not None:
            info["pv"] = pv

        if refutation_move is not None:
            if "refutation" not in info:
                info["refutation"] = {}
            info["refutation"][refutation_move] = refuted_by

        if currline_cpunr is not None:
            if "currline" not in info:
                info["currline"] = {}
            info["currline"][currline_cpunr] = currline_moves

    # Parse all other parameters.
    current_parameter = None
    for token in arg.split(" "):
        if current_parameter == "string":
            string.append(token)
        elif not token:
            # Ignore extra spaces. Those can not be directly discarded,
            # because they may occur in the string parameter.
            pass
        elif token in ["depth", "seldepth", "time", "nodes", "pv", "multipv", "score", "currmove", "currmovenumber", "hashfull", "nps", "tbhits", "cpuload", "refutation", "currline", "ebf", "string"]:
            end_of_parameter()
            current_parameter = token

            board = None
            pv = None
            score_kind = None
            refutation_move = None
            refuted_by = []
            currline_cpunr = None
            currline_moves = []

            if current_parameter == "pv" and selector & INFO_PV:
                pv = []
                board = root_board.copy(stack=False)
            elif current_parameter == "refutation" and selector & INFO_REFUTATION:
                board = root_board.copy(stack=False)
            elif current_parameter == "currline" and selector & INFO_CURRLINE:
                board = root_board.copy(stack=False)
        elif current_parameter in ["depth", "seldepth", "nodes", "multipv", "currmovenumber", "hashfull", "nps", "tbhits", "cpuload"]:
            try:
                info[current_parameter] = int(token)
            except ValueError:
                LOGGER.error("exception parsing %s from info: %r", current_parameter, arg)
        elif current_parameter == "time":
            try:
                info[current_parameter] = int(token) / 1000.0
            except ValueError:
                LOGGER.error("exception parsing %s from info: %r", current_parameter, arg)
        elif current_parameter == "pv" and pv is not None:
            try:
                pv.append(board.push_uci(token))
            except ValueError:
                LOGGER.exception("exception parsing pv from info: %r, position at root: %s", arg, root_board.fen())
        elif current_parameter == "score" and selector & INFO_SCORE:
            try:
                if token in ["cp", "mate"]:
                    score_kind = token
                elif token == "lowerbound":
                    info["lowerbound"] = True
                elif token == "upperbound":
                    info["upperbound"] = True
                elif score_kind == "cp":
                    info["score"] = PovScore(Cp(int(token)), root_board.turn)
                elif score_kind == "mate":
                    info["score"] = PovScore(Mate(int(token)), root_board.turn)
            except ValueError:
                LOGGER.error("exception parsing score %s from info: %r", score_kind, arg)
        elif current_parameter == "currmove":
            try:
                info[current_parameter] = chess.Move.from_uci(token)
            except ValueError:
                LOGGER.error("exception parsing %s from info: %r", current_parameter, arg)
        elif current_parameter == "refutation" and board is not None:
            try:
                if refutation_move is None:
                    refutation_move = board.push_uci(token)
                else:
                    refuted_by.append(board.push_uci(token))
            except ValueError:
                LOGGER.exception("exception parsing refutation from info: %r, position at root: %s", arg, root_board.fen())
        elif current_parameter == "currline" and board is not None:
            try:
                if currline_cpunr is None:
                    currline_cpunr = int(token)
                else:
                    currline_moves.append(board.push_uci(token))
            except ValueError:
                LOGGER.exception("exception parsing currline from info: %r, position at root: %s", arg, root_board.fen())
        elif current_parameter == "ebf":
            try:
                info[current_parameter] = float(token)
            except ValueError:
                LOGGER.error("exception parsing %s from info: %r", current_parameter, arg)

    end_of_parameter()

    if string:
        info["string"] = " ".join(string)

    return info


class UciOptionMap(collections.abc.MutableMapping):
    """Dictionary with case-insensitive keys."""

    def __init__(self, data=None, **kwargs):
        self._store = dict()
        if data is None:
            data = {}
        self.update(data, **kwargs)

    def __setitem__(self, key, value):
        self._store[key.lower()] = (key, value)

    def __getitem__(self, key):
        return self._store[key.lower()][1]

    def __delitem__(self, key):
        del self._store[key.lower()]

    def __iter__(self):
        return (casedkey for casedkey, mappedvalue in self._store.values())

    def __len__(self):
        return len(self._store)

    def __eq__(self, other):
        for key, value in self.items():
            if key not in other or other[key] != value:
                return False

        for key, value in other.items():
            if key not in self or self[key] != value:
                return False

        return True

    def copy(self):
        return type(self)(self._store.values())

    def __copy__(self):
        return self.copy()

    def __repr__(self):
        return "{}({!r})".format(type(self).__name__, dict(self.items()))


class XBoardProtocol(EngineProtocol):
    """
    An implementation of the
    `XBoard protocol <http://hgm.nubati.net/CECP.html>`_ (CECP).
    """

    def __init__(self):
        super().__init__()
        self.features = {}
        self.id = {}
        self.options = {
            "random": Option("random", "check", False, None, None, None),
            "computer": Option("computer", "check", False, None, None, None),
        }
        self.config = {}
        self.board = chess.Board()
        self.game = None

    @asyncio.coroutine
    def _initialize(self):
        class Command(BaseCommand):
            def start(self, engine):
                engine.send_line("xboard")
                engine.send_line("protover 2")
                self.timeout_handle = engine.loop.call_later(2.0, lambda: self.timeout(engine))

            def timeout(self, engine):
                LOGGER.error("%s: Timeout during initialization", engine)
                self.end(engine)

            def line_received(self, engine, line):
                if line.startswith("#"):
                    pass
                elif line.startswith("feature "):
                    self._feature(engine, line.split(" ", 1)[1])

            def _feature(self, engine, arg):
                for feature in shlex.split(arg):
                    key, value = feature.split("=", 1)
                    if key == "option":
                        option = _parse_xboard_option(value)
                        if option.name not in ["random", "computer", "cores", "memory"]:
                            engine.options[option.name] = option
                    else:
                        try:
                            engine.features[key] = int(value)
                        except ValueError:
                            engine.features[key] = value

                if "done" in engine.features:
                    self.timeout_handle.cancel()
                if engine.features.get("done"):
                    self.end(engine)

            def end(self, engine):
                if not engine.features.get("ping", 0):
                    self.result.set_exception(EngineError("xboard engine did not declare required feature: ping"))
                if not engine.features.get("setboard", 0):
                    self.result.set_exception(EngineError("xboard engine did not declare required feature: setboard"))

                if not engine.features.get("reuse", 1):
                    LOGGER.warning("%s: Rejecting feature reuse=0", engine)
                    engine.send_line("reject reuse")
                if not engine.features.get("sigterm", 1):
                    LOGGER.warning("%s: Rejecting feature sigterm=0", engine)
                    engine.send_line("reject sigterm")
                if engine.features.get("usermove", 0):
                    LOGGER.warning("%s: Rejecting feature usermove=1", engine)
                    engine.send_line("reject usermove")
                if engine.features.get("san", 0):
                    LOGGER.warning("%s: Rejecting feature san=1", engine)
                    engine.send_line("reject san")

                if "myname" in engine.features:
                    engine.id["name"] = engine.features["myname"]

                if engine.features.get("memory", 0):
                    engine.options["memory"] = Option("memory", "spin", 16, 1, None, None)
                    engine.send_line("accept memory")
                if engine.features.get("smp", 0):
                    engine.options["cores"] = Option("cores", "spin", 1, 1, None, None)
                    engine.send_line("accept smp")
                if engine.features.get("egt"):
                    for egt in engine.features["egt"].split(","):
                        name = "egtpath {}".format(egt)
                        engine.options[name] = Option(name, "path", None, None, None, None)
                    engine.send_line("accept egt")

                self.set_finished()

        yield from self.communicate(Command)

    def _ping(self, n):
        self.send_line("ping {}".format(n))

    def _variant(self, variant):
        variants = self.features.get("variants", "").split(",")
        if not variant or variant not in variants:
            raise EngineError("unsupported xboard variant: {} (available: {})".format(variant, ", ".join(variants)))

        self.send_line("variant {}".format(variant))

    def _new(self, board, game, options):
        self._configure(options)

        # Setup start position.
        root = board.root()
        new_options = "random" in options or "computer" in options
        new_game = self.game != game or new_options or root != self.board.root()
        self.game = game
        if new_game:
            self.board = root
            self.send_line("new")

            variant = type(board).xboard_variant
            if variant == "normal" and board.chess960:
                self._variant("fischerandom")
            elif variant != "normal":
                self._variant(variant)

            if self.config.get("random"):
                self.send_line("random")
            if self.config.get("computer"):
                self.send_line("computer")

        self.send_line("force")

        if new_game:
            fen = root.fen()
            if variant != "normal" or fen != chess.STARTING_FEN or board.chess960:
                self.send_line("setboard {}".format(root.shredder_fen() if board.chess960 else fen))

        # Undo moves until common position.
        common_stack_len = 0
        if not new_game:
            for left, right in zip(self.board.move_stack, board.move_stack):
                if left == right:
                    common_stack_len += 1
                else:
                    break

            while len(self.board.move_stack) > common_stack_len + 1:
                self.send_line("remove")
                self.board.pop()
                self.board.pop()

            while len(self.board.move_stack) > common_stack_len:
                self.send_line("undo")
                self.board.pop()

        # Play moves from board stack.
        for move in board.move_stack[common_stack_len:]:
            self.send_line(self.board.xboard(move))
            self.board.push(move)

    @asyncio.coroutine
    def ping(self):
        class Command(BaseCommand):
            def start(self, engine):
                n = id(self) & 0xffff
                self.pong = "pong {}".format(n)
                engine._ping(n)

            def line_received(self, engine, line):
                if line == self.pong:
                    self.set_finished()
                elif not line.startswith("#"):
                    LOGGER.warning("%s: Unexpected engine output: %s", engine, line)

        return (yield from self.communicate(Command))

    @asyncio.coroutine
    def play(self, board, limit, *, game=None, info=INFO_NONE, ponder=False, root_moves=None, options={}):
        previous_config = self.config.copy()

        if root_moves is not None:
            raise EngineError("play with root_moves, but xboard supports include only in analysis mode")

        class Command(BaseCommand):
            def start(self, engine):
                self.info = {}
                self.stopped = False
                self.final_pong = None
                self.draw_offered = False

                # Set game, position and configure.
                engine._new(board, game, options)

                # Limit or time control.
                increment = limit.white_inc if board.turn else limit.black_inc
                if limit.remaining_moves or increment:
                    base_mins, base_secs = divmod(int(limit.white_clock if board.turn else limit.black_clock), 60)
                    engine.send_line("level {} {}:{02d} {}".format(limit.remaining_moves or 0, base_mins, base_secs, increment))

                if limit.nodes is not None:
                    if limit.time is not None or limit.white_clock is not None or limit.black_clock is not None or increment is not None:
                        raise EngineError("xboard does not support mixing node limits with time limits")

                    if "nps" not in engine.features:
                        LOGGER.warning("%s: Engine did not declare explicit support for node limits (feature nps=?)")
                    elif not engine.features["nps"]:
                        raise EngineError("xboard engine does not support node limits (feature nps=0)")

                    engine.send_line("nps 100")
                    engine.send_line("st {}".format(int(limit.nodes)))
                if limit.depth is not None:
                    engine.send_line("sd {}".format(limit.depth))
                if limit.time is not None:
                    engine.send_line("st {}".format(int(limit.time * 100)))
                if limit.white_clock is not None:
                    engine.send_line("{} {}".format("time" if board.turn else "otim", int(limit.white_clock * 100)))
                if limit.black_clock is not None:
                    engine.send_line("{} {}".format("otim" if board.turn else "time", int(limit.black_clock * 100)))

                # Start thinking.
                engine.send_line("post" if info else "nopost")
                engine.send_line("hard" if ponder else "easy")
                engine.send_line("go")

            def line_received(self, engine, line):
                if line.startswith("move "):
                    self._move(engine, line.split(" ", 1)[1])
                elif line == self.final_pong:
                    if not self.result.done():
                        self.result.set_exception(EngineError("xboard engine answered final pong before sending move"))
                    self.end(engine)
                elif line == "offer draw":
                    self.draw_offered = True
                elif line == "resign":
                    self.result.set_exception(EngineError("xboard engine resigned"))
                    self.end(engine)
                elif line.startswith("1-0") or line.startswith("0-1") or line.startswith("1/2-1/2"):
                    if not self.result.done():
                        self.result.set_result(PlayResult(None, None, self.info, self.draw_offered))
                    self.end(engine)
                elif line.startswith("#") or line.startswith("Hint:"):
                    pass
                elif len(line.split()) >= 4 and line.lstrip()[0].isdigit():
                    self._post(engine, line)
                else:
                    LOGGER.warning("%s: Unexpected engine output: %s", engine, line)

            def _post(self, engine, line):
                if not self.result.done():
                    self.info = _parse_xboard_post(line, engine.board, info)

            def _move(self, engine, arg):
                if not self.result.cancelled():
                    try:
                        move = engine.board.push_xboard(arg)
                    except ValueError as err:
                        self.result.set_exception(EngineError(err))

                    self.result.set_result(PlayResult(move, None, self.info, self.draw_offered))

                if not ponder:
                    self.end(engine)

            def cancel(self, engine):
                if self.stopped:
                    return
                self.stopped = True

                if self.result.cancelled():
                    engine.send_line("?")

                if ponder:
                    engine.send_line("easy")

                    n = id(self) & 0xffff
                    self.final_pong = "pong {}".format(n)
                    engine._ping(n)

            def end(self, engine):
                if not self.finished.done():
                    engine._configure(previous_config)
                    for name, option in engine.options.items():
                        if name not in previous_config and option.default is not None:
                            engine._configure({name: option.default})

                    self.set_finished()

        return (yield from self.communicate(Command))

    @asyncio.coroutine
    def analysis(self, board, limit=None, *, multipv=None, game=None, info=INFO_ALL, root_moves=None, options={}):
        previous_config = self.config.copy()

        if multipv is not None:
            raise EngineError("xboard engine does not support multipv")

        if limit is not None and (limit.white_clock is not None or limit.black_clock is not None):
            raise EngineError("xboard analysis does not support clock limits")

        class Command(BaseCommand):
            def start(self, engine):
                self.stopped = False
                self.analysis = AnalysisResult(stop=lambda: self.cancel(engine))
                self.final_pong = None

                engine._new(board, game, options)

                if root_moves is not None:
                    if not engine.features.get("exclude", 0):
                        raise EngineError("xboard engine does not support root_moves (feature exclude=0)")

                    engine.send_line("exclude all")
                    for move in root_moves:
                        engine.send_line("include {}".format(engine.board.xboard(move)))

                engine.send_line("post")
                engine.send_line("analyze")

                self.result.set_result(self.analysis)

                if limit is not None and limit.time is not None:
                    self.time_limit_handle = engine.loop.call_later(limit.time, lambda: self.cancel(engine))
                else:
                    self.time_limit_handle = None

            def line_received(self, engine, line):
                if line.startswith("#"):
                    pass
                elif len(line.split()) >= 4 and line.lstrip()[0].isdigit():
                    self._post(engine, line)
                elif line == self.final_pong:
                    self.end(engine)
                else:
                    LOGGER.warning("%s: Unexpected engine output: %s", engine, line)

            def _post(self, engine, line):
                post_info = _parse_xboard_post(line, engine.board, info | INFO_BASIC)
                self.analysis.post(post_info)

                if limit is not None:
                    if limit.time is not None and post_info.get("time", 0) >= limit.time:
                        self.cancel(engine)
                    elif limit.nodes is not None and post_info.get("nodes", 0) >= limit.nodes:
                        self.cancel(engine)
                    elif limit.depth is not None and post_info.get("depth", 0) >= limit.depth:
                        self.cancel(engine)
                    elif limit.mate is not None and "score" in post_info:
                        if post_info["score"].relative >= limit.mate:
                            self.cancel(engine)

            def end(self, engine):
                if self.time_limit_handle:
                    self.time_limit_handle.cancel()

                self.analysis.set_finished()

                engine._configure(previous_config)
                for name, option in engine.options.items():
                    if name not in previous_config and option.default is not None:
                        engine._configure({name: option.default})

                self.set_finished()

            def cancel(self, engine):
                if self.stopped:
                    return
                self.stopped = True

                engine.send_line(".")
                engine.send_line("exit")

                n = id(self) & 0xffff
                self.final_pong = "pong {}".format(n)
                engine._ping(n)

            def engine_terminated(self, engine, exc):
                LOGGER.debug("%s: Closing analysis because engine has been terminated (error: %s)", engine, exc)

                if self.time_limit_handle:
                    self.time_limit_handle.cancel()

                self.analysis.set_exception(exc)

        return (yield from self.communicate(Command))

    def _configure(self, options):
        for name, value in options.items():
            if value is not None and self.config.get(name) == value:
                continue

            try:
                option = self.options[name]
            except KeyError:
                raise EngineError("unsupported xboard option or command: {}".format(name))

            self.config[name] = value = option.parse(value)

            if name in ["random", "computer"]:
                pass
            elif name in ["memory", "cores"] or name.startswith("egtpath "):
                self.send_line("{} {}".format(name, value))
            elif value is None:
                self.send_line("option {}".format(name))
            elif value is True:
                self.send_line("option {}=1".format(name))
            elif value is False:
                self.send_line("option {}=0".format(name))
            else:
                self.send_line("option {}={}".format(name, value))

    @asyncio.coroutine
    def configure(self, options):
        class Command(BaseCommand):
            def start(self, engine):
                engine._configure(options)
                self.set_finished()

        return (yield from self.communicate(Command))

    @asyncio.coroutine
    def quit(self):
        self.send_line("quit")
        yield from self.returncode


def _parse_xboard_option(feature):
    params = feature.split()

    name = params[0]
    type = params[1][1:]
    default = None
    min = None
    max = None
    var = None

    if type == "combo":
        var = []
        choices = params[2:]
        for choice in choices:
            if choice == "///":
                continue
            elif choice[0] == "*":
                default = choice[1:]
                var.append(choice[1:])
            else:
                var.append(choice)
    elif type == "check":
        default = int(params[2])
    elif type in ["string", "file", "path"]:
        if len(params) > 2:
            default = params[2]
        else:
            default = ""
    elif type == "spin":
        default = int(params[2])
        min = int(params[3])
        max = int(params[4])

    return Option(name, type, default, min, max, var)


def _parse_xboard_post(line, root_board, selector=INFO_ALL):
    # Format: depth score time nodes [seldepth [nps [tbhits]]] pv
    info = {}

    # Split leading integer tokens from pv.
    pv_tokens = line.split()
    integer_tokens = []
    while pv_tokens:
        token = pv_tokens.pop(0)
        try:
            integer_tokens.append(int(token))
        except ValueError:
            pv_tokens.insert(0, token)
            break

    if len(integer_tokens) < 4 or not selector:
        return info

    # Required integer tokens.
    info["depth"] = integer_tokens.pop(0)
    cp = integer_tokens.pop(0)
    info["time"] = float(integer_tokens.pop(0)) / 100
    info["nodes"] = int(integer_tokens.pop(0))

    # Score.
    if cp <= -100000:
        score = Mate(cp + 100000)
    elif cp == 100000:
        score = MateGiven
    elif cp >= 100000:
        score = Mate(cp - 100000)
    else:
        score = Cp(cp)
    info["score"] = PovScore(score, root_board.turn)

    # Optional integer tokens.
    if integer_tokens:
        info["seldepth"] = integer_tokens.pop(0)
    if integer_tokens:
        info["nps"] = integer_tokens.pop(0)

    while len(integer_tokens) > 1:
        # Reserved for future extensions.
        integer_tokens.pop(0)

    if integer_tokens:
        info["tbhits"] = integer_tokens.pop(0)

    # Principal variation.
    if not (selector & INFO_PV):
        return info

    info["pv"] = []
    board = root_board.copy(stack=False)
    for token in pv_tokens:
        if token.rstrip(".").isdigit():
            continue

        try:
            info["pv"].append(board.push_xboard(token))
        except ValueError:
            break

    return info


class AnalysisResult:
    """
    Handle to ongoing engine analysis.

    Can be used to asynchronously iterate over information sent by the engine.

    Automatically stops the analysis when used as a context manager.

    Returned by :func:`chess.engine.EngineProtocol.analysis()`.
    """
    def __init__(self, stop=None):
        self._stop = stop
        self._queue = asyncio.Queue()
        self._seen_kork = False
        self._finished = asyncio.Future()
        self.multipv = [{}]

    def post(self, info):
        multipv = info.get("multipv", 1)
        while len(self.multipv) < multipv:
            self.multipv.append({})
        self.multipv[multipv - 1].update(info)

        self._queue.put_nowait(info)

    def set_finished(self):
        self._queue.put_nowait(KORK)
        self._finished.set_result(None)

    def set_exception(self, exc):
        self._queue.put_nowait(KORK)
        self._finished.set_exception(exc)

    @property
    def info(self):
        return self.multipv[0]

    def stop(self):
        """Stops the analysis as soon as possible."""
        if self._stop and not self._finished.done():
            self._stop()
            self._stop = None

    @asyncio.coroutine
    def wait(self):
        """Waits until the analysis is complete (or stopped)."""
        yield from self._finished

    def __aiter__(self):
        return self

    @asyncio.coroutine
    def next(self):
        """
        Waits for the next dictionary of information from the engine and
        returns it. Returns ``None`` if the analysis has been stopped and
        all information has been consumed.

        It might be more convenient to use ``async for info in analysis``
        (requires at least Python 3.5).
        """
        try:
            return (yield from self.__anext__())
        except StopAsyncIteration:
            return None

    @asyncio.coroutine
    def __anext__(self):
        if self._seen_kork:
            raise StopAsyncIteration

        info = yield from self._queue.get()
        if info is KORK:
            self._seen_kork = True
            yield from self._finished
            raise StopAsyncIteration

        return info

    def __enter__(self):
        return self

    def __exit__(self, a, b, c):
        self.stop()


@asyncio.coroutine
def popen_uci(command, *, setpgrp=False, **popen_args):
    """
    Spawns and initializes an UCI engine.

    :param command: Path of the engine executable, or a list including the
        path and arguments.
    :param setpgrp: Open the engine process in a new process group. This will
        stop signals (such as keyboard interrupts) from propagating from the
        parent process. Defaults to ``False``.
    :param popen_args: Additional arguments for
        `popen <https://docs.python.org/3/library/subprocess.html#popen-constructor>`_.
        Do not set ``stdin``, ``stdout``, ``bufsize`` or
        ``universal_newlines``.

    Returns a subprocess transport and engine protocol pair.
    """
    return (yield from UciProtocol.popen(command, setpgrp=setpgrp, **popen_args))


@asyncio.coroutine
def popen_xboard(command, *, setpgrp=False, **popen_args):
    """
    Spawns and initializes an XBoard engine.

    :param command: Path of the engine executable, or a list including the
        path and arguments.
    :param setpgrp: Open the engine process in a new process group. This will
        stop signals (such as keyboard interrupts) from propagating from the
        parent process. Defaults to ``False``.
    :param popen_args: Additional arguments for
        `popen <https://docs.python.org/3/library/subprocess.html#popen-constructor>`_.
        Do not set ``stdin``, ``stdout``, ``bufsize`` or
        ``universal_newlines``.

    Returns a subprocess transport and engine protocol pair.
    """
    return (yield from XBoardProtocol.popen(command, setpgrp=setpgrp, **popen_args))


class SimpleEngine:
    """
    Synchronous wrapper around a transport and engine protocol pair. Provides
    the same methods and attributes as :class:`~chess.engine.EngineProtocol`,
    with blocking functions instead of coroutines.

    Methods will raise :class:`asyncio.TimeoutError` if an operation takes
    *timeout* seconds longer than expected (unless *timeout* is ``None``).

    Automatically closes the transport when used as a context manager.

    You may not concurrently modify objects passed to any of the methods. Other
    than that :class:`~chess.engine.SimpleEngine` is thread-safe. When sending
    a new command to the engine, any previous running command will be cancelled
    as soon as possible.
    """

    def __init__(self, transport, protocol, *, timeout=10.0):
        self.transport = transport
        self.protocol = protocol
        self.timeout = timeout

        self._shutdown_lock = threading.Lock()
        self._shutdown = False
        self._shutdown_event = asyncio.Event()

    def _timeout_for(self, limit):
        if self.timeout is None or limit is None or limit.time is None:
            return None
        return self.timeout + limit.time

    @contextlib.contextmanager
    def _not_shut_down(self):
        with self._shutdown_lock:
            if self._shutdown:
                raise EngineTerminatedError("engine event loop dead")
            yield

    @property
    def options(self):
        @asyncio.coroutine
        def _get():
            return self.protocol.options.copy()

        with self._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(_get(), self.protocol.loop)
        return future.result()

    @property
    def id(self):
        @asyncio.coroutine
        def _get():
            return self.protocol.id.copy()

        with self._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(_get(), self.protocol.loop)
        return future.result()

    def configure(self, options):
        with self._not_shut_down():
            coro = asyncio.wait_for(self.protocol.configure(options), self.timeout)
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def ping(self):
        with self._not_shut_down():
            coro = asyncio.wait_for(self.protocol.ping(), self.timeout)
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def play(self, board, limit, *, game=None, info=INFO_NONE, ponder=False, root_moves=None, options={}):
        with self._not_shut_down():
            coro = asyncio.wait_for(self.protocol.play(board, limit, game=game, info=info, ponder=ponder, root_moves=root_moves, options=options), self._timeout_for(limit))
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def analyse(self, board, limit, *, multipv=None, game=None, info=INFO_ALL, root_moves=None, options={}):
        with self._not_shut_down():
            coro = asyncio.wait_for(self.protocol.analyse(board, limit, multipv=multipv, game=game, info=info, root_moves=root_moves, options=options), self._timeout_for(limit))
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def analysis(self, board, limit=None, *, multipv=None, game=None, info=INFO_ALL, root_moves=None, options={}):
        with self._not_shut_down():
            coro = asyncio.wait_for(self.protocol.analysis(board, limit, multipv=multipv, game=game, info=info, root_moves=root_moves, options=options), self.timeout)
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return SimpleAnalysisResult(self, future.result())

    def quit(self):
        with self._not_shut_down():
            coro = asyncio.wait_for(self.protocol.quit(), self.timeout)
            future = asyncio.run_coroutine_threadsafe(coro, self.protocol.loop)
        return future.result()

    def close(self):
        """Closes the transport and the background event loop."""
        with self._shutdown_lock:
            if not self._shutdown:
                self._shutdown = True
                self.protocol.loop.call_soon_threadsafe(lambda: (self.transport.close(), self._shutdown_event.set()))

    @classmethod
    def popen(cls, Protocol, command, *, timeout=10.0, debug=False, setpgrp=False, **popen_args):
        @asyncio.coroutine
        def background(future):
            transport, protocol = yield from asyncio.wait_for(Protocol.popen(command, setpgrp=setpgrp, **popen_args), timeout)
            simple_engine = cls(transport, protocol, timeout=timeout)
            future.set_result(simple_engine)
            yield from protocol.returncode
            simple_engine.close()
            yield from simple_engine._shutdown_event.wait()

        return run_in_background(background, debug=debug)

    @classmethod
    def popen_uci(cls, command, *, timeout=10.0, debug=False, setpgrp=False, **popen_args):
        """
        Spawns and initializes an UCI engine.
        Returns a :class:`~chess.engine.SimpleEngine` instance.
        """
        return cls.popen(UciProtocol, command, timeout=timeout, debug=debug, setpgrp=setpgrp, **popen_args)

    @classmethod
    def popen_xboard(cls, command, *, timeout=10.0, debug=False, setpgrp=False, **popen_args):
        """
        Spawns and initializes an XBoard engine.
        Returns a :class:`~chess.engine.SimpleEngine` instance.
        """
        return cls.popen(XBoardProtocol, command, timeout=timeout, debug=debug, setpgrp=setpgrp, **popen_args)

    def __enter__(self):
        return self

    def __exit__(self, a, b, c):
        self.close()

    def __repr__(self):
        pid = self.transport.get_pid()  # This happens to be thread-safe.
        return "<{} (pid={})>".format(type(self).__name__, pid)


class SimpleAnalysisResult:
    """
    Synchronous wrapper around :class:`~chess.engine.AnalysisResult`. Returned
    by :func:`chess.engine.SimpleEngine.analysis()`.
    """

    def __init__(self, simple_engine, inner):
        self.simple_engine = simple_engine
        self.inner = inner

    @property
    def info(self):
        @asyncio.coroutine
        def _get():
            return self.inner.info.copy()

        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(_get(), self.simple_engine.protocol.loop)
        return future.result()

    @property
    def multipv(self):
        @asyncio.coroutine
        def _get():
            return [info.copy() for info in self.inner.multipv]

        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(_get(), self.simple_engine.protocol.loop)
        return future.result()

    def stop(self):
        with self.simple_engine._not_shut_down():
            self.simple_engine.protocol.loop.call_soon_threadsafe(self.inner.stop)

    def wait(self):
        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(self.inner.wait(), self.simple_engine.protocol.loop)
        return future.result()

    def next(self):
        with self.simple_engine._not_shut_down():
            future = asyncio.run_coroutine_threadsafe(self.inner.next(), self.simple_engine.protocol.loop)
        return future.result()

    def __iter__(self):
        with self.simple_engine._not_shut_down():
            self.simple_engine.protocol.loop.call_soon_threadsafe(self.inner.__aiter__)
        return self

    def __next__(self):
        try:
            with self.simple_engine._not_shut_down():
                future = asyncio.run_coroutine_threadsafe(self.inner.__anext__(), self.simple_engine.protocol.loop)
            return future.result()
        except StopAsyncIteration:
            raise StopIteration

    def __enter__(self):
        return self

    def __exit__(self, a, b, c):
        self.stop()
