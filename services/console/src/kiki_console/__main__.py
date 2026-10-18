import logging
import os
import socket

import uvicorn


def main() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        pass
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Dual-stack, like the agent host: Railway's private network is IPv6.
    with socket.create_server(
        ("::", int(os.getenv("PORT", "8095"))), family=socket.AF_INET6, dualstack_ipv6=True
    ) as listener:
        uvicorn.run(
            "kiki_console.app:application",
            factory=True,
            fd=listener.fileno(),
            workers=1,
            access_log=False,
            log_config=None,
        )


if __name__ == "__main__":
    main()
