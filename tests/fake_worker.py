"""A stand-in for the trading bot process, used to test the controller for real (subprocess + HTTP).

Behaviour is driven by the environment / actions:
  FAKE_POSITION=1    report an open position (blocks mode switches)
  FAKE_HANG=1        never write a heartbeat
  FAKE_EXIT=<code>   exit immediately with that code (e.g. 2 = config error, 3 = crash)
  action "stop"      graceful stop: exit note + exit code 0
  action "crash"     exit with code 3
  action "maint"     exit with code 75 (daily maintenance restart)
"""

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from topstep_bot.service import write_exit_note  # noqa: E402
from topstep_bot.web import HttpServer, Request  # noqa: E402

MODE = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "paper"


async def main() -> int:
    if os.environ.get("FAKE_EXIT"):
        code = int(os.environ["FAKE_EXIT"])
        write_exit_note(code, "fake configuration problem" if code == 2 else "fake crash")
        return code
    done: asyncio.Future = asyncio.get_running_loop().create_future()
    position = 1 if os.environ.get("FAKE_POSITION") == "1" else 0

    def status(_: Request) -> dict:
        return {"bot": {"mode": MODE, "position": position, "trade": None, "pid": os.getpid()}, "log": {"errors": 0, "warnings": 0, "recent": []}}

    def action(req: Request) -> dict | str:
        name, payload = req.param, req.json()
        if name == "stop":
            write_exit_note(0, f"stop requested from {payload.get('source')}")
            done.set_result(0)
            return "stopping"
        if name in ("crash", "maint"):
            done.set_result(3 if name == "crash" else 75)
            return "bye"
        if name == "status_text":
            return {"text": f"fake bot in {MODE} mode"}
        if name == "bad":
            raise ValueError("bad request from fake")
        return {"echo": name, "payload": payload}

    server = HttpServer("127.0.0.1", int(os.environ["TOPSTEP_BOT_API_PORT"]),
                        [("GET", "/status", status), ("POST", "/action/*", action)],
                        token=os.environ["TOPSTEP_BOT_API_TOKEN"], token_for_reads=True, name="fake bot")
    await server.start()
    heartbeat = Path(os.environ["TOPSTEP_BOT_HEARTBEAT"])

    async def beat():
        while True:
            if os.environ.get("FAKE_HANG") != "1":
                heartbeat.write_text(str(time.time()))
            await asyncio.sleep(0.2)

    task = asyncio.create_task(beat())
    code = await done
    await asyncio.sleep(0.1)  # let the HTTP response go out
    task.cancel()
    await server.stop()
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
