"""An agent provider with no model, no network and no credentials.

This is the fixture the whole suite runs against, and the thing that makes
`pytest` work offline on a laptop with no API key. It is also the smallest
complete example of the provider interface, so it doubles as the reference an
adapter author reads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from agent_host_server.provider.base import (
    AgentInfo,
    AgentSessionContext,
    ClientToolCall,
    CompletionItem,
    CompletionRequest,
    ConfigRequest,
    ConfigResolution,
    ConfigValue,
    InputQuestion,
    InputRequest,
    SessionDescription,
    ToolConfirmation,
    TurnSink,
    UserMessage,
)
from agent_host_server.provider.demo_customizations import (
    demo_customizations,
    demo_server_tools,
)

__all__ = ["EchoProvider", "EchoSession"]


def _text_of(tool_input: Any, fallback: str) -> str:
    if isinstance(tool_input, Mapping) and isinstance(tool_input.get("text"), str):
        return str(tool_input["text"])
    return fallback


def _first_active_client(context: AgentSessionContext) -> str | None:
    """The client this session was created by, if it published itself."""
    return context.active_client_id


def _selected(answer: Any) -> str | None:
    """The chosen option id out of a `single-select` answer, or None.

    Read defensively: the answer came off a client-dispatched action, and the
    reducer stores whatever it was sent.
    """
    if not isinstance(answer, Mapping):
        return None
    value = answer.get("value")
    return value if isinstance(value, str) else None


class EchoSession:
    def __init__(
        self,
        context: AgentSessionContext,
        *,
        delay: float = 0.0,
        customizations: bool = False,
        elicit: bool = False,
        confirm_tools: bool = False,
        client_tools: bool = False,
    ) -> None:
        self.context = context
        self._delay = delay
        self._customizations = customizations
        self._elicit = elicit
        self._confirm_tools = confirm_tools
        self._client_tools = client_tools
        self._cancelled = False
        #: What a client has toggled, for tests and for the demo host's log.
        self.toggled: dict[str, bool] = {}

    async def describe(self) -> SessionDescription:
        """Contribute a fully-populated customization tree, when asked to.

        Off by default: it exists to find out what a client renders, not to
        pretend the echo agent has plugins.
        """
        if not self._customizations:
            return SessionDescription()
        return SessionDescription(
            customizations=demo_customizations(), server_tools=demo_server_tools()
        )

    async def send_user_message(self, message: UserMessage, sink: TurnSink) -> None:
        self._cancelled = False
        if self._confirm_tools:
            await self._echo_via_confirmed_tool(message, sink)
            return
        if self._client_tools:
            await self._echo_via_client_tool(message, sink)
            return
        if self._elicit:
            # ADR 0005: the one sink method that suspends. The answer arrives on
            # whichever client the user used, which may not be the one that sent
            # this message -- so an adapter never sees a connection here.
            outcome = await sink.request_input(
                InputRequest(
                    message="Echo it back how?",
                    questions=[
                        InputQuestion(
                            id="style",
                            kind="single-select",
                            message="Style",
                            options=[
                                {"id": "plain", "label": "Plain"},
                                {"id": "shout", "label": "SHOUTING"},
                            ],
                        )
                    ],
                )
            )
            if not outcome.accepted:
                await sink.text_delta("(cancelled)")
                return
            style = _selected(outcome.answers.get("style"))
            text = message.text.upper() if style == "shout" else message.text
            await sink.text_delta(f"You said: {text}")
            return

        # Whatever the client settled on during `resolveSessionConfig` arrives
        # here, so the configuration is observably load-bearing rather than
        # decorative.
        prefix = self.context.config.get("prefix")
        prefix = prefix if isinstance(prefix, str) else "You said:"
        text = message.text.upper() if self.context.config.get("style") == "shout" else message.text
        for chunk in (f"{prefix} ", text):
            if self._cancelled:
                return
            if self._delay:
                await asyncio.sleep(self._delay)
            await sink.text_delta(chunk)

    async def _echo_via_confirmed_tool(self, message: UserMessage, sink: TurnSink) -> None:
        """Ask before "running" a tool, then honour whatever was approved."""
        call_id = "echo-tool-1"
        await sink.tool_call_started(
            call_id, "echo_tool", {"text": message.text}, display_name="Echo Tool"
        )
        outcome = await sink.confirm_tool_call(
            ToolConfirmation(
                call_id=call_id,
                name="echo_tool",
                display_name="Echo Tool",
                invocation_message=f"Echo {message.text!r} back",
                tool_input={"text": message.text},
                confirmation_title="Run echo tool",
                editable=True,
            )
        )
        if not outcome.approved:
            await sink.tool_call_completed(
                call_id,
                {"content": [{"type": "text", "text": "denied"}]},
                success=False,
                past_tense_message="Echo was denied",
            )
            await sink.text_delta("(denied)")
            return
        # `outcome.tool_input`, never the proposed one: an editable call lets a
        # client rewrite the parameters, and running the original would execute
        # something nobody agreed to.
        text = _text_of(outcome.tool_input, message.text)
        await sink.tool_call_completed(
            call_id,
            {"content": [{"type": "text", "text": text}]},
            past_tense_message="Echoed the message back",
        )
        await sink.text_delta(f"You said: {text}")

    async def _echo_via_client_tool(self, message: UserMessage, sink: TurnSink) -> None:
        """Delegate the work to a tool the CLIENT owns.

        The point of this mode: the agent gets the editor's own tools with no
        filesystem API on the host at all.
        """
        client_id = _first_active_client(self.context)
        if client_id is None:
            await sink.text_delta("(no active client to run a tool)")
            return
        result = await sink.run_client_tool(
            ClientToolCall(
                call_id="client-tool-1",
                name="echo",
                display_name="Client Echo",
                client_id=client_id,
                tool_input={"text": message.text},
            )
        )
        await sink.text_delta(f"The client said: {result.value}")

    async def customization_toggled(self, customization_id: str, enabled: bool) -> None:
        """A client switched a customization on or off.

        The reducer already updated state; this is how the AGENT finds out, so
        a disabled skill actually stops being used. Recorded rather than acted
        on here, because the echo agent has no behaviour to change.
        """
        self.toggled[customization_id] = enabled

    async def cancel(self, reason: str | None = None) -> None:
        self._cancelled = True

    async def aclose(self) -> None:
        self._cancelled = True


class EchoProvider:
    """Echoes the user's message back, one text delta at a time."""

    def __init__(
        self,
        *,
        provider_id: str = "echo",
        display_name: str = "Echo",
        description: str = "Echoes your message back. No model, no network.",
        model_name: str = "Echo Model v1",
        delay: float = 0.0,
        customizations: bool = False,
        elicit: bool = False,
        confirm_tools: bool = False,
        client_tools: bool = False,
        configurable: bool = False,
        capabilities: Mapping[str, Any] | None = None,
    ) -> None:
        self._configurable = configurable
        self._delay = delay
        self._customizations = customizations
        self._elicit = elicit
        self._confirm_tools = confirm_tools
        self._client_tools = client_tools
        # `display_name` is what a client labels the agent with; `models` become
        # entries in VS Code's chat model picker (AgentHostLanguageModelProvider
        # reads them straight out of root state). They are deliberately different
        # strings here so it is obvious which is which in the UI.
        #
        # `capabilities` is empty by default and that is the conformant choice:
        # every entry in `AgentCapabilities` is a client MUST NOT that only its
        # presence lifts, so a host that declares nothing is a host with the
        # narrowest surface, not an incomplete one.
        self._info = AgentInfo(
            provider=provider_id,
            display_name=display_name,
            description=description,
            models=({"id": "echo-1", "name": model_name},),
            capabilities=dict(capabilities or {}),
        )

    @property
    def agent(self) -> AgentInfo:
        return self._info

    async def resolve_config(self, request: ConfigRequest) -> ConfigResolution:
        """A small, contextual schema -- enough to see the mechanism work.

        `style` is fixed at creation. `prefix` is `sessionMutable`, so it is the
        only one a client may change afterwards, and `greeting` only appears
        once a style is chosen -- which is the point of resolving iteratively
        rather than publishing one static schema.
        """
        if not self._configurable:
            return ConfigResolution()
        properties: dict[str, Mapping[str, Any]] = {
            "style": {
                "type": "string",
                "title": "Reply style",
                "enum": ["plain", "shout"],
                "enumLabels": ["Plain", "SHOUTING"],
                "default": "plain",
            },
            "prefix": {
                "type": "string",
                "title": "Reply prefix",
                "default": "You said:",
                "sessionMutable": True,
            },
        }
        if request.values.get("style") == "shout":
            properties["greeting"] = {
                "type": "string",
                "title": "Greeting",
                "description": "Offered only once the style is SHOUTING.",
                "enumDynamic": True,
            }
        return ConfigResolution(
            properties=properties,
            values={"style": request.values.get("style", "plain")},
        )

    async def complete_config(self, request: ConfigRequest) -> Sequence[ConfigValue]:
        """Dynamic values for `greeting`, filtered by what the user has typed."""
        if request.property != "greeting":
            return ()
        options = ("HELLO", "HI THERE", "GREETINGS", "OI")
        query = request.query.upper()
        return [ConfigValue(value=o, label=o) for o in options if o.startswith(query)]

    async def complete(self, request: CompletionRequest) -> Sequence[CompletionItem]:
        """Suggest a couple of fake attachments after `#`.

        Enough to exercise the offset conversion, which is the part that goes
        wrong: `offset` is in UTF-16 code units and a Python string index is
        not the same number once anything outside the BMP is in the text.
        """
        prefix = request.text_before_cursor()
        marker = prefix.rfind("#")
        if marker < 0:
            return ()
        typed = prefix[marker + 1 :]
        start = len(prefix[:marker].encode("utf-16-le")) // 2
        return [
            CompletionItem(
                insert_text=f"#{name}",
                range_start=start,
                range_end=request.offset,
                # `type`, not `kind`; `resource`, not `file`. The discriminant
                # is MessageAttachmentKind (channels-chat/state.ts:530-541) and
                # the shipping client's switch has a bare `default: return`, so
                # the wrong key dropped every item silently. `label` is
                # required on every attachment and is what the picker shows.
                attachment={
                    "type": "resource",
                    "uri": f"file:///demo/{name}",
                    "label": name,
                    "displayKind": "document",
                },
            )
            for name in ("readme.md", "recipe.txt")
            if name.startswith(typed)
        ]

    async def create_session(self, context: AgentSessionContext) -> EchoSession:
        return EchoSession(
            context,
            delay=self._delay,
            customizations=self._customizations,
            elicit=self._elicit,
            confirm_tools=self._confirm_tools,
            client_tools=self._client_tools,
        )
