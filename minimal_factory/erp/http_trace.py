"""把 ERP→MES 实际 HTTP 请求与响应打印到后台日志，供终端观察。"""

import logging
from functools import partial
from http.client import HTTPConnection
from urllib.request import HTTPHandler


logger = logging.getLogger("uvicorn.error")


def log_lines(message_id, direction, text):
    for line in text.splitlines() or [""]:
        logger.info("[ERP->MES %s] %s %s", message_id, direction, line)


class TracedHTTPConnection(HTTPConnection):
    """记录本下发连接实际发送的 UTF-8 JSON 与 HTTP 头。"""

    def __init__(self, *args, message_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.message_id = message_id

    def send(self, data):
        super().send(data)
        # http.client 已经生成请求行与全部请求头，随后单独发送 JSON 字节。
        log_lines(self.message_id, ">", data.decode("utf-8", errors="replace"))

    def getresponse(self):
        response = super().getresponse()
        version = {9: "0.9", 10: "1.0", 11: "1.1"}.get(response.version, str(response.version))
        log_lines(self.message_id, "<", f"HTTP/{version} {response.status} {response.reason}")
        for name, value in response.getheaders():
            log_lines(self.message_id, "<", f"{name}: {value}")
        log_lines(self.message_id, "<", "")
        return response


class TraceHTTPHandler(HTTPHandler):
    def __init__(self, message_id):
        super().__init__()
        self.message_id = message_id

    def http_open(self, request):
        connection = partial(TracedHTTPConnection, message_id=self.message_id)
        return self.do_open(connection, request)


def read_response_body(response, message_id):
    body = response.read()
    log_lines(message_id, "<", body.decode("utf-8", errors="replace"))
    return body
