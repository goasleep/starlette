import typing

if typing.TYPE_CHECKING:
    # 检查模式下导入类型
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.websockets import WebSocket

AppType = typing.TypeVar("AppType")

# 表示当前请求的上下文信息，例如请求的方法、路径、协议等；
# scope example：
# {
#     "type": "http",
#     "http_version": "1.1",
#     "method": "GET",
#     "scheme": "http",
#     "path": "/",
#     "query_string": b"",
#     "headers": [
#         (b"host", b"localhost:8000"),
#         (b"cookie", b"csrftoken=abc123"),
#     ],
#     "client": ("127.0.0.1", 12345),
#     "server": ("127.0.0.1", 8000),
#     "asgi": {"version": "3.0"},
# }
Scope = typing.MutableMapping[str, typing.Any]

# message example
# {
#     "type": "http.request",
#     "body": b"Hello, world!",
#     "more_body": False,
#     "asgi": {"version": "3.0"}
# }
Message = typing.MutableMapping[str, typing.Any]

# 表示接收请求数据的异步函数
Receive = typing.Callable[[], typing.Awaitable[Message]]
# Send 表示发送响应数据的异步函数
Send = typing.Callable[[Message], typing.Awaitable[None]]

ASGIApp = typing.Callable[[Scope, Receive, Send], typing.Awaitable[None]]

StatelessLifespan = typing.Callable[[AppType], typing.AsyncContextManager[None]]
StatefulLifespan = typing.Callable[
    [AppType], typing.AsyncContextManager[typing.Mapping[str, typing.Any]]
]
Lifespan = typing.Union[StatelessLifespan[AppType], StatefulLifespan[AppType]]

HTTPExceptionHandler = typing.Callable[
    ["Request", Exception],
    typing.Union["Response", typing.Awaitable["Response"]],
]
WebSocketExceptionHandler = typing.Callable[
    ["WebSocket", Exception], typing.Awaitable[None]
]
ExceptionHandler = typing.Union[HTTPExceptionHandler, WebSocketExceptionHandler]
