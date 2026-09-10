"""Deploy the CircuitPython tree over a USB serial raw REPL and verify SHA-256."""

import argparse
import base64
import hashlib
import pathlib
import sys
import time

import serial

from run_raw_diagnostic import enter_raw_repl, send_chunked


def raw_exec(port, source, timeout=20.0):
    port.reset_input_buffer()
    send_chunked(port, source.encode("utf-8"))
    port.write(b"\x04")
    port.flush()
    deadline = time.monotonic() + timeout
    response = bytearray()
    while time.monotonic() < deadline:
        waiting = port.in_waiting
        if waiting:
            response.extend(port.read(waiting))
            if b"\x04>" in response:
                break
        else:
            time.sleep(0.01)
    else:
        raise RuntimeError("Timed out executing raw command: {!r}".format(bytes(response[-500:])))
    if not response.startswith(b"OK"):
        raise RuntimeError("Raw command rejected: {!r}".format(bytes(response[-500:])))
    stdout, stderr = bytes(response[2:]).split(b"\x04", 1)
    stderr = stderr.split(b"\x04", 1)[0]
    if stderr:
        raise RuntimeError(stderr.decode("utf-8", "replace"))
    return stdout.decode("utf-8", "replace").strip()


def remote_sha256(port, relative):
    verify_source = (
        "import hashlib,binascii\n"
        "h=hashlib.new('sha256')\n"
        "f=open({!r},'rb')\n"
        "while True:\n"
        " d=f.read(1024)\n"
        " if not d: break\n"
        " h.update(d)\n"
        "f.close()\n"
        "print(binascii.hexlify(h.digest()).decode())"
    ).format(relative)
    return raw_exec(port, verify_source)


def deploy_file(port, source_root, local_path):
    relative = local_path.relative_to(source_root).as_posix()
    payload = local_path.read_bytes()
    local_hash = hashlib.sha256(payload).hexdigest()
    try:
        if remote_sha256(port, relative) == local_hash:
            return relative, len(payload), local_hash, "already verified"
    except RuntimeError:
        pass

    parent = pathlib.PurePosixPath(relative).parent
    current = pathlib.PurePosixPath()
    for part in parent.parts:
        current /= part
        raw_exec(
            port,
            "import os\ntry: os.mkdir({!r})\nexcept OSError: pass".format(str(current)),
        )

    temporary = relative + ".part"
    raw_exec(port, "f=open({!r},'wb')\nf.close()".format(temporary))
    for start in range(0, len(payload), 1536):
        encoded = base64.b64encode(payload[start:start + 1536]).decode("ascii")
        write_source = (
            "import binascii\n"
            "d=binascii.a2b_base64({!r})\n"
            "f=open({!r},'ab')\n"
            "f.write(d)\n"
            "f.close()"
        ).format(encoded, temporary)
        raw_exec(port, write_source)

    remote_hash = remote_sha256(port, temporary)
    if remote_hash != local_hash:
        raise RuntimeError("Hash mismatch for {}: {} != {}".format(temporary, remote_hash, local_hash))
    promote_source = (
        "import os\n"
        "try: os.remove({!r})\n"
        "except OSError: pass\n"
        "os.rename({!r},{!r})"
    ).format(relative, temporary, relative)
    raw_exec(port, promote_source)
    if remote_sha256(port, relative) != local_hash:
        raise RuntimeError("Post-promote hash mismatch for {}".format(relative))
    return relative, len(payload), local_hash, "uploaded"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=pathlib.Path)
    parser.add_argument("--port", default="COM7")
    parser.add_argument("--no-reset", action="store_true")
    parser.add_argument("--native-usb", action="store_true")
    args = parser.parse_args()
    source_root = args.source.resolve()
    files = sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in (".pyc", ".pyo")
    )
    # Upload the entry point last so an unexpected reset never launches a
    # partially updated dependency tree.
    files.sort(key=lambda path: path.relative_to(source_root).as_posix() == "code.py")
    print("[DEPLOY] {} files from {}".format(len(files), source_root), flush=True)

    port = serial.Serial(args.port, 115200, timeout=0.05, write_timeout=3)
    if not args.native_usb:
        port.dtr = False
        port.rts = False
    try:
        port.reset_input_buffer()
        enter_raw_repl(port)
        for index, path in enumerate(files, 1):
            relative, size, digest, action = deploy_file(port, source_root, path)
            print("[DEPLOY] {}/{} {} {} bytes {} ({})".format(index, len(files), relative, size, digest[:12], action), flush=True)
        print("[DEPLOY] All hashes verified", flush=True)
        if not args.no_reset:
            # Leave raw mode first; Ctrl-D alone reboots back into raw REPL.
            port.write(b"\x02")
            port.flush()
            time.sleep(0.15)
            port.write(b"\x04")
            port.flush()
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                data = port.read(port.in_waiting or 1)
                if data:
                    sys.stdout.write(data.decode("utf-8", "replace"))
                    sys.stdout.flush()
    finally:
        port.close()


if __name__ == "__main__":
    main()
