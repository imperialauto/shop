"""Imperial Local Node worker — runs on the shop workstation.

Standard-library Python only (3.9+). It:
  * talks to Ollama on 127.0.0.1 only,
  * makes OUTBOUND HTTPS calls to Imperial Operations (Railway) with its own
    worker key (never INTERNAL_API_KEY), and opens no listening port,
  * claims one diagnostic review at a time, runs the model, and reports the
    raw answer back. Railway validates the answer; this worker approves nothing.

Usage (normally started by run-worker.ps1, which supplies the key):
  python worker.py --config D:\\Imperial\\LocalNode\\config\\config.json            # run forever
  python worker.py --config ... --check   # commissioning checks, then exit
  python worker.py --config ... --eval    # run synthetic test cases on the real model
  python worker.py --config ... --once    # one heartbeat + at most one task

Key: environment variable LOCAL_NODE_WORKER_KEY.
"""
import argparse
import ipaddress
import json
import logging
import logging.handlers
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.1.0"
log = logging.getLogger("imperial-local-node")

DEFAULTS = {
    "backendUrl": "",
    "workerId": "",
    "ollamaUrl": "http://127.0.0.1:11434",
    "pollSeconds": 10,
    "heartbeatSeconds": 60,
    "requestTimeoutSeconds": 30,
    "ollamaTimeoutSeconds": 600,
    "logDir": "",
}


class ConfigError(Exception):
    pass


class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def load_config(path):
    with open(path, encoding="utf-8-sig") as handle:  # PowerShell 5.1 writes a BOM
        config = dict(DEFAULTS, **json.load(handle))
    backend = urllib.parse.urlparse(config["backendUrl"])
    local_backend = backend.hostname in ("127.0.0.1", "localhost")
    if backend.scheme != "https" and not (backend.scheme == "http" and local_backend):
        raise ConfigError("backendUrl must be https:// (plain http is only allowed for localhost testing)")
    ollama = urllib.parse.urlparse(config["ollamaUrl"])
    try:
        loopback = ollama.hostname == "localhost" or ipaddress.ip_address(ollama.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ConfigError("ollamaUrl must be a loopback address (127.0.0.1); Ollama must not be exposed")
    if not config["workerId"]:
        config["workerId"] = ("shop-" + socket.gethostname().lower())[:64]
    config["backendUrl"] = config["backendUrl"].rstrip("/")
    config["ollamaUrl"] = config["ollamaUrl"].rstrip("/")
    return config


def _dpapi_unprotect(blob: bytes) -> bytes:
    """Decrypt a Windows DPAPI blob for the current Windows user (CryptUnprotectData)."""
    if os.name != "nt":
        raise ConfigError("An encrypted key file can only be read on Windows")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(blob, len(blob))
    source = DATA_BLOB(len(blob), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)):
        raise ConfigError("Could not decrypt the worker key file for this Windows user (re-run install.ps1)")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def read_key_file(path: str) -> str:
    """Read the key saved by install.ps1 with ConvertFrom-SecureString (hex DPAPI blob of UTF-16LE text)."""
    with open(path, encoding="ascii") as handle:
        text = handle.read().strip()
    try:
        blob = bytes.fromhex(text)
    except ValueError:
        raise ConfigError("The worker key file is not in the expected format (re-run install.ps1)") from None
    return _dpapi_unprotect(blob).decode("utf-16-le")


def worker_key(key_file=None):
    key = read_key_file(key_file) if key_file else os.environ.get("LOCAL_NODE_WORKER_KEY", "")
    if len(key) < 32:
        raise ConfigError("Worker key is missing or too short (pass --key-file, or set LOCAL_NODE_WORKER_KEY)")
    return key


def http_json(method, url, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": f"imperial-local-node/{VERSION}", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code, exc.read().decode("utf-8", "replace")) from None


class Backend:
    def __init__(self, config, key):
        self.base = config["backendUrl"] + "/internal/local-node"
        self.headers = {"X-Local-Node-Key": key}
        self.worker_id = config["workerId"]
        self.timeout = config["requestTimeoutSeconds"]

    def call(self, method, path, body=None):
        return http_json(method, self.base + path, body, self.headers, self.timeout)

    def heartbeat(self, model, ollama_ok, detail=""):
        return self.call("POST", "/heartbeat", {"workerId": self.worker_id, "version": VERSION, "model": model,
                                                "ollamaOk": bool(ollama_ok), "detail": detail[:500]})

    def claim(self):
        return self.call("POST", "/claim", {"workerId": self.worker_id}).get("task")

    def result(self, task, model, output, duration_ms):
        return self.call("POST", f"/tasks/{task['taskId']}/result", {
            "workerId": self.worker_id, "leaseToken": task["leaseToken"], "model": model,
            "output": output[:60000], "durationMs": duration_ms})

    def error(self, task, message, retryable=True):
        return self.call("POST", f"/tasks/{task['taskId']}/error", {
            "workerId": self.worker_id, "leaseToken": task["leaseToken"],
            "error": message[:1000], "retryable": retryable})


class Ollama:
    def __init__(self, config):
        self.base = config["ollamaUrl"]
        self.timeout = config["ollamaTimeoutSeconds"]

    def version(self):
        return http_json("GET", self.base + "/api/version", timeout=5).get("version", "")

    def models(self):
        return [m.get("name", "") for m in http_json("GET", self.base + "/api/tags", timeout=10).get("models", [])]

    def chat(self, model, messages, schema, options):
        body = {"model": model, "messages": messages, "stream": False, "format": schema,
                "options": options or {}, "think": False, "keep_alive": "10m"}
        try:
            reply = http_json("POST", self.base + "/api/chat", body, timeout=self.timeout)
        except HttpError as exc:
            if exc.status == 400 and "think" in exc.body.lower():
                body.pop("think")  # older Ollama without the think switch
                reply = http_json("POST", self.base + "/api/chat", body, timeout=self.timeout)
            else:
                raise
        return (reply.get("message") or {}).get("content", "")


def ollama_health(ollama, model):
    try:
        ollama.version()
        installed = ollama.models()
    except Exception as exc:  # noqa: BLE001 - report, never crash the loop
        return False, f"Ollama unreachable: {type(exc).__name__}"
    if model and model not in installed and f"{model}:latest" not in installed:
        return False, f"Model {model} is not installed in Ollama"
    return True, "ok"


def process_one(backend, ollama):
    """Claim and run at most one task. Returns True if a task was handled."""
    task = backend.claim()
    if not task:
        return False
    model = task["model"]
    log.info("task %s claimed (attempt %s, prompt %s)", task["taskId"], task.get("attempt"), task.get("promptVersion"))
    started = time.monotonic()
    try:
        output = ollama.chat(model, task["messages"], task.get("format"), task.get("options"))
    except HttpError as exc:
        retryable = exc.status != 404  # 404 = model not found: retrying will not help
        log.warning("task %s: Ollama HTTP %s", task["taskId"], exc.status)
        backend.error(task, f"Ollama returned HTTP {exc.status}", retryable=retryable)
        return True
    except Exception as exc:  # noqa: BLE001 - timeouts, connection refused, etc.
        log.warning("task %s: Ollama failed: %s", task["taskId"], type(exc).__name__)
        backend.error(task, f"Ollama call failed: {type(exc).__name__}", retryable=True)
        return True
    duration = int((time.monotonic() - started) * 1000)
    verdict = backend.result(task, model, output, duration)
    if verdict.get("accepted"):
        log.info("task %s: draft accepted (%s ms)", task["taskId"], duration)
    else:
        log.warning("task %s: draft rejected by Railway checks: %s -> %s", task["taskId"],
                    ",".join(verdict.get("violations", [])), verdict.get("status"))
    return True


def run_forever(config, backend, ollama, once=False):
    model_hint = ""
    last_beat = 0.0
    failures = 0
    while True:
        try:
            if time.monotonic() - last_beat >= config["heartbeatSeconds"] or once:
                ok, detail = ollama_health(ollama, model_hint)
                reply = backend.heartbeat(model_hint, ok, detail)
                model_hint = reply.get("model", model_hint)
                last_beat = time.monotonic()
                if not ok:
                    log.warning("Ollama not ready: %s", detail)
            handled = False
            if ollama_health(ollama, "")[0]:
                handled = process_one(backend, ollama)
            failures = 0
            if once:
                return 0
            if handled:
                continue  # look for the next queued task right away
        except HttpError as exc:
            failures += 1
            if exc.status in (401, 403):
                log.error("Railway refused the worker key (HTTP %s). Check LOCAL_NODE_WORKER_KEY.", exc.status)
            elif exc.status == 409:
                log.warning("Lease lost (another worker or the job was cancelled); continuing.")
                failures = 0
            else:
                log.warning("Railway call failed: HTTP %s", exc.status)
            if once:
                return 1
        except Exception as exc:  # noqa: BLE001 - network down, DNS, TLS
            failures += 1
            log.warning("Railway unreachable: %s", type(exc).__name__)
            if once:
                return 1
        delay = config["pollSeconds"] if failures == 0 else min(300, config["pollSeconds"] * (2 ** min(failures, 5)))
        time.sleep(delay)


def run_check(config, backend, ollama):
    """Commissioning: prints PASS/FAIL lines; exit code 0 only if all pass."""
    results = []

    def record(name, ok, detail=""):
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")

    record("Python version", sys.version_info >= (3, 9), platform.python_version())
    try:
        record("Ollama reachable on loopback", True, "version " + ollama.version())
        installed = ollama.models()
    except Exception as exc:  # noqa: BLE001
        record("Ollama reachable on loopback", False, type(exc).__name__)
        installed = []
    model = ""
    try:
        reply = backend.heartbeat("", bool(installed), "commissioning check")
        model = reply.get("model", "")
        record("Railway reachable and worker key accepted", True, config["backendUrl"])
    except HttpError as exc:
        record("Railway reachable and worker key accepted", False, f"HTTP {exc.status}")
    except Exception as exc:  # noqa: BLE001
        record("Railway reachable and worker key accepted", False, type(exc).__name__)
    if model:
        present = model in installed or f"{model}:latest" in installed
        record(f"Model '{model}' installed", present, ", ".join(installed) or "no models found")
        if present:
            try:
                started = time.monotonic()
                answer = ollama.chat(model, [{"role": "user", "content": "Reply with the JSON object {\"ok\": true}"}],
                                     "json", {"temperature": 0})
                record("Model answers", '"ok"' in answer, f"{int((time.monotonic() - started) * 1000)} ms")
            except Exception as exc:  # noqa: BLE001
                record("Model answers", False, type(exc).__name__)
    return 0 if results and all(results) else 1


def run_eval(backend, ollama):
    """Run the synthetic commissioning cases on the real model; Railway grades them.
    A pass means the guardrails held, NOT that the diagnosis is right."""
    suite = backend.call("GET", "/eval-cases")
    failures = 0
    for case in suite["cases"]:
        started = time.monotonic()
        try:
            output = ollama.chat(suite["model"], case["messages"], suite["format"], {"temperature": 0.1, "num_ctx": 8192})
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {case['name']}: Ollama error {type(exc).__name__}")
            failures += 1
            continue
        verdict = backend.call("POST", "/validate", {"case": case["name"], "output": output})
        took = int((time.monotonic() - started) * 1000)
        status = "PASS" if verdict["ok"] else "FAIL"
        failures += 0 if verdict["ok"] else 1
        print(f"[{status}] {case['name']} ({took} ms) expected: {case['expect']}")
        for item in verdict["violations"]:
            print(f"        violation: {item}")
        print("        model output: " + " ".join(output.split())[:600])
    print("Guardrail checks passed." if not failures else f"{failures} case(s) failed guardrail checks.")
    return 0 if not failures else 1


class SingleInstance:
    """Refuse to start a second worker on the same machine."""

    def __init__(self, path):
        self.handle = open(path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise ConfigError("Another Imperial Local Node worker is already running on this computer.") from None


def _record_startup_error(config_path, message):
    """pythonw has no console: leave startup failures somewhere a person will find them."""
    try:
        logs = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(config_path))), "logs")
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "worker-startup-errors.log"), "a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except OSError:
        pass


def setup_logging(log_dir, to_console):
    log.setLevel(logging.INFO)
    log.propagate = False
    for existing in list(log.handlers):
        log.removeHandler(existing)
        existing.close()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(os.path.join(log_dir, "worker.log"), maxBytes=2_000_000,
                                                       backupCount=5, encoding="utf-8")
        handler.setFormatter(formatter)
        log.addHandler(handler)
    if to_console or not log_dir:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        log.addHandler(console)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Imperial Local Node worker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--key-file", help="DPAPI-encrypted key written by install.ps1 (Windows)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--eval", action="store_true")
    mode.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        key = worker_key(args.key_file)
    except (ConfigError, OSError, ValueError) as exc:
        message = f"Configuration error: {exc}"
        print(message, file=sys.stderr)
        _record_startup_error(args.config, message)
        return 2
    setup_logging(config["logDir"], to_console=args.check or args.eval or args.once)
    backend, ollama = Backend(config, key), Ollama(config)
    if args.check:
        return run_check(config, backend, ollama)
    if args.eval:
        return run_eval(backend, ollama)
    lock_dir = config["logDir"] or os.path.dirname(os.path.abspath(args.config))
    try:
        lock = SingleInstance(os.path.join(lock_dir, "worker.lock"))  # held for the process lifetime
    except ConfigError as exc:
        log.error(str(exc))
        return 3
    log.info("Imperial Local Node %s starting as %s -> %s (pid %s)", VERSION, config["workerId"], config["backendUrl"], os.getpid())
    try:
        return run_forever(config, backend, ollama, once=args.once)
    except KeyboardInterrupt:
        log.info("stopped")
        return 0
    finally:
        lock.handle.close()


if __name__ == "__main__":
    sys.exit(main())
