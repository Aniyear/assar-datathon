"""Streaming multipart reader: one ``file`` part written to disk with a hard size cap."""
from __future__ import annotations

from pathlib import Path

from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect, Request

MULTIPART_OVERHEAD = 64 * 1024  # Boundaries, part headers and small extra fields.


class UploadRejected(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def receive_mov(request: Request, dest: Path, max_bytes: int) -> int:
    """Write the ``file`` field to ``dest``; return its size. Raises UploadRejected."""
    ctype, params = parse_options_header(request.headers.get("content-type"))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise UploadRejected(415, "Expected multipart/form-data with a .mov file field")
    length = request.headers.get("content-length")
    limit = max_bytes + MULTIPART_OVERHEAD
    if length is not None and length.isdigit() and int(length) > limit:
        raise UploadRejected(413, "Upload exceeds the size limit")

    state = {"field": b"", "value": b"", "headers": {}, "in_file": False, "seen": False, "size": 0}
    out = dest.open("wb")

    def on_part_begin() -> None:
        state.update(field=b"", value=b"", headers={}, in_file=False)

    def on_header_field(data: bytes, start: int, end: int) -> None:
        state["field"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        state["value"] += data[start:end]

    def on_header_end() -> None:
        state["headers"][state["field"].lower()] = state["value"]
        state["field"], state["value"] = b"", b""

    def on_headers_finished() -> None:
        _, disp = parse_options_header(state["headers"].get(b"content-disposition"))
        if disp.get(b"name") != b"file":
            return
        if state["seen"]:
            raise UploadRejected(422, "Exactly one file field is expected")
        filename = disp.get(b"filename", b"").decode("utf-8", "replace")
        if not filename.lower().endswith(".mov"):
            raise UploadRejected(415, "Only .mov files are accepted")
        state["seen"] = state["in_file"] = True

    def on_part_data(data: bytes, start: int, end: int) -> None:
        if not state["in_file"]:
            return
        state["size"] += end - start
        if state["size"] > max_bytes:
            raise UploadRejected(413, "Upload exceeds the size limit")
        out.write(data[start:end])

    parser = MultipartParser(boundary, {
        "on_part_begin": on_part_begin, "on_header_field": on_header_field,
        "on_header_value": on_header_value, "on_header_end": on_header_end,
        "on_headers_finished": on_headers_finished, "on_part_data": on_part_data,
    }, max_size=limit)
    try:
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                raise UploadRejected(413, "Upload exceeds the size limit")
            parser.write(chunk)
        parser.finalize()
    except UploadRejected:
        raise
    except ClientDisconnect:
        raise UploadRejected(422, "Upload was interrupted") from None
    except Exception:  # noqa: BLE001 - malformed multipart; details stay local.
        raise UploadRejected(422, "Malformed multipart body") from None
    finally:
        out.close()
    if not state["seen"]:
        raise UploadRejected(422, "Missing file field")
    if state["size"] == 0:
        raise UploadRejected(422, "Uploaded file is empty")
    return state["size"]
