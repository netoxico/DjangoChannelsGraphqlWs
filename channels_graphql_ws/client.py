# Copyright (C) DATADVANCE, 2010-2023
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""GraphQL client."""


import asyncio
import textwrap
import time
import uuid

# Underscored to avoid name clash with GraphqlWsClient init's argument.
from . import transport as _transport


class GraphqlWsClient:
    """A client for the GraphQL WebSocket server.

    The client implements a WebSocket-based GraphQL protocol. It is the
    same protocol as the server implemented by the `GraphqlWsConsumer`
    class. See its docstring for details and for the protocol
    description.

    This class implements only the protocol itself. The implementation
    of the message delivery extracted into the separate interface
    `GraphqlWsTransport`. So it is possible to use this client with
    different network frameworks (e.g. Tornado, AIOHTTP).

    NOTE: The `receive` method retrieves the first response received by
    backend, when used with subscriptions it may either return
    subscription data or some query result. The response type must be
    checked outside the client manually.

    NOTE: In the comments in brackets near the type of messages in protocol
    `graphql-transport-ws`, an analogue of this message in protocol
    `graphql-ws` is specifieded.

    Args:
        transport: The `GraphqlWsTransport` instance used to send and
            receive messages over the WebSocket connection.
        subprotocol: WebSocket subprotocol to use by the Client. Can
            have a value of "graphql-transport-ws" or "graphql-ws".
            By default set to "graphql-transport-ws".
    """

    def __init__(
        self,
        transport: _transport.GraphqlWsTransport,
        subprotocol="graphql-transport-ws",
    ):
        """Constructor."""
        assert isinstance(
            transport, _transport.GraphqlWsTransport
        ), "Given transport does not implement the 'GraphqlWsTransport' interface!"
        self._transport = transport
        self._is_connected = False
        assert subprotocol in (
            "graphql-transport-ws",
            "graphql-ws",
        ), "Client only supports graphql-transport-ws and graphql-ws subprotocols!"
        self._subprotocol = subprotocol

    @property
    def transport(self) -> _transport.GraphqlWsTransport:
        """Underlying network transport."""
        return self._transport

    @property
    def connected(self) -> bool:
        """Indicate whether client is connected."""
        return self._is_connected

    async def connect_and_init(self, connect_only: bool = False) -> None:
        """Establish and initialize WebSocket GraphQL connection.

        1. Establish WebSocket connection.
        2. Initialize GraphQL connection. Skipped if connect_only=True.
        """
        await self._transport.connect()
        if not connect_only:
            await self._transport.send({"type": "connection_init", "payload": ""})
            resp = await self._transport.receive()
            assert resp["type"] == "connection_ack", f"Unexpected response `{resp}`!"
        self._is_connected = True

    # Default value for `id`, because `None` is also a valid value.
    AUTO = object()

    async def receive_next(self, op_id):
        """Receive GraphQL next message.

        Args:
            op_id: Subscribe message ID, in response
                to which the next message should be sent.
        Returns:
            The `payload` field of the message received.

        """
        msg_type = "next" if self._subprotocol == "graphql-transport-ws" else "data"
        return await self.receive_raw_message(assert_id=op_id, assert_type=msg_type)

    async def receive_complete(self, op_id):
        """Receive GraphQL complete message.

        Args:
            op_id: Subscribe message ID, in response
                to which the complete message should be sent.

        """
        return await self.receive_raw_message(assert_id=op_id, assert_type="complete")

    async def receive_raw_message(
        self, *, wait_id=None, assert_id=None, assert_type=None, raw_response=False
    ):
        """Receive GraphQL message checking its content.

        Args:
            wait_id: Wait until response with the given id received, all
                intermediate responses will be skipped.
            assert_id: Raise error if response id does not match value.
            assert_type: Raise error if response type does not match
                value.
            raw_response: Whether return the entire response or the only
                payload.
        Returns:
            The `payload` field of the message received or `None` or
                the entire response if the raw_response flag is True.

        """
        while True:
            response = await self._transport.receive()
            if self._is_ping_pong_message(response):
                continue
            if wait_id is None or response["id"] == wait_id:
                break

        if assert_type is not None:
            assert response["type"] == assert_type, (
                f"Type `{assert_type}` expected, but `{response['type']}` received!"
                f" Response: {response}."
            )
        if assert_id is not None:
            assert response["id"] == assert_id, "Response id != expected id!"

        payload = response.get("payload", None)
        if payload is not None and "errors" in payload:
            raise GraphqlWsResponseError(response)

        if raw_response:
            return response
        return payload

    async def execute(self, query, variables=None):
        """Execute query or mutation request and wait for the reply.

        Args:
            query: A GraphQL query string. We `dedent` it, so you do not
                have to.
            variables: Dict of variables (optional).
        Returns:
            Dictionary with the GraphQL response.

        """
        msg_id = await self.start(query, variables=variables)
        try:
            resp = await self.receive_raw_message(wait_id=msg_id)
        finally:
            # Consume 'complete' message.
            await self.receive_raw_message(wait_id=msg_id)
        return resp

    async def subscribe(self, query, *, variables=None, wait_confirmation=True):
        """Execute subscription request and wait for the confirmation.

        Args:
            query: A GraphQL string query. We `dedent` it, so you do not
                have to.
            variables: Dict of variables (optional).
            wait_confirmation: If `True` wait for the subscription
                confirmation message.

        Returns:
            The message identifier.

        """
        msg_id = await self.start(query, variables=variables)
        if wait_confirmation:
            await self.receive_raw_message(wait_id=msg_id)
        return msg_id

    async def start(self, query, variables=None, operation_name=None, msg_id=AUTO):
        """Start GraphQL request. Responses must be checked explicitly.

        Args:
            query: A GraphQL string query. We `dedent` it, so you do nothave to.
            variables: Dict of variables (optional).
            msg_id: The message identifier. Automatically generated by default.

        Returns:
            The message identifier.

        """
        msg_type = (
            "subscribe" if self._subprotocol == "graphql-transport-ws" else "start"
        )
        payload = {
            "query": textwrap.dedent(query),
            "variables": variables or {},
            "operationName": operation_name,
        }
        if msg_id is self.AUTO:
            msg_id = str(uuid.uuid4().hex)
        message = {"type": msg_type, "payload": payload, "id": msg_id}
        await self._transport.send(message)
        return msg_id

    async def complete(self, op_id):
        """Complete GraphQL request.

        Args:
            op_id: Operation id that should be completed.

        """
        msg_type = "complete" if self._subprotocol == "graphql-transport-ws" else "stop"
        message = {"type": msg_type, "id": op_id}
        await self._transport.send(message)

    async def finalize(self):
        """Disconnect and wait the transport to finish gracefully."""
        await self._transport.disconnect()
        self._is_connected = False

    async def wait_response(self, response_checker, timeout=None):
        """Wait for particular response skipping all intermediate ones.

        Useful when you need to need to wait until subscription reports
        desired state or skip subscription messages between request and
        response.

        Args:
            response_checker: Function with accepts GraphQL response as
                single parameter and must return `True` for desired
                response and `False` the other responses.
            timeout: Seconds to wait until response is received.

        Returns:
            Response payload, same as `receive`.

        Raises:
            `asyncio.TimeoutError` when timeout is reached.

        """
        if timeout is None:
            timeout = self._transport.TIMEOUT
        while timeout > 0:
            start = time.monotonic()
            try:
                response = await self.receive_raw_message()
                if response_checker(response):
                    return response
            except asyncio.TimeoutError:
                # Ignore `receive` calls timeout until wait timeout
                # is reached.
                pass
            timeout -= time.monotonic() - start
        raise asyncio.TimeoutError

    async def wait_disconnect(self, timeout=None):
        """Wait server to close the connection.

        Args:
            timeout: Seconds to wait the connection to close.

        Raises:
            `asyncio.TimeoutError` when timeout is reached.

        """
        await self._transport.wait_disconnect(timeout)
        self._is_connected = False

    @staticmethod
    def _is_ping_pong_message(response):
        """Check if received GraphQL response is ping(keepalive) or pong message."""
        return response.get("type") in ("ping", "pong", "ka")


class GraphqlWsResponseError(Exception):
    """Errors data from the GraphQL response."""

    def __init__(self, response, message=None):
        """Exception constructor."""
        super().__init__(self)
        self.message = message
        self.response = response

    def __str__(self):
        """Nice string representation."""
        return f"{self.message or 'Error in GraphQL response'}: {self.response}!"
