"""Small, process-local Anvil harness for EVM integration tests.

This deliberately does not join the Subtensor docker-compose project. EVM
tests start their own Anvil process on an ephemeral loopback port, mine only
when instructed, and tear it down when their fixture exits.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
from dataclasses import dataclass
from typing import Final, cast

import httpx

READY_DEADLINE_SECONDS: Final[float] = 15.0
READY_POLL_SECONDS: Final[float] = 0.05


@dataclass(frozen=True, slots=True)
class EvmBlock:
    """The identity and base fee of one Anvil block."""

    number: int
    hash: str
    base_fee_wei: int


def _unused_loopback_port() -> int:
    """Reserve an ephemeral port long enough to discover its number."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class EvmLocalChain:
    """Driver for a disposable, manually-mined Anvil node."""

    def __init__(
        self,
        *,
        chain_id: int,
        external_rpc_env: str | None = None,
    ) -> None:
        if chain_id <= 0:
            raise ValueError("chain_id must be positive")
        external_rpc = os.environ.get(external_rpc_env) if external_rpc_env else None
        self.chain_id = chain_id
        self._external = external_rpc is not None
        self.port = 8545 if self._external else _unused_loopback_port()
        self.rpc_url = external_rpc or f"ws://127.0.0.1:{self.port}"
        self.http_url = self.rpc_url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0

    async def start(self) -> None:
        """Start Anvil and wait for a successful Ethereum JSON-RPC response."""

        if self._external:
            await self.wait_until_ready()
            return
        anvil = shutil.which("anvil")
        if anvil is None:
            raise RuntimeError(
                "anvil is required for EVM integration tests; "
                "install Foundry from https://getfoundry.sh"
            )
        self._process = await asyncio.create_subprocess_exec(
            anvil,
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--chain-id",
            str(self.chain_id),
            "--no-mining",
            "--quiet",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self.wait_until_ready()

    async def stop(self) -> None:
        """Terminate Anvil, escalating to kill if it does not exit promptly."""

        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def wait_until_ready(self) -> None:
        """Wait until Anvil answers ``eth_chainId`` or fail with useful context."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + READY_DEADLINE_SECONDS
        last_error: BaseException | None = None
        while loop.time() < deadline:
            process = self._process
            if process is not None and process.returncode is not None:
                raise RuntimeError(f"anvil exited before becoming ready ({process.returncode})")
            try:
                chain_id = await self.rpc("eth_chainId")
                if chain_id == hex(self.chain_id):
                    return
            except (httpx.HTTPError, RuntimeError) as exc:
                last_error = exc
            await asyncio.sleep(READY_POLL_SECONDS)
        raise TimeoutError(
            f"anvil RPC did not become ready within {READY_DEADLINE_SECONDS:.0f}s: {last_error!r}"
        )

    async def rpc(self, method: str, params: list[object] | None = None) -> object:
        """Call one JSON-RPC method over HTTP and return its result."""

        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or [],
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(self.http_url, json=request)
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"invalid JSON-RPC response for {method}: {payload!r}")
        error = payload.get("error")
        if error is not None:
            raise RuntimeError(f"JSON-RPC {method} failed: {error!r}")
        return payload.get("result")

    async def mine(self, *, base_fee_wei: int) -> EvmBlock:
        """Mine one block with an exact base fee and return its identity."""

        if base_fee_wei < 0:
            raise ValueError("base_fee_wei must be non-negative")
        await self.rpc("anvil_setNextBlockBaseFeePerGas", [hex(base_fee_wei)])
        await self.rpc("evm_mine")
        raw_block = await self.rpc("eth_getBlockByNumber", ["latest", False])
        if not isinstance(raw_block, dict):
            raise RuntimeError(f"eth_getBlockByNumber returned {raw_block!r}")
        block = cast("dict[str, object]", raw_block)
        return EvmBlock(
            number=int(str(block["number"]), 16),
            hash=str(block["hash"]),
            base_fee_wei=int(str(block["baseFeePerGas"]), 16),
        )
