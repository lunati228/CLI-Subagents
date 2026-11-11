"""A tiny stdio MCP server for the bridge proxy tests.

Tools: echo(text) answers with the text; slow(seconds, text) waits first;
needs_client sends the client a sampling request, then answers with the reply
it got. Requests are handled on threads, so slow calls overlap.
"""

import json
import sys
import threading
import time

lock = threading.Lock()
waiting = {}


def send(message):
    with lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def handle(message):
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-stdio", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in ("echo", "slow", "needs_client")]}
    elif method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments") or {}
        if name == "slow":
            time.sleep(float(arguments.get("seconds", 1)))
        if name == "needs_client":
            event = threading.Event()
            waiting["s1"] = [event, None]
            send({"jsonrpc": "2.0", "id": "s1", "method": "sampling/createMessage", "params": {}})
            event.wait(10)
            text = json.dumps(waiting["s1"][1])
        else:
            text = str(arguments.get("text", ""))
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    else:
        send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "no such method"}})
        return
    send({"jsonrpc": "2.0", "id": message["id"], "result": result})


for line in sys.stdin:
    if not line.strip():
        continue
    message = json.loads(line)
    if "method" not in message:  # a reply to our request
        entry = waiting.get(message.get("id"))
        if entry:
            entry[1] = message
            entry[0].set()
        continue
    if "id" in message:
        threading.Thread(target=handle, args=(message,), daemon=True).start()
