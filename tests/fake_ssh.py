"""Stand-in for `ssh` in the remote runtime tests.

    fake_ssh.py --server-config PATH --user NAME [ssh options] DESTINATION COMMAND...

It runs the server command directly with SSH_ORIGINAL_COMMAND set, the way sshd
runs a key's forced command. Both ends are on this machine, so a reverse
forward `-R 127.0.0.1:0:127.0.0.1:PORT` is reported as "Allocated port PORT":
the server reaches the bridge on the PC's own port.

FAKE_SSH_RECORD: file to append {"argv": [...]} records to.
"""

import json
import os
import subprocess
import sys

WITH_VALUE = {"-o", "-i", "-R", "-L", "-p", "-l", "-F", "-E", "-J", "-b", "-c", "-m"}


def main(argv):
    config = user = None
    while argv and argv[0] in ("--server-config", "--user"):
        if argv[0] == "--server-config":
            config = argv[1]
        else:
            user = argv[1]
        argv = argv[2:]
    record = os.environ.get("FAKE_SSH_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv}) + "\n")
    if "-V" in argv:
        print("OpenSSH_fake for tests", file=sys.stderr)
        return 0
    forward = None
    index = 0
    while index < len(argv) and argv[index].startswith("-"):
        option = argv[index]
        if option in WITH_VALUE:
            if option == "-R":
                forward = argv[index + 1]
            index += 2
        else:
            index += 1
    if index >= len(argv):
        print("fake ssh: no destination", file=sys.stderr)
        return 255
    command = argv[index + 1:]
    if forward:
        local_port = forward.rsplit(":", 1)[1]
        print(f"Allocated port {local_port} for remote forward to 127.0.0.1:{local_port}", file=sys.stderr, flush=True)
    env = dict(os.environ, SSH_ORIGINAL_COMMAND=" ".join(command))
    server = [sys.executable, "-m", "cli_subagents.remote.server", "--user", user, "--config", config]
    return subprocess.call(server, env=env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
