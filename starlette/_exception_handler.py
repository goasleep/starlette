import typing

from starlette._utils import is_async_callable
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import (
    ASGIApp,
    ExceptionHandler,
    Message,
    Receive,
    Scope,
    Send,
)
from starlette.websockets import WebSocket

ExceptionHandlers = typing.Dict[typing.Any, ExceptionHandler]
StatusHandlers = typing.Dict[int, ExceptionHandler]


def _lookup_exception_handler(
    exc_handlers: ExceptionHandlers, exc: Exception
) -> typing.Optional[ExceptionHandler]:
    # 查找与给定异常实例 exc 相关的异常处理程序函数。
    # 它首先遍历 exc 的类层次结构（即方法解析顺序 __mro__），
    # 如果找到了一个匹配的类，则返回与该类关联的异常处理程序函数
    for cls in type(exc).__mro__:
        if cls in exc_handlers:
            return exc_handlers[cls]
    return None


def wrap_app_handling_exceptions(
    app: ASGIApp, conn: typing.Union[Request, WebSocket]
) -> ASGIApp:
    exception_handlers: ExceptionHandlers
    status_handlers: StatusHandlers
    try:
        exception_handlers, status_handlers = conn.scope["starlette.exception_handlers"]
    except KeyError:
        exception_handlers, status_handlers = {}, {}

    async def wrapped_app(scope: Scope, receive: Receive, send: Send) -> None:
        response_started = False

        async def sender(message: Message) -> None:
            # 用于跟踪响应是否已经开始
            # ASGI 应用程序中，响应是通过发送 http.response.start 消息来开始的
            # 响应开始，就不能再发送 http.response.start 消息，因为这会导致协议错误
            nonlocal response_started

            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await app(scope, receive, sender)
        except Exception as exc:
            handler = None

            if isinstance(exc, HTTPException):
                handler = status_handlers.get(exc.status_code)

            if handler is None:
                # 查询exc的所有的异常处理器，包括其父处理器，
                handler = _lookup_exception_handler(exception_handlers, exc)

            if handler is None:
                raise exc

            if response_started:
                msg = "Caught handled exception, but response already started."
                raise RuntimeError(msg) from exc

            # 当捕获到异常时，我们首先判断当前请求的类型是否为 "http"，
            # 如果是，则说明当前请求是一个 HTTP 请求，需要返回一个 HTTP 响应。
            # 如果不是，则说明当前请求不是 HTTP 请求，可能是 WebSocket 请求或其他类型的请求，需要进行其他处理
            if scope["type"] == "http":
                if is_async_callable(handler):
                    response = await handler(conn, exc)
                else:
                    response = await run_in_threadpool(handler, conn, exc)
                await response(scope, receive, sender)
            elif scope["type"] == "websocket":
                if is_async_callable(handler):
                    await handler(conn, exc)
                else:
                    await run_in_threadpool(handler, conn, exc)

    return wrapped_app
