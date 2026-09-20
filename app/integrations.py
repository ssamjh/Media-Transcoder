"""Small, dependency-free outbound integrations for Sonarr, Radarr and AutoPulse.

The clients in this module deliberately accept plain values and an injectable
``opener``/``sleep`` pair.  That keeps integration work out of the durable
outbox and makes the HTTP contract straightforward to test without a third
party HTTP library.
"""

from __future__ import annotations

import base64
import json
import posixpath
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping


class IntegrationError(RuntimeError):
    """Base class for an outbound integration failure."""


class IntegrationHTTPError(IntegrationError):
    """The remote endpoint returned a non-2xx HTTP response."""

    def __init__(self, status: int, url: str, body: str = "") -> None:
        self.status = int(status)
        self.url = url
        self.body = body
        detail = body.strip()
        super().__init__(f"HTTP {self.status} from {url}" + (f": {detail}" if detail else ""))


class IntegrationProtocolError(IntegrationError):
    """A successful response did not satisfy the API's JSON contract."""


class CommandFailedError(IntegrationError):
    """An Arr command reported a failed/cancelled status."""

    def __init__(self, command: Mapping[str, Any]) -> None:
        self.command = dict(command)
        status = command.get("status", "unknown")
        error = command.get("errorMessage") or command.get("message") or ""
        super().__init__(f"Arr command failed ({status})" + (f": {error}" if error else ""))


class CommandTimeoutError(IntegrationError):
    """An Arr command did not finish before the configured deadline."""

    def __init__(self, command_id: Any, timeout: float) -> None:
        self.command_id = command_id
        self.timeout = timeout
        super().__init__(f"Arr command {command_id!r} did not finish within {timeout:g}s")


@dataclass(frozen=True)
class PathMapping:
    """A prefix mapping between the path visible to Arr and the local path.

    ``remote`` is the Arr-side prefix and ``local`` is the worker-side prefix.
    The longest matching prefix wins, and separators are respected so ``/tv2``
    cannot accidentally match ``/tv20``.
    """

    remote: str
    local: str

    def to_local(self, path: str) -> str:
        return _replace_prefix(path, self.remote, self.local)

    def to_remote(self, path: str) -> str:
        return _replace_prefix(path, self.local, self.remote)


@dataclass(frozen=True)
class RenameResult:
    """Result of renaming one Arr file."""

    final_path: str
    source_file_id: Any
    preview: Mapping[str, Any]
    command: Mapping[str, Any]

    @property
    def path(self) -> str:
        """Convenient alias used by outbox callers."""

        return self.final_path

    def __str__(self) -> str:
        return self.final_path

    def __getitem__(self, key: str) -> Any:
        """Allow lightweight dict-style use by outbox serializers."""

        if key in {"path", "final_path"}:
            return self.final_path
        if key in {"source_file_id", "file_id", "id"}:
            return self.source_file_id
        if key == "preview":
            return self.preview
        if key == "command":
            return self.command
        raise KeyError(key)


def _replace_prefix(path: str, old: str, new: str) -> str:
    path = str(path)
    old = str(old).rstrip("/\\")
    if not old:
        return path
    # Arr is HTTP/JSON but paths can be Windows paths.  Compare separators in
    # a portable way while retaining the spelling returned by the API.
    candidate = path.replace("\\", "/")
    source = old.replace("\\", "/")
    if candidate == source:
        suffix = ""
    elif candidate.startswith(source + "/"):
        suffix = candidate[len(source):]
    else:
        return path
    replacement = str(new).rstrip("/\\").replace("\\", "/")
    return replacement + suffix


def _normalise_path(path: str) -> str:
    value = str(path).replace("\\", "/")
    # Preserve a leading slash and drive-letter paths, but collapse duplicate
    # separators and dot segments for reliable preview matching.
    drive = ""
    if len(value) >= 2 and value[1] == ":":
        drive, value = value[:2], value[2:]
    leading = value.startswith("/")
    value = posixpath.normpath(value)
    if leading and not value.startswith("/"):
        value = "/" + value
    return (drive + value).casefold()


def _is_absolute_path(path: str) -> bool:
    value = str(path).replace("\\", "/")
    return value.startswith("/") or value.startswith("//") or (len(value) >= 2 and value[1] == ":")


def _path_candidates(path: str, mappings: Iterable[PathMapping]) -> set[str]:
    values = {str(path)}
    for mapping in mappings:
        values.add(mapping.to_local(str(path)))
        values.add(mapping.to_remote(str(path)))
    return {_normalise_path(v) for v in values}


def _as_mappings(value: Any) -> tuple[PathMapping, ...]:
    if not value:
        return ()
    if isinstance(value, Mapping):
        value = [value]
    result: list[PathMapping] = []
    for item in value:
        if isinstance(item, PathMapping):
            result.append(item)
        elif isinstance(item, Mapping):
            remote = item.get("remote", item.get("arr", item.get("from")))
            local = item.get("local", item.get("to"))
            if remote is None or local is None:
                raise ValueError("path mapping requires remote and local")
            result.append(PathMapping(str(remote), str(local)))
        else:
            try:
                remote, local = item
            except (TypeError, ValueError) as exc:
                raise ValueError("path mapping must be PathMapping, mapping, or (remote, local)") from exc
            result.append(PathMapping(str(remote), str(local)))
    return tuple(result)


def _open(opener: Callable[..., Any], request: urllib.request.Request, timeout: float) -> Any:
    """Call either ``urlopen``-style functions or OpenerDirector objects."""

    target = getattr(opener, "open", opener)
    try:
        return target(request, timeout=timeout)
    except TypeError as exc:
        # Tiny test doubles often expose ``open(request)`` only.  Retry that
        # form, while leaving ordinary network/protocol errors untouched.
        try:
            return target(request)
        except TypeError:
            raise exc


class ArrClient:
    """HTTP client for the common Sonarr/Radarr v3 APIs.

    ``provider`` is ``"sonarr"`` or ``"radarr"``.  ``opener`` may be
    ``urllib.request.urlopen`` or any callable accepting ``(Request, timeout)``
    and returning a response with ``status``, ``read`` and optionally ``close``.
    """

    provider = ""
    entity_parameter = ""
    rescan_command = ""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        provider: str | None = None,
        timeout: float = 30.0,
        command_timeout: float = 300.0,
        poll_interval: float = 2.0,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], Any] | None = None,
        path_mappings: Iterable[Any] = (),
        path_mapping: Iterable[Any] | None = None,
    ) -> None:
        selected = (provider or self.provider).strip().lower()
        if selected not in {"sonarr", "radarr"}:
            raise ValueError("provider must be sonarr or radarr")
        self.provider = selected
        self.entity_parameter = "seriesId" if selected == "sonarr" else "movieId"
        self.rescan_command = "RescanSeries" if selected == "sonarr" else "RescanMovie"
        self.base_url = str(base_url).rstrip("/")
        self.api_key = str(api_key)
        self.timeout = float(timeout)
        self.command_timeout = float(command_timeout)
        self.poll_interval = max(0.0, float(poll_interval))
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep or time.sleep
        self.path_mappings = _as_mappings(path_mapping if path_mapping is not None else path_mappings)

    def _url(self, endpoint: str, params: Mapping[str, Any] | None = None) -> str:
        url = self.base_url + "/" + endpoint.lstrip("/")
        if params:
            query = urllib.parse.urlencode([(k, v) for k, v in params.items() if v is not None], doseq=True)
            if query:
                url += "?" + query
        return url

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        payload: Any = None,
        require_json: bool = True,
    ) -> Any:
        url = self._url(endpoint, params)
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json", "X-Api-Key": self.api_key}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            response = _open(self.opener, request, self.timeout)
            try:
                status = int(getattr(response, "status", getattr(response, "code", 200)))
                raw = response.read()
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except Exception:
                raw = b""
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            raise IntegrationHTTPError(exc.code, url, text) from exc
        except urllib.error.URLError as exc:
            raise IntegrationError(f"HTTP request to {url} failed: {exc.reason}") from exc
        except OSError as exc:
            raise IntegrationError(f"HTTP request to {url} failed: {exc}") from exc

        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        if status < 200 or status >= 300:
            raise IntegrationHTTPError(status, url, text)
        if not text.strip():
            return None
        if not require_json:
            return text
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise IntegrationProtocolError(f"Invalid JSON from {url}: {text[:200]}") from exc

    @staticmethod
    def _command_id(command: Any) -> Any:
        if not isinstance(command, Mapping) or command.get("id") is None:
            raise IntegrationProtocolError("Arr command response did not contain an id")
        return command["id"]

    def wait_for_command(
        self,
        command_id: Any,
        *,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> Mapping[str, Any]:
        limit = self.command_timeout if timeout is None else max(0.0, float(timeout))
        interval = self.poll_interval if poll_interval is None else max(0.0, float(poll_interval))
        started = time.monotonic()
        while True:
            command = self._request("GET", f"/api/v3/command/{urllib.parse.quote(str(command_id), safe='')}")
            if not isinstance(command, Mapping):
                raise IntegrationProtocolError("Arr command status was not a JSON object")
            status = str(command.get("status", "")).lower()
            if status in {"completed", "complete", "success", "succeeded"}:
                return command
            if status in {"failed", "aborted", "cancelled", "canceled", "error"}:
                raise CommandFailedError(command)
            if time.monotonic() - started >= limit:
                raise CommandTimeoutError(command_id, limit)
            self.sleep(interval)

    def _start_command(self, payload: Mapping[str, Any], *, timeout: float | None, poll_interval: float | None) -> Mapping[str, Any]:
        started = self._request("POST", "/api/v3/command", payload=dict(payload))
        return self.wait_for_command(self._command_id(started), timeout=timeout, poll_interval=poll_interval)

    def start_rescan(
        self,
        entity_id: int | str,
        *,
        wait: bool = True,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> Mapping[str, Any]:
        payload = {"name": self.rescan_command, self.entity_parameter: entity_id}
        started = self._request("POST", "/api/v3/command", payload=payload)
        if not wait:
            if not isinstance(started, Mapping):
                raise IntegrationProtocolError("Arr rescan response was not a JSON object")
            return started
        return self.wait_for_command(self._command_id(started), timeout=timeout, poll_interval=poll_interval)

    # Names used by callers that think in terms of synchronization rather than
    # Arr's command terminology.
    rescan = start_rescan
    trigger_rescan = start_rescan

    def _preview(self, entity_id: int | str) -> list[Mapping[str, Any]]:
        value = self._request("GET", "/api/v3/rename", params={self.entity_parameter: entity_id})
        if isinstance(value, list):
            records = value
        elif isinstance(value, Mapping):
            records = value.get("records", value.get("items", value.get("files", [])))
        else:
            records = []
        if not isinstance(records, list) or any(not isinstance(item, Mapping) for item in records):
            raise IntegrationProtocolError("Arr rename preview was not a list of objects")
        return records

    def _choose_preview(
        self,
        records: list[Mapping[str, Any]],
        source_file_id: Any,
        existing_path: str | None,
    ) -> Mapping[str, Any]:
        if source_file_id is not None:
            wanted = str(source_file_id)
            matches = [r for r in records if any(str(r.get(k)) == wanted for k in ("id", "fileId", "movieFileId", "episodeFileId", "sourceFileId"))]
            # Changing container can make Arr replace its file record during
            # the rescan, so the ID received on import is no longer present.
            # Fall back to the post-transcode path in that case.
            if matches:
                if len(matches) != 1:
                    raise IntegrationProtocolError(
                        "Could not select one Arr rename preview record: multiple matching files")
                return matches[0]
        if existing_path:
            wanted = _path_candidates(existing_path, self.path_mappings)
            matches = []
            for record in records:
                values = [record.get(k) for k in ("existingPath", "path", "originalPath", "filePath")]
                candidates = set().union(*(
                    _path_candidates(str(v), self.path_mappings)
                    for v in values if v is not None))
                if any(a == b or a.endswith("/" + b.lstrip("/"))
                       or b.endswith("/" + a.lstrip("/"))
                       for a in candidates for b in wanted):
                    matches.append(record)
        elif len(records) == 1:
            matches = records
        else:
            matches = []
        if len(matches) != 1:
            reason = "no matching file" if not matches else "multiple matching files"
            raise IntegrationProtocolError(f"Could not select one Arr rename preview record: {reason}")
        return matches[0]

    @staticmethod
    def _record_file_id(record: Mapping[str, Any], requested: Any = None) -> Any:
        keys = ("id", "fileId", "movieFileId", "episodeFileId", "sourceFileId")
        recorded = [record[key] for key in keys if record.get(key) is not None]
        # A rescan after a container change can replace Arr's file record.
        # When preview selection fell back from the webhook's stale ID to the
        # new path, RenameFiles must use the new preview ID, not the old one.
        if requested is not None and any(str(value) == str(requested)
                                         for value in recorded):
            return requested
        if recorded:
            return recorded[0]
        if requested is not None:
            return requested
        raise IntegrationProtocolError("Arr rename preview record did not contain a source file id")

    @staticmethod
    def _record_path(record: Mapping[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = record.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def rename_affected(
        self,
        entity_id: int | str,
        *,
        source_file_id: Any = None,
        existing_path: str | None = None,
        final_path: str | None = None,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> RenameResult:
        records = self._preview(entity_id)
        if not records:
            if not final_path:
                raise IntegrationProtocolError("Arr rename preview was empty and no final path was provided")
            resolved = self.path_to_local(final_path)
            return RenameResult(resolved, source_file_id, {}, {})
        record = self._choose_preview(records, source_file_id,
                                      final_path or existing_path)
        file_id = self._record_file_id(record, source_file_id)
        command = self._start_command(
            {"name": "RenameFiles", self.entity_parameter: entity_id, "files": [file_id]},
            timeout=timeout,
            poll_interval=poll_interval,
        )
        resolved = self._record_path(
            command,
            "finalPath", "newPath", "path", "filePath", "movieFilePath",
        ) or self._record_path(record, "newPath", "newFilePath", "finalPath", "renamedPath")
        if not resolved:
            # A few Arr versions return only the command status.  The preview
            # is still authoritative about the path it intends to produce.
            resolved = final_path or self._record_path(record, "path", "existingPath", "filePath")
        if not resolved:
            raise IntegrationProtocolError("Arr rename response did not contain a final path")
        if not _is_absolute_path(resolved):
            known_path = existing_path or final_path
            preview_existing = self._record_path(record, "existingPath", "path", "originalPath", "filePath")
            if known_path:
                known_remote = self.path_to_remote(known_path).replace("\\", "/")
                if preview_existing:
                    preview_norm = preview_existing.replace("\\", "/").lstrip("./")
                    known_norm = known_remote.lstrip("./")
                    if known_norm.casefold().endswith(preview_norm.casefold()):
                        root = known_remote[:len(known_remote) - len(preview_norm)].rstrip("/")
                    else:
                        root = known_remote.rsplit("/", 1)[0]
                else:
                    root = known_remote.rsplit("/", 1)[0]
                resolved = (root.rstrip("/") + "/" + resolved.lstrip("./\\/")) if root else resolved
        resolved = self.path_to_local(resolved)
        return RenameResult(resolved, file_id, record, command)

    rename = rename_affected

    def path_to_local(self, path: str) -> str:
        value = str(path)
        for mapping in sorted(self.path_mappings, key=lambda item: len(item.remote), reverse=True):
            converted = mapping.to_local(value)
            if converted != value:
                return converted
        return value

    def path_to_remote(self, path: str) -> str:
        value = str(path)
        for mapping in sorted(self.path_mappings, key=lambda item: len(item.local), reverse=True):
            converted = mapping.to_remote(value)
            if converted != value:
                return converted
        return value

    def system_status(self) -> Any:
        """Cheapest authenticated call an Arr offers.

        Used by the panel's Test button: it proves the URL resolves, the
        port is right and the API key is accepted, without asking the Arr to
        do any work.
        """
        return self._request("GET", "/api/v3/system/status")

    def reconcile(
        self,
        entity_id: int | str,
        source_file_id: Any = None,
        original_path: str | None = None,
        final_path: str | None = None,
        *,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> RenameResult:
        """Rescan an entity, then rename precisely one affected file."""

        self.start_rescan(entity_id, timeout=timeout, poll_interval=poll_interval)
        return self.rename_affected(
            entity_id,
            source_file_id=source_file_id,
            existing_path=original_path,
            final_path=final_path,
            timeout=timeout,
            poll_interval=poll_interval,
        )


class SonarrClient(ArrClient):
    provider = "sonarr"


class RadarrClient(ArrClient):
    provider = "radarr"


class JellyfinClient:
    """Tell Jellyfin that one media path was created or modified."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0,
                 opener: Callable[..., Any] | None = None) -> None:
        self.url = str(base_url).rstrip("/") + "/Library/Media/Updated"
        self.api_key = str(api_key)
        self.timeout = float(timeout)
        self.opener = opener or urllib.request.urlopen

    def update(self, path: str, update_type: str = "Modified") -> None:
        data = json.dumps({"Updates": [{"Path": str(path),
                                        "UpdateType": update_type}]}).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=data, method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "Authorization": f'MediaBrowser Token="{self.api_key}"'})
        try:
            response = _open(self.opener, request, self.timeout)
            try:
                status = int(getattr(response, "status", getattr(response, "code", 200)))
                raw = response.read()
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        except urllib.error.HTTPError as exc:
            raise IntegrationHTTPError(exc.code, self.url, exc.read().decode(
                "utf-8", errors="replace")) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise IntegrationError(f"HTTP request to {self.url} failed: {exc}") from exc
        if status < 200 or status >= 300:
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            raise IntegrationHTTPError(status, self.url, text)


class AutoPulseClient:
    """Trigger AutoPulse's manual import endpoint.

    AutoPulse documents ``GET /triggers/manual?path=...&hash=...``.  The
    method/endpoint remain configurable for older deployments that exposed a
    JSON POST endpoint.
    """

    def __init__(
        self,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        *,
        auth: tuple[str, str] | None = None,
        endpoint: str = "/triggers/manual",
        method: str = "GET",
        timeout: float = 30.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.endpoint = endpoint
        self.method = method.upper()
        self.timeout = float(timeout)
        self.opener = opener or urllib.request.urlopen
        if auth is not None:
            username, password = auth
        self.username = username
        self.password = password

    def trigger(
        self,
        path: str,
        file_hash: str | None = None,
        *,
        hash: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"path": str(path)}
        value = file_hash if file_hash is not None else hash
        if value is not None:
            payload["hash"] = value
        url = self.base_url + "/" + self.endpoint.lstrip("/")
        if self.method == "GET":
            url += "?" + urllib.parse.urlencode(payload)
            data = None
        else:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {}),
                **self._auth_header(),
            },
            method=self.method,
        )
        try:
            response = _open(self.opener, request, self.timeout)
            try:
                status = int(getattr(response, "status", getattr(response, "code", 200)))
                raw = response.read()
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except Exception:
                raw = b""
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            raise IntegrationHTTPError(exc.code, url, text) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise IntegrationError(f"HTTP request to {url} failed: {exc}") from exc
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        if status < 200 or status >= 300:
            raise IntegrationHTTPError(status, url, text)
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    manual_trigger = trigger
    trigger_manual = trigger

    def _auth_header(self) -> dict[str, str]:
        if self.username is None:
            return {}
        token = base64.b64encode(f"{self.username}:{self.password or ''}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}"}


__all__ = [
    "ArrClient", "AutoPulseClient", "JellyfinClient", "CommandFailedError", "CommandTimeoutError",
    "IntegrationError", "IntegrationHTTPError", "IntegrationProtocolError",
    "PathMapping", "RadarrClient", "RenameResult", "SonarrClient",
]
