"""The paid x402 route, off unless the deployment switches it on.

The free `/v1/score` answer stays free. This module adds a second route that answers
the same question after a USDC payment on Base, using the x402 `exact` scheme. It is
how a paying agent with no account can still call Zapproof, and it is what the x402
catalogs (Bazaar, x402scan, x402-list) require before they will index the service.

Two boundaries this module keeps:

* No key and no wallet live here. `pay_to` is a public address the owner controls;
  the facilitator moves the money. This server only verifies and forwards.
* The paid answer is not cacheable. The free route publishes a 60-second cache and
  an ETag; a paid resource that an intermediary can serve for free defeats itself,
  so the wrapper forces `no-store` and drops the ETag.

Environment (all read once at import time by the application):

  ZAPPROOF_X402_ENABLED=1        install the route at all
  ZAPPROOF_X402_PAY_TO           public, checksummed EVM address (required)
  ZAPPROOF_X402_NETWORK          eip155:8453 (default) or eip155:84532 for rehearsal
  ZAPPROOF_X402_PRICE            default "$0.005" - see the price note below
  ZAPPROOF_X402_FACILITATOR      optional facilitator URL override
  ZAPPROOF_X402_ICON_URL         optional catalog icon URL

Price note: $0.005, not $0.001. CDP charges $0.001 per on-chain settlement after the
free tier, so a $0.001 price would hand every later payment to the facilitator.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any

from eth_utils import is_checksum_address  # type: ignore[attr-defined]
from fastapi import FastAPI, Request, Response
from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
from x402.http import HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer

PAID_PREFIX = "/v1/paid"
PAID_PATH = PAID_PREFIX + "/score/{identifier}"
# The middleware matches Express-style parameters, not FastAPI's braces.
PAID_ROUTE_KEY = "GET " + PAID_PREFIX + "/score/:identifier"

MAINNET = "eip155:8453"
TESTNET = "eip155:84532"
DEFAULT_NETWORK = MAINNET
DEFAULT_PRICE = "$0.005"
DEFAULT_ICON = "https://raw.githubusercontent.com/noareyenoar/zapproof/main/assets/logo.png"
TESTNET_FACILITATOR = "https://x402.org/facilitator"
# The public facilitator serves testnets only; mainnet needs CDP (or another
# facilitator configured explicitly).
_NETWORKS = (MAINNET, TESTNET)
_PRICE = re.compile(r"^\$\d+(?:\.\d{1,6})?$")
_DESCRIPTION = (
    "Settled-payment evidence for one EVM or Solana address, as a rules-based "
    "interpretation with its limits stated. Unknown is returned as unknown."
)
_EXAMPLE = {
    "identifier": "0x0000000000000000000000000000000000000000",
    "found": False,
    "status": "unknown",
    "score": None,
    "note": "This is not a low score. This address is unknown, not bad.",
}


class PaidConfigError(ValueError):
    """The paid route is switched on but cannot be configured safely."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def paid_config() -> dict[str, str]:
    """Read and validate the paid-route settings. Raises before serving anything."""
    pay_to = _env("ZAPPROOF_X402_PAY_TO")
    if not pay_to:
        raise PaidConfigError(
            "ZAPPROOF_X402_PAY_TO is required: the public address that receives the "
            "payment. It must be checksummed; convert it once with "
            "python -c 'from eth_utils import to_checksum_address; "
            "print(to_checksum_address(\"0x...\"))'."
        )
    if not is_checksum_address(pay_to):
        raise PaidConfigError(
            "ZAPPROOF_X402_PAY_TO must be an EIP-55 checksummed address. A "
            "lowercase address has no typo protection, and a mistyped payTo sends "
            "payments to nobody."
        )

    network = _env("ZAPPROOF_X402_NETWORK", DEFAULT_NETWORK)
    if network not in _NETWORKS:
        raise PaidConfigError(
            f"ZAPPROOF_X402_NETWORK must be one of {', '.join(_NETWORKS)}; got {network!r}"
        )

    price = _env("ZAPPROOF_X402_PRICE", DEFAULT_PRICE)
    if not _PRICE.match(price):
        raise PaidConfigError(
            f'ZAPPROOF_X402_PRICE must look like "$0.005" (US dollars, up to 6 '
            f"decimals); got {price!r}"
        )

    return {
        "pay_to": pay_to,
        "network": network,
        "price": price,
        "facilitator": _env("ZAPPROOF_X402_FACILITATOR"),
        "icon": _env("ZAPPROOF_X402_ICON_URL", DEFAULT_ICON),
    }


def facilitator_config(network: str, url: str = "") -> Any:
    """Where settlement happens. Mainnet without an override means Coinbase CDP."""
    if url:
        return {"url": url}
    if network == TESTNET:
        return {"url": TESTNET_FACILITATOR}
    missing = [name for name in ("CDP_API_KEY_ID", "CDP_API_KEY_SECRET") if not _env(name)]
    if missing:
        raise PaidConfigError(
            "Base mainnet settlement needs CDP credentials ("
            + ", ".join(missing)
            + "), or set ZAPPROOF_X402_FACILITATOR to another facilitator. There is "
            "no free mainnet facilitator that also feeds the Coinbase catalog."
        )
    from cdp.x402 import create_facilitator_config

    return create_facilitator_config()


def install_paid_endpoint(
    app: FastAPI, handler: Callable[[str, Request], Response]
) -> None:
    """Add the paid route and its payment middleware to an existing app.

    `handler` is the free route's handler: the paid answer is the same evidence, so
    there is one implementation and no second story about what a score means.
    """
    config = paid_config()
    server = x402ResourceServer(HTTPFacilitatorClient(facilitator_config(
        config["network"], config["facilitator"]
    )))
    # The SDK's own annotations disagree with its runtime: ExactEvmServerScheme does
    # not structurally match SchemeNetworkServer (payment_flows is typed as a dict of
    # configs but holds flow phase names), and the class is untyped. The prototype
    # against the real facilitator answered the same 402 this test asserts.
    server.register(config["network"], ExactEvmServerScheme())  # type: ignore[arg-type,no-untyped-call]

    routes = {
        PAID_ROUTE_KEY: RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    pay_to=config["pay_to"],
                    price=config["price"],
                    network=config["network"],
                    max_timeout_seconds=60,
                )
            ],
            mime_type="application/json",
            description=_DESCRIPTION,
            service_name="Zapproof",
            tags=["reputation", "agents", "base", "x402"],
            icon_url=config["icon"],
            extensions=declare_discovery_extension(
                path_params_schema={
                    "properties": {"identifier": {"type": "string"}},
                    "required": ["identifier"],
                },
                output=OutputConfig(example=_EXAMPLE),
            ),
        )
    }
    app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)

    def paid_score(identifier: str, request: Request) -> Response:
        response = handler(identifier, request)
        # A paid answer must not be served again from a cache, and its ETag would
        # let a caller revalidate without paying.
        response.headers["Cache-Control"] = "no-store"
        if "etag" in response.headers:
            del response.headers["etag"]
        return response

    app.add_api_route(PAID_PATH, paid_score, methods=["GET"], response_model=None)
