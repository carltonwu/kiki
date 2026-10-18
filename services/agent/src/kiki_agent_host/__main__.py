import asyncio
import os
import socket
import sys

import uvicorn

from .logs import configure_logging


def main() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        pass
    configure_logging()
    if sys.argv[1:] == ["index"]:
        from .retrieval import index_all

        asyncio.run(index_all(os.environ["DATABASE_URL"], os.environ["OPENROUTER_CHAT_API_KEY"]))
        return
    # asyncio's default IPv6 listener is IPv6-only. Explicit dual-stack binding
    # also accepts IPv4 health checks and local Docker port forwarding.
    with socket.create_server(
        ("::", int(os.getenv("PORT", "8081"))),
        family=socket.AF_INET6,
        dualstack_ipv6=True,
    ) as listener:
        uvicorn.run(
            "kiki_agent_host.app:application",
            factory=True,
            fd=listener.fileno(),
            workers=1,
            access_log=False,
            log_config=None,
            timeout_graceful_shutdown=20,
        )


if __name__ == "__main__":
    main()
